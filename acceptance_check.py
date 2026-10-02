#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建筑工程验收流程校验工具（纯 Python 标准库，单文件）。

输入文件格式（UTF-8 文本；`#` 开头为注释，空行忽略；字段用 `|` 分隔）：

    [工序]
    名称 | 依赖工序(逗号分隔,无依赖留空) | 合格标准 | 整改标准
    [验收]
    工序名 | 合格|整改 | 整改说明(结果为整改时必填,多个整改项用 ; 分隔)
    [复验]
    工序名 | 整改项 | 合格|仍不合格

三个段可重复出现，事件按在文件中出现的先后顺序处理（跨流状态延续）。

用法：
    python3 acceptance_check.py 输入文件
    python3 acceptance_check.py --demo      运行内置演示样例
    python3 acceptance_check.py -           从标准输入读取

退出码：0 = 无错误；1 = 存在错误；2 = 用法/文件错误。
"""

import sys
from dataclasses import dataclass, field

# 复验“仍不合格”累计阈值：达到即触发停工级联。
# 取值理由：参照工地“三检制”（自检/互检/专检）惯例给施工方最多 3 次整改机会——
# 2 次偏严（单次复验失败可能含测量误差等偶发因素），
# 4 次以上则质量风险、返工成本与工期损失累积到不可接受，故取 3。
REJECT_THRESHOLD = 3

# 工序状态
PENDING = "PENDING"        # 待验收
ACCEPTED = "ACCEPTED"      # 验收合格
RECTIFYING = "RECTIFYING"  # 整改中（存在未复验通过的整改项，期间不可验收）
BLOCKED = "BLOCKED"        # 不可验收（被停工工序级联阻断）
STOPPED = "STOPPED"        # 停工（复验仍不合格累计达阈值）

STATUS_TEXT = {
    PENDING: "待验收",
    ACCEPTED: "合格",
    RECTIFYING: "整改中(未复验通过,不可验收)",
    BLOCKED: "不可验收(级联阻断)",
    STOPPED: "停工",
}


@dataclass
class ProcessDef:
    name: str
    deps: list
    accept_std: str
    rectify_std: str
    line: int


@dataclass
class ProcState:
    status: str = PENDING
    rectify_items: dict = field(default_factory=dict)  # 整改项 -> 仍不合格累计次数
    resolved: set = field(default_factory=set)          # 已复验合格的整改项
    seen_acceptance: bool = False                       # 是否已有验收记录（查重复验收）
    block_reason: str = ""


@dataclass
class Event:
    line: int
    kind: str    # 'accept' 或 'recheck'
    fields: list


class Engine:
    def __init__(self):
        self.defs = {}          # 工序名 -> ProcessDef
        self.states = {}        # 工序名 -> ProcState
        self.dependents = {}    # 工序名 -> [直接后继工序名]
        self.events = []        # 按文件顺序的验收/复验事件
        self.errors = []        # (行号, 类别, 说明)

    # ---------- 解析 ----------
    def error(self, line, category, msg):
        self.errors.append((line, category, msg))

    def parse(self, text):
        section = None
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                if section not in ("工序", "验收", "复验"):
                    self.error(lineno, "格式错误", f"未知段名 [{section}]")
                    section = None
                continue
            parts = [p.strip() for p in line.split("|")]
            if section == "工序":
                self._parse_def(lineno, parts)
            elif section == "验收":
                if len(parts) < 2:
                    self.error(lineno, "格式错误", "验收记录至少需要: 工序 | 结果")
                    continue
                note = parts[2] if len(parts) > 2 else ""
                self.events.append(Event(lineno, "accept", [parts[0], parts[1], note]))
            elif section == "复验":
                if len(parts) < 3:
                    self.error(lineno, "格式错误", "复验记录需要: 工序 | 整改项 | 结果")
                    continue
                self.events.append(Event(lineno, "recheck", parts[:3]))
            else:
                self.error(lineno, "格式错误", "记录必须位于 [工序]/[验收]/[复验] 段内")

    def _parse_def(self, lineno, parts):
        if len(parts) < 3:
            self.error(lineno, "格式错误", "工序定义至少需要: 名称 | 依赖 | 合格标准 [| 整改标准]")
            return
        name = parts[0]
        if not name:
            self.error(lineno, "格式错误", "工序名称不能为空")
            return
        if name in self.defs:
            self.error(lineno, "重复定义", f"工序「{name}」重复定义")
            return
        deps = [d.strip() for d in parts[1].replace("，", ",").split(",") if d.strip()]
        accept_std = parts[2]
        rectify_std = parts[3] if len(parts) > 3 else ""
        self.defs[name] = ProcessDef(name, deps, accept_std, rectify_std, lineno)
        self.states[name] = ProcState()

    def validate_defs(self):
        for d in self.defs.values():
            if d.name in d.deps:
                self.error(d.line, "依赖错误", f"工序「{d.name}」不能依赖自身")
            for dep in d.deps:
                if dep not in self.defs:
                    self.error(d.line, "依赖错误",
                               f"工序「{d.name}」依赖了不存在的工序「{dep}」")
                else:
                    self.dependents.setdefault(dep, []).append(d.name)
        # 循环依赖检测（DFS 三色标记）
        color = {}
        def visit(n, stack):
            color[n] = 1
            for dep in self.defs[n].deps:
                if dep not in self.defs:
                    continue
                if color.get(dep) == 1:
                    self.error(self.defs[n].line, "依赖错误",
                               "检测到循环依赖: " + " -> ".join(stack + [dep]))
                elif color.get(dep) is None:
                    visit(dep, stack + [dep])
            color[n] = 2
        for name in self.defs:
            if color.get(name) is None:
                visit(name, [name])

    # ---------- 事件处理 ----------
    def run(self):
        for ev in self.events:
            if ev.kind == "accept":
                self._accept(ev)
            else:
                self._recheck(ev)

    def _accept(self, ev):
        name, result, note = ev.fields
        if name not in self.defs:
            self.error(ev.line, "引用错误", f"验收引用了不存在的工序「{name}」")
            return
        st = self.states[name]
        if st.seen_acceptance:
            self.error(ev.line, "重复验收", f"工序「{name}」已有验收记录，重复验收无效")
            return
        st.seen_acceptance = True
        if st.status == STOPPED:
            self.error(ev.line, "不可验收", f"工序「{name}」已停工，不能再验收")
            return
        if st.status == BLOCKED:
            self.error(ev.line, "不可验收",
                       f"工序「{name}」被级联阻断（{st.block_reason}），不可验收")
            return
        unpassed = [d for d in self.defs[name].deps
                    if d in self.states and self.states[d].status != ACCEPTED]
        if unpassed:
            desc = "、".join(f"「{d}」({STATUS_TEXT[self.states[d].status]})" for d in unpassed)
            self.error(ev.line, "不可验收",
                       f"工序「{name}」的依赖工序未验收通过: {desc}")
            return
        if result == "合格":
            st.status = ACCEPTED
        elif result == "整改":
            items = [s.strip() for s in note.replace("；", ";").split(";") if s.strip()]
            if not items:
                self.error(ev.line, "格式错误",
                           f"工序「{name}」验收结果为整改，但整改说明为空")
                items = ["(未命名整改项)"]
            st.status = RECTIFYING
            for it in items:
                st.rectify_items.setdefault(it, 0)
        else:
            self.error(ev.line, "格式错误",
                       f"工序「{name}」验收结果非法: 「{result}」，应为 合格/整改")

    def _recheck(self, ev):
        name, item, result = ev.fields
        if name not in self.defs:
            self.error(ev.line, "引用错误", f"复验引用了不存在的工序「{name}」")
            return
        st = self.states[name]
        if st.status == STOPPED:
            self.error(ev.line, "已停工", f"工序「{name}」已停工，复验无效")
            return
        if st.status == BLOCKED:
            self.error(ev.line, "级联阻断",
                       f"工序「{name}」已被级联阻断（{st.block_reason}），复验无效")
            return
        if st.status != RECTIFYING:
            self.error(ev.line, "未整改",
                       f"复验引用了未处于整改状态的工序「{name}」"
                       f"（当前状态: {STATUS_TEXT[st.status]}）")
            return
        if item not in st.rectify_items:
            self.error(ev.line, "引用错误",
                       f"工序「{name}」不存在整改项「{item}」"
                       f"（已登记: {'、'.join(st.rectify_items) or '无'}）")
            return
        if item in st.resolved:
            self.error(ev.line, "重复复验",
                       f"工序「{name}」整改项「{item}」已复验合格，重复复验无效")
            return
        if result == "合格":
            st.resolved.add(item)
            if st.resolved == set(st.rectify_items):
                st.status = ACCEPTED
        elif result == "仍不合格":
            st.rectify_items[item] += 1
            if st.rectify_items[item] >= REJECT_THRESHOLD:
                self._stop(name, f"整改项「{item}」仍不合格累计 "
                                 f"{st.rectify_items[item]} 次，达到阈值 {REJECT_THRESHOLD}")
        else:
            self.error(ev.line, "格式错误",
                       f"工序「{name}」复验结果非法: 「{result}」，应为 合格/仍不合格")

    def _stop(self, name, reason):
        self.states[name].status = STOPPED
        self.error(self.defs[name].line, "停工", f"工序「{name}」触发停工: {reason}")
        # 级联：所有（传递）依赖该工序且尚未合格的工序置为不可验收
        queue = list(self.dependents.get(name, []))
        seen = set()
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            cst = self.states[cur]
            if cst.status in (PENDING, RECTIFYING):
                cst.status = BLOCKED
                cst.block_reason = f"上游工序「{name}」停工"
                self.error(self.defs[cur].line, "级联阻断",
                           f"工序「{cur}」因上游工序「{name}」停工而不可验收")
            queue.extend(self.dependents.get(cur, []))

    # ---------- 输出 ----------
    def report(self, out=sys.stdout):
        print("=" * 60, file=out)
        print("一、验收状态", file=out)
        print("=" * 60, file=out)
        if not self.defs:
            print("（无工序定义）", file=out)
        for name, d in self.defs.items():
            st = self.states[name]
            print(f"工序: {name}", file=out)
            print(f"  状态: {STATUS_TEXT[st.status]}", file=out)
            print(f"  依赖: {'、'.join(d.deps) if d.deps else '无'}", file=out)
            print(f"  合格标准: {d.accept_std}", file=out)
            if d.rectify_std:
                print(f"  整改标准: {d.rectify_std}", file=out)
            if st.rectify_items:
                for it, cnt in st.rectify_items.items():
                    mark = "已复验合格" if it in st.resolved else \
                           f"未通过(仍不合格 {cnt}/{REJECT_THRESHOLD} 次)"
                    print(f"  整改项「{it}」: {mark}", file=out)
            if st.status == BLOCKED:
                print(f"  阻断原因: {st.block_reason}", file=out)
        print(file=out)
        print("=" * 60, file=out)
        print("二、错误清单", file=out)
        print("=" * 60, file=out)
        if not self.errors:
            print("无错误。", file=out)
        for i, (line, cat, msg) in enumerate(self.errors, 1):
            loc = f"第{line}行" if line else "-"
            print(f"{i:>3}. [{cat}] ({loc}) {msg}", file=out)
        print(file=out)
        print(f"合计: 工序 {len(self.defs)} 个, 事件 {len(self.events)} 条, "
              f"错误 {len(self.errors)} 条", file=out)


DEMO_INPUT = """\
# 演示样例：覆盖全部校验场景
[工序]
土方开挖 | | 标高偏差≤50mm | 超挖部分回填夯实
垫层施工 | 土方开挖 | 强度≥C15 | 强度不足部位凿除重浇
钢筋绑扎 | 垫层施工 | 间距偏差≤10mm | 间距超限重新绑扎
模板安装 | 钢筋绑扎 | 垂直度≤3mm | 校正加固
混凝土浇筑 | 模板安装 | 坍落度合格 | 离析部位返工
砌体工程 | 混凝土浇筑 | 灰缝饱满度≥80% | 透明缝修补
抹灰工程 | | 表面平整度≤4mm | 空鼓处铲除重做
门窗安装 | | 启闭灵活无渗漏 | 缝隙发泡处理

[验收]
土方开挖 | 合格 |
垫层施工 | 整改 | 强度不足; 表面起砂
钢筋绑扎 | 合格 |
垫层施工 | 合格 |
模板安装 | 合格 |
混凝土浇筑 | 整改 | 局部离析
砌体工程 | 合格 |
抹灰工程 | 合格 |
门窗安装 | 整改 | 窗框缝隙过大
不存在的工序 | 合格 |

[复验]
垫层施工 | 强度不足 | 仍不合格
垫层施工 | 强度不足 | 仍不合格
垫层施工 | 表面起砂 | 合格
垫层施工 | 强度不足 | 仍不合格
钢筋绑扎 | 间距超限 | 合格
混凝土浇筑 | 局部离析 | 合格
抹灰工程 | 空鼓 | 合格
门窗安装 | 未登记的整改项 | 合格
"""


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    if argv[1] == "--demo":
        text = DEMO_INPUT
    elif argv[1] == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(argv[1], "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"无法读取文件: {e}", file=sys.stderr)
            return 2
    eng = Engine()
    eng.parse(text)
    eng.validate_defs()
    eng.run()
    eng.report()
    return 1 if eng.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
