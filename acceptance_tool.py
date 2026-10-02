#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acceptance_tool.py — 建筑工程工序验收状态机与错误报告工具（纯 Python 标准库，单文件）

用法:
    python3 acceptance_tool.py 输入.json [--threshold N] [--json]
    python3 acceptance_tool.py --demo          # 运行内置示例（覆盖全部规则）
    python3 acceptance_tool.py - < 输入.json   # 从标准输入读取

输入 JSON 结构（键名为中文，字符串值）:
{
  "工序定义": [ {"名称": "地基工程", "依赖": ["场地平整"],
                "合格标准": "...", "整改标准": "..."}, ... ],
  "验收流":   [ {"工序": "地基工程", "结果": "合格|整改", "整改说明": "..."}, ... ],
  "复验流":   [ {"工序": "地基工程", "整改项": "...", "结果": "合格|仍不合格"}, ... ]
}

可选字段 "序号"（整数）：若验收流/复验流记录带有序号，则两条流合并后按序号
升序处理，实现跨流交错事件的真实时序；无序号时默认先验收流后复验流。

停工阈值说明（--threshold，默认 3）：
    参照工程"三检制"惯例——首次验收不合格给予整改，整改后给予两次复验机会；
    同一工序复验"仍不合格"累计达到 3 次，说明整改方案或施工能力存在根本性
    缺陷，继续施工将放大质量风险，故触发停工并级联影响所有下游工序。
"""
import argparse
import json
import sys
from dataclasses import dataclass, field

# ---- 状态常量 ---------------------------------------------------------------
STATUS_PASS = "验收通过"
STATUS_STOP = "停工"
STATUS_CASCADE = "级联停工"
STATUS_RECTIFY = "整改中(不可验收)"
STATUS_BLOCKED = "不可验收(依赖未通过)"
STATUS_PENDING = "待验收"

RESULT_PASS = "合格"
RESULT_RECTIFY = "整改"
RESULT_STILL_FAIL = "仍不合格"

DEFAULT_THRESHOLD = 3


# ---- 数据模型 ---------------------------------------------------------------
@dataclass
class Process:
    name: str
    deps: list
    pass_criteria: str = ""
    rectify_criteria: str = ""
    accepted: bool = False                 # 是否已有验收记录（防重复验收）
    passed: bool = False                   # 是否最终验收通过
    stopped: bool = False                  # 是否被停工
    open_items: list = field(default_factory=list)   # 待复验的整改项
    fail_counts: dict = field(default_factory=dict)  # 整改项 -> 复验仍不合格次数


class Engine:
    """工序验收状态机：顺序消费验收/复验事件，跨流保持状态。"""

    def __init__(self, definitions, threshold=DEFAULT_THRESHOLD):
        self.threshold = threshold
        self.procs = {}
        self.errors = []
        self._closure_cache = {}
        for d in definitions:
            name = str(d.get("名称", "")).strip()
            if not name:
                self._err("定义错误", "工序定义", "(未命名)", "工序定义缺少名称")
                continue
            if name in self.procs:
                self._err("定义错误", "工序定义", name, "工序重复定义，后者被忽略")
                continue
            deps = [str(x).strip() for x in (d.get("依赖") or [])]
            self.procs[name] = Process(
                name, deps,
                str(d.get("合格标准", "")), str(d.get("整改标准", "")),
            )
        # 定义阶段即检查依赖引用
        for p in self.procs.values():
            for dep in p.deps:
                if dep not in self.procs:
                    self._err("引用不存在的工序", "工序定义", p.name,
                              f"依赖的工序「{dep}」未定义")

    # ---- 内部工具 -----------------------------------------------------------
    def _err(self, category, source, process, message):
        self.errors.append({"类别": category, "来源": source,
                            "工序": process, "说明": message})

    def dep_closure(self, name):
        """工序的全部传递依赖（含不存在的名字，结构在加载后不变，可缓存）。"""
        if name in self._closure_cache:
            return self._closure_cache[name]
        seen, stack = set(), list(self.procs[name].deps)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur in self.procs:
                stack.extend(self.procs[cur].deps)
        self._closure_cache[name] = seen
        return seen

    def status_of(self, name):
        """派生状态：由基础事实（passed/stopped/open_items/依赖图）实时推导，
        因此停工后的级联状态天然正确，无需手动传播。"""
        p = self.procs[name]
        if p.passed:
            return STATUS_PASS
        if p.stopped:
            return STATUS_STOP
        for dep in self.dep_closure(name):
            if dep in self.procs and self.procs[dep].stopped:
                return STATUS_CASCADE
        if p.open_items:
            return STATUS_RECTIFY
        for dep in p.deps:
            if dep not in self.procs or not self.procs[dep].passed:
                return STATUS_BLOCKED
        return STATUS_PENDING

    # ---- 事件入口 -----------------------------------------------------------
    def run(self, acceptance, reinspection):
        events = []
        for i, r in enumerate(acceptance, 1):
            events.append((r.get("序号"), f"验收流#{i}", "验收", r))
        for i, r in enumerate(reinspection, 1):
            events.append((r.get("序号"), f"复验流#{i}", "复验", r))
        # 带序号的记录按序号排，无序号保持 验收流→复验流 的原始相对顺序
        indexed = list(enumerate(events))
        indexed.sort(key=lambda t: (t[1][0] if t[1][0] is not None
                                    else float("inf"), t[0]))
        for _, (_, src, kind, rec) in indexed:
            if kind == "验收":
                self._do_accept(src, rec)
            else:
                self._do_reinspect(src, rec)
        self._finalize()

    # ---- 验收流 -------------------------------------------------------------
    def _do_accept(self, src, rec):
        name = str(rec.get("工序", "")).strip()
        result = rec.get("结果", "")
        note = str(rec.get("整改说明", "")).strip()
        p = self.procs.get(name)
        if p is None:
            self._err("引用不存在的工序", src, name or "(空)",
                      "验收记录引用了未定义的工序")
            return
        if p.accepted:
            self._err("重复验收", src, name,
                      "该工序已有验收记录，重复验收无效（整改后应走复验流）")
            return
        st = self.status_of(name)
        if st in (STATUS_STOP, STATUS_CASCADE):
            self._err("停工不可验收", src, name,
                      f"工序处于「{st}」状态，不可验收")
            return
        unpassed = [d for d in p.deps
                    if d in self.procs and not self.procs[d].passed]
        missing = [d for d in p.deps if d not in self.procs]
        if unpassed or missing:
            parts = []
            if unpassed:
                parts.append("依赖工序未验收通过: " + ", ".join(unpassed))
            if missing:
                parts.append("依赖工序不存在: " + ", ".join(missing))
            self._err("依赖未通过不可验收", src, name, "；".join(parts))
            return
        if result not in (RESULT_PASS, RESULT_RECTIFY):
            self._err("结果非法", src, name,
                      f"验收结果「{result}」非法，应为 合格/整改")
            return
        p.accepted = True
        if result == RESULT_PASS:
            p.passed = True
        else:
            if not note:
                self._err("整改说明缺失", src, name,
                          "验收结果为整改但未提供整改说明，已按「未说明整改项」登记")
                note = "未说明整改项"
            p.open_items.append(note)
            p.fail_counts.setdefault(note, 0)

    # ---- 复验流 -------------------------------------------------------------
    def _do_reinspect(self, src, rec):
        name = str(rec.get("工序", "")).strip()
        item = str(rec.get("整改项", "")).strip()
        result = rec.get("结果", "")
        p = self.procs.get(name)
        if p is None:
            self._err("引用不存在的工序", src, name or "(空)",
                      "复验记录引用了未定义的工序")
            return
        if p.stopped:
            self._err("已停工", src, name, "工序已停工，复验记录无效")
            return
        if not p.open_items:
            self._err("复验引用未整改的工序", src, name,
                      "该工序当前没有待复验的整改项")
            return
        if item not in p.open_items:
            self._err("整改项不存在", src, name,
                      f"整改项「{item}」不存在，当前待复验整改项: "
                      + ", ".join(p.open_items))
            return
        if result == RESULT_PASS:
            p.open_items.remove(item)
            if not p.open_items:
                p.passed = True          # 全部整改项复验合格 → 工序通过
        elif result == RESULT_STILL_FAIL:
            p.fail_counts[item] = p.fail_counts.get(item, 0) + 1
            total = sum(p.fail_counts.values())
            if total >= self.threshold:
                p.stopped = True
                affected = sorted(
                    n for n, q in self.procs.items()
                    if n != name and not q.passed and not q.stopped
                    and name in self.dep_closure(n))
                self._err("停工级联", src, name,
                          f"复验仍不合格累计 {total} 次达到阈值 "
                          f"{self.threshold}，工序停工；级联影响: "
                          + (", ".join(affected) if affected else "无"))
        else:
            self._err("结果非法", src, name,
                      f"复验结果「{result}」非法，应为 合格/仍不合格")

    # ---- 收尾报告 -----------------------------------------------------------
    def _finalize(self):
        """跨流状态延续到流程结束：仍未闭环的整改项要报告不可验收。"""
        for name, p in self.procs.items():
            if p.open_items and not p.stopped:
                self._err("整改未闭环不可验收", "流程结束", name,
                          "整改项未复验通过: " + ", ".join(p.open_items))


# ---- 输出 -------------------------------------------------------------------
def render_text(engine):
    lines = ["===== 验收状态 ====="]
    for name, p in engine.procs.items():
        st = engine.status_of(name)
        deps = ", ".join(p.deps) if p.deps else "无"
        line = f"[{st}] {name} (依赖: {deps})"
        if p.open_items:
            items = ", ".join(
                f"{it}(仍不合格{p.fail_counts.get(it, 0)}次)"
                for it in p.open_items)
            line += f" | 待复验整改项: {items}"
        lines.append(line)
    lines.append("")
    lines.append("===== 错误清单 =====")
    if not engine.errors:
        lines.append("（无错误）")
    for i, e in enumerate(engine.errors, 1):
        lines.append(f"{i}. [{e['类别']}] {e['来源']} "
                     f"工序「{e['工序']}」: {e['说明']}")
    passed = sum(1 for n in engine.procs
                 if engine.status_of(n) == STATUS_PASS)
    lines.append("")
    lines.append(f"汇总: 工序 {len(engine.procs)} 个，验收通过 {passed} 个，"
                 f"错误/报告 {len(engine.errors)} 条，"
                 f"停工阈值 {engine.threshold}")
    return "\n".join(lines)


def render_json(engine):
    return json.dumps({
        "停工阈值": engine.threshold,
        "状态": [{
            "工序": name,
            "状态": engine.status_of(name),
            "依赖": p.deps,
            "待复验整改项": list(p.open_items),
            "仍不合格次数": dict(p.fail_counts),
        } for name, p in engine.procs.items()],
        "错误": engine.errors,
    }, ensure_ascii=False, indent=2)


# ---- 内置示例（覆盖全部规则） ------------------------------------------------
DEMO = {
    "工序定义": [
        {"名称": "场地平整", "依赖": [], "合格标准": "标高误差≤50mm", "整改标准": "重新找平"},
        {"名称": "地基工程", "依赖": ["场地平整"], "合格标准": "承载力达标", "整改标准": "加固修补"},
        {"名称": "主体结构", "依赖": ["地基工程"], "合格标准": "强度达标", "整改标准": "返工加固"},
        {"名称": "装饰装修", "依赖": ["主体结构"], "合格标准": "观感合格", "整改标准": "返修"},
        {"名称": "机电安装", "依赖": ["主体结构"], "合格标准": "通电通水", "整改标准": "重新敷设"},
        {"名称": "园林绿化", "依赖": [], "合格标准": "成活率≥95%", "整改标准": "补植"},
    ],
    "验收流": [
        {"工序": "主体结构", "结果": "合格"},                       # 依赖未通过 → 不可验收
        {"工序": "场地平整", "结果": "合格"},
        {"工序": "园林绿化", "结果": "合格"},
        {"工序": "地基工程", "结果": "整改", "整改说明": "地基裂缝修补"},
        {"工序": "地基工程", "结果": "合格"},                       # 重复验收
        {"工序": "幕墙工程", "结果": "合格"},                       # 引用不存在的工序
        {"工序": "主体结构", "结果": "合格"},                       # 地基整改未闭环 → 仍不可验收
    ],
    "复验流": [
        {"工序": "装饰装修", "整改项": "墙面空鼓", "结果": "合格"},  # 复验引用未整改的工序
        {"工序": "地基工程", "整改项": "地基裂缝修补", "结果": "仍不合格"},
        {"工序": "地基工程", "整改项": "桩基偏位", "结果": "合格"},  # 整改项不存在
        {"工序": "地基工程", "整改项": "地基裂缝修补", "结果": "仍不合格"},
        {"工序": "地基工程", "整改项": "地基裂缝修补", "结果": "仍不合格"},  # 累计3次 → 停工级联
        {"工序": "地基工程", "整改项": "地基裂缝修补", "结果": "合格"},      # 已停工，记录无效
    ],
}


# ---- 主入口 -----------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="建筑工程工序验收状态机与错误报告工具（纯标准库）")
    ap.add_argument("input", nargs="?",
                    help="输入 JSON 文件路径，'-' 表示标准输入")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"复验仍不合格累计停工阈值（默认 {DEFAULT_THRESHOLD}）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.threshold < 1:
        ap.error("--threshold 必须 ≥ 1")

    if args.demo:
        data = DEMO
    else:
        if not args.input:
            ap.error("缺少输入文件（或使用 --demo）")
        try:
            text = (sys.stdin.read() if args.input == "-"
                    else open(args.input, encoding="utf-8").read())
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读取输入失败: {exc}", file=sys.stderr)
            return 2

    engine = Engine(data.get("工序定义", []), threshold=args.threshold)
    engine.run(data.get("验收流", []), data.get("复验流", []))
    print(render_json(engine) if args.json else render_text(engine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
