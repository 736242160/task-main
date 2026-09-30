#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""燃气入户安检隐患管理工具（纯 Python 标准库，单文件）。

用法：
    python3 gas_inspection.py 输入文件      # 从文件读取
    python3 gas_inspection.py              # 从标准输入读取
    python3 gas_inspection.py --demo       # 运行内置示例

输入格式（按行，# 开头为注释，空行忽略；安检流与整改流按出现顺序处理，
状态跨流延续）：

    户   户号 名称 地址 上次安检日期          例：户 H001 张伟 幸福路1号 2025-01-05
    类型 隐患名称 等级(严重|一般)             例：类型 燃气泄漏 严重
    安检 户号 日期 [隐患1,隐患2,...]          例：安检 H001 2025-03-01 燃气泄漏,软管老化
    整改 户号 隐患名称 结果(完成|延期) [日期]  例：整改 H001 燃气泄漏 延期 2025-03-05

说明：
    * 地址与隐患名称允许包含空格（按字段位置解析）。
    * 整改行的日期可省略，省略时取该户最近一次事件的日期。
    * 隐患列表用中文或英文逗号分隔；安检不带隐患列表表示本次安检无隐患。

自定规则（理由）：
    * 延期复查期限：严重隐患 7 天，一般隐患 30 天。
      严重隐患（如燃气泄漏）直接威胁人身与财产安全，须在一周内复查闭环；
      一般隐患风险较低，给一个抄表/账单周期（30 天）较为合理。
    * 到期判定：以安检流/整改流中后续事件日期（及全程最大日期）作为
      “当前日期”，超过复查期限仍未销号即为到期未整改。
    * 停供级联：严重隐患到期未整改 -> 该户立即停供并报告；停供期间该户
      后续安检状态级联为“停供中安检”（隐患仍登记在册）；当该户严重隐患
      全部销号后自动恢复供气并报告级联更新。
    * 一般隐患到期未整改只发催办警示，不停供。
"""

import sys
from datetime import date, timedelta

SEVERE_RECHECK_DAYS = 7    # 严重隐患延期复查期限（天）
GENERAL_RECHECK_DAYS = 30  # 一般隐患延期复查期限（天）

LEVELS = ("严重", "一般")
RESULTS = ("完成", "延期")

DEMO_INPUT = """\
# ---- 户号定义：户 户号 名称 地址 上次安检日期 ----
户 H001 张伟 幸福路1号 2025-01-05
户 H002 李芳 幸福路2号 2025-01-06
户 H003 王强 和平街8号 2025-01-07

# ---- 隐患类型：类型 名称 等级 ----
类型 燃气泄漏 严重
类型 软管老化 一般
类型 无熄火保护 一般

# ---- 安检流：安检 户号 日期 隐患列表 ----
安检 H001 2025-03-01 燃气泄漏,软管老化
安检 H002 2025-03-02 软管老化
安检 H002 2025-03-03 软管老化
安检 H009 2025-03-04 燃气泄漏
安检 H001 2025-03-10 未知隐患

# ---- 整改流：整改 户号 隐患 结果 [日期] ----
整改 H001 软管老化 完成 2025-03-05
整改 H001 燃气泄漏 延期 2025-03-05
整改 H002 软管老化 延期 2025-03-06
安检 H003 2025-03-20
安检 H001 2025-03-25 无熄火保护
整改 H001 燃气泄漏 完成 2025-03-26
整改 H009 软管老化 完成 2025-03-27
整改 H002 软管老化 完成 2025-04-10
整改 H002 软管老化 完成 2025-04-10
"""


def parse_date(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


class Engine:
    def __init__(self):
        self.households = {}      # 户号 -> {name, addr, last_check, suspended}
        self.hazard_types = {}    # 隐患名 -> 等级
        self.open = {}            # (户号, 隐患名) -> {level, found, deadline, overdue}
        self.reports = []         # (类别, 消息)
        self.max_date = None

    # ---------- 报告 ----------
    def report(self, kind, msg):
        self.reports.append((kind, msg))

    # ---------- 到期检查（跨流状态延续的核心） ----------
    def check_overdue(self, as_of):
        for (hid, hname), hz in sorted(self.open.items()):
            if hz["deadline"] is None or hz["deadline"] >= as_of or hz["overdue"]:
                continue
            hz["overdue"] = True
            hh = self.households[hid]
            if hz["level"] == "严重":
                if not hh["suspended"]:
                    hh["suspended"] = True
                    self.report("停供级联",
                        f"{as_of}：户 {hid}（{hh['name']}）严重隐患「{hname}」"
                        f"到期未整改（复查期限 {hz['deadline']}），已停供")
                else:
                    self.report("警示",
                        f"{as_of}：户 {hid} 严重隐患「{hname}」到期未整改"
                        f"（复查期限 {hz['deadline']}），该户已处于停供状态")
            else:
                self.report("催办",
                    f"{as_of}：户 {hid}（{hh['name']}）一般隐患「{hname}」"
                    f"到期未整改（复查期限 {hz['deadline']}），请催办")

    def maybe_restore(self, hid):
        hh = self.households[hid]
        if not hh["suspended"]:
            return
        if any(h["level"] == "严重" for (h, _), h in self.open.items() if h == hid):
            return
        hh["suspended"] = False
        self.report("停供级联",
            f"户 {hid}（{hh['name']}）严重隐患已全部销号，恢复供气")

    def advance_date(self, d):
        if d is None:
            return
        if self.max_date is None or d > self.max_date:
            self.max_date = d
        self.check_overdue(d)

    # ---------- 指令处理 ----------
    def define_household(self, tokens, lineno):
        if len(tokens) < 5:
            self.report("错误", f"第{lineno}行：户定义字段不足（户 户号 名称 地址 日期）")
            return
        hid, name = tokens[1], tokens[2]
        d = parse_date(tokens[-1])
        if d is None:
            self.report("错误", f"第{lineno}行：户 {hid} 上次安检日期无效：{tokens[-1]!r}")
            return
        if hid in self.households:
            self.report("错误", f"第{lineno}行：户号 {hid} 重复定义，已忽略")
            return
        addr = " ".join(tokens[3:-1])
        self.households[hid] = {"name": name, "addr": addr,
                                "last_check": d, "suspended": False}
        self.advance_date(d)

    def define_hazard_type(self, tokens, lineno):
        if len(tokens) < 3 or tokens[-1] not in LEVELS:
            self.report("错误", f"第{lineno}行：隐患类型定义无效（类型 名称 严重|一般）")
            return
        name = " ".join(tokens[1:-1])
        if name in self.hazard_types:
            self.report("错误", f"第{lineno}行：隐患类型「{name}」重复定义，已覆盖")
        self.hazard_types[name] = tokens[-1]

    def do_inspection(self, tokens, lineno):
        if len(tokens) < 3:
            self.report("错误", f"第{lineno}行：安检记录字段不足（安检 户号 日期 [隐患列表]）")
            return
        hid = tokens[1]
        d = parse_date(tokens[2])
        if d is None:
            self.report("错误", f"第{lineno}行：安检日期无效：{tokens[2]!r}")
            return
        self.advance_date(d)
        if hid not in self.households:
            self.report("错误", f"第{lineno}行：安检引用了不存在的户号 {hid}，已跳过")
            return
        hh = self.households[hid]
        hh["last_check"] = d
        if hh["suspended"]:
            self.report("停供级联",
                f"{d}：户 {hid}（{hh['name']}）处于停供状态，本次安检按“停供中安检”处理")
        hazards = []
        if len(tokens) > 3:
            raw = " ".join(tokens[3:]).replace("，", ",")
            hazards = [h.strip() for h in raw.split(",") if h.strip()]
        for hname in hazards:
            if hname not in self.hazard_types:
                self.report("错误",
                    f"第{lineno}行：户 {hid} 安检隐患「{hname}」未在隐患类型中定义，已忽略")
                continue
            key = (hid, hname)
            if key in self.open:
                self.report("警示",
                    f"{d}：户 {hid} 隐患「{hname}」重复安检上报"
                    f"（首次发现于 {self.open[key]['found']}，尚未销号），不重复登记")
                continue
            self.open[key] = {"level": self.hazard_types[hname], "found": d,
                              "deadline": None, "overdue": False}
            self.report("隐患",
                f"{d}：户 {hid}（{hh['name']}）新发现{self.hazard_types[hname]}隐患「{hname}」")

    def do_rectification(self, tokens, lineno):
        # 整改 户号 隐患名... 结果 [日期]
        idx = next((i for i in range(len(tokens) - 1, 1, -1) if tokens[i] in RESULTS), None)
        if idx is None or idx < 3:
            self.report("错误", f"第{lineno}行：整改记录无效（整改 户号 隐患 完成|延期 [日期]）")
            return
        hid = tokens[1]
        hname = " ".join(tokens[2:idx])
        result = tokens[idx]
        d = parse_date(tokens[idx + 1]) if len(tokens) > idx + 1 else None
        if len(tokens) > idx + 1 and d is None:
            self.report("错误", f"第{lineno}行：整改日期无效：{tokens[idx + 1]!r}")
            return
        if d is None:
            d = self.max_date  # 省略日期时取当前已知最新日期
        self.advance_date(d)
        if hid not in self.households:
            self.report("错误", f"第{lineno}行：整改引用了不存在的户号 {hid}，已跳过")
            return
        key = (hid, hname)
        if key not in self.open:
            self.report("错误",
                f"第{lineno}行：户 {hid} 无在册隐患「{hname}」，整改记录已跳过")
            return
        hz = self.open[key]
        hh = self.households[hid]
        if result == "完成":
            del self.open[key]
            self.report("销号",
                f"{d}：户 {hid}（{hh['name']}）{hz['level']}隐患「{hname}」整改完成，已销号")
            self.maybe_restore(hid)
        else:  # 延期
            days = SEVERE_RECHECK_DAYS if hz["level"] == "严重" else GENERAL_RECHECK_DAYS
            hz["deadline"] = d + timedelta(days=days)
            hz["overdue"] = False
            self.report("复查",
                f"{d}：户 {hid}（{hh['name']}）{hz['level']}隐患「{hname}」整改延期，"
                f"到期复查期限 {hz['deadline']}（{hz['level']}隐患 {days} 天）")

    # ---------- 入口 ----------
    def process(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            tokens = line.split()
            cmd = tokens[0]
            if cmd == "户":
                self.define_household(tokens, lineno)
            elif cmd == "类型":
                self.define_hazard_type(tokens, lineno)
            elif cmd == "安检":
                self.do_inspection(tokens, lineno)
            elif cmd == "整改":
                self.do_rectification(tokens, lineno)
            else:
                self.report("错误", f"第{lineno}行：无法识别的指令 {cmd!r}，已跳过")
        if self.max_date is not None:
            self.check_overdue(self.max_date)

    # ---------- 输出 ----------
    def household_status(self, hid):
        hh = self.households[hid]
        open_hz = [(n, h) for (h_id, n), h in self.open.items() if h_id == hid]
        if hh["suspended"]:
            status = "停供"
        elif open_hz:
            status = "待整改"
        else:
            status = "正常"
        return status, open_hz

    def render(self):
        out = []
        out.append("=" * 60)
        out.append("一、安检状态")
        out.append("=" * 60)
        for hid, hh in self.households.items():
            status, open_hz = self.household_status(hid)
            out.append(f"户号 {hid}  {hh['name']}  {hh['addr']}")
            out.append(f"  状态：{status}    上次安检：{hh['last_check']}")
            if open_hz:
                sev = sum(1 for _, h in open_hz if h["level"] == "严重")
                gen = len(open_hz) - sev
                out.append(f"  在册隐患 {len(open_hz)} 项（严重 {sev} / 一般 {gen}）：")
                for name, h in sorted(open_hz, key=lambda x: (x[1]["level"] != "严重", x[0])):
                    dl = str(h["deadline"]) if h["deadline"] else "未延期"
                    flag = "【已到期】" if h["overdue"] else ""
                    out.append(f"    - [{h['level']}] {name}  发现 {h['found']}  "
                               f"复查期限 {dl} {flag}")
            else:
                out.append("  在册隐患：无")
        out.append("")
        out.append("=" * 60)
        out.append("二、错误与事件报告")
        out.append("=" * 60)
        if not self.reports:
            out.append("（无）")
        for i, (kind, msg) in enumerate(self.reports, 1):
            out.append(f"{i:2d}. [{kind}] {msg}")
        errors = sum(1 for k, _ in self.reports if k == "错误")
        out.append("")
        out.append(f"合计：错误 {errors} 条，事件/警示 {len(self.reports) - errors} 条；"
                   f"在册隐患 {len(self.open)} 项；"
                   f"停供 {sum(1 for h in self.households.values() if h['suspended'])} 户")
        return "\n".join(out)


def main(argv):
    if "--demo" in argv:
        text = DEMO_INPUT
        print("【输入】")
        print(text)
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    engine = Engine()
    engine.process(text)
    print("【输出】")
    print(engine.render())
    return 1 if any(k == "错误" for k, _ in engine.reports) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
