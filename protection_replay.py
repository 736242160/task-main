#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变电站保护故障重放工具(纯 Python 标准库, 单文件)。

用法:
    python3 protection_replay.py 输入文件 [--grade 0.3]
    python3 protection_replay.py --demo        # 跑内置样例并输出完整报告
    python3 protection_replay.py --selftest    # 跑内置自测(断言全部需求点)

输入格式(四个流以节区分, 节可交错重复出现, 状态跨流延续):
    [protection]  名称 所在线路 类型(主|后备) 动作时间(秒)
    [setting]     保护名称 新动作时间(秒)
    [fault]       线路 故障序号
    [refusal]     故障序号 保护名称
    # 井号开头为注释, 空行忽略

语义约定:
    * 故障注入后不立即结算; 其后的拒动、整定持续作用于未结束故障。
    * 一条线路的全部保护均被拒动时, 该故障立即以"越级"结束,
      越级报告记录当时的动作链快照(线路、动作链)。
    * 输入结束时, 未结束故障按当时动作时间升序构成动作链结算:
      链上第一个未被拒动的保护动作; 全部被拒动则越级。
    * 整定变更后, 受影响线路上所有未结束故障的动作链立即级联重算并记录。
    * 每次保护定义/整定后, 重算该线路相邻上下级时间级差,
      差值小于 --grade(默认 0.3s) 即报告(上级、下级、差值)。
"""

import argparse
import sys
from dataclasses import dataclass

SECTIONS = ("protection", "setting", "fault", "refusal")
VALID_KINDS = ("主", "后备")
EPS = 1e-9


@dataclass
class Protection:
    name: str
    line: str
    kind: str
    delay: float


@dataclass
class Fault:
    seq: str
    line: str
    settled: bool = False
    result: tuple = None  # ("act", 保护名, 动作时间, 链快照) 或 ("trip", 链快照)


def snapshot(chain):
    return [(p.name, p.kind, p.delay) for p in chain]


def fmt_chain(snap, refused):
    if not snap:
        return "(线路上无保护)"
    parts = []
    for name, kind, delay in snap:
        s = "%s(%s,%.2fs)" % (name, kind, delay)
        if name in refused:
            s += "[拒动]"
        parts.append(s)
    return " -> ".join(parts)


class Engine:
    """事件流引擎: 四个流共用同一状态机, 跨流状态延续。"""

    def __init__(self, grade=0.3):
        self.grade = grade
        self.protections = {}      # 保护名 -> Protection
        self.faults = {}           # 故障序号 -> Fault
        self.fault_order = []      # 注入顺序
        self.refusals = {}         # 故障序号 -> {拒动保护名}
        self.errors = []
        self.recalc_log = []       # 整定后未结束故障的级联重算记录
        self._err_seen = set()

    def error(self, msg):
        if msg not in self._err_seen:
            self._err_seen.add(msg)
            self.errors.append(msg)

    def line_chain(self, line):
        """线路动作链: 按动作时间升序(同时间按名称), 主/后备统一排队。"""
        prots = [p for p in self.protections.values() if p.line == line]
        return sorted(prots, key=lambda p: (p.delay, p.name))

    # ---- 级差 ----
    def check_grade(self, line, trigger):
        chain = self.line_chain(line)
        for lower, upper in zip(chain, chain[1:]):
            diff = upper.delay - lower.delay
            if diff < self.grade - EPS:
                self.error("[级差不足][%s] 线路%s: 上级%s 下级%s 差值%.2fs < %.2fs"
                           % (trigger, line, upper.name, lower.name, diff, self.grade))

    # ---- 保护定义流 ----
    def define(self, name, line, kind, delay):
        if name in self.protections:
            self.error("[保护] 重复定义: %s" % name)
            return
        if kind not in VALID_KINDS:
            self.error("[保护] %s 类型非法: %s (须为 主/后备)" % (name, kind))
            return
        if delay < 0:
            self.error("[保护] %s 动作时间不能为负: %s" % (name, delay))
            return
        self.protections[name] = Protection(name, line, kind, delay)
        self.check_grade(line, "保护定义")

    # ---- 整定流 ----
    def set_time(self, name, delay):
        p = self.protections.get(name)
        if p is None:
            self.error("[整定] 保护%s不存在" % name)
            return
        if delay < 0:
            self.error("[整定] 保护%s动作时间不能为负: %s" % (name, delay))
            return
        p.delay = delay
        self.check_grade(p.line, "整定后")          # 整定后上下级级差重算
        self.recalc_open_faults(p.line)             # 未结束故障动作链级联重算

    def recalc_open_faults(self, line):
        for seq in self.fault_order:
            f = self.faults[seq]
            if f.line != line or f.settled:
                continue
            chain = self.line_chain(line)
            refused = self.refusals.get(seq, set())
            pend = next((p for p in chain if p.name not in refused), None)
            target = ("待动作 %s(%.2fs)" % (pend.name, pend.delay)) if pend \
                else "全部被拒动"
            self.recalc_log.append("故障%s 线路%s 动作链重算: %s => %s"
                                   % (seq, line, fmt_chain(snapshot(chain), refused), target))

    # ---- 故障流 ----
    def fault(self, line, seq):
        if seq in self.faults:
            self.error("[故障] 重复注入: 序号%s (线路%s)" % (seq, line))
            return
        f = Fault(seq=seq, line=line)
        self.faults[seq] = f
        self.fault_order.append(seq)
        self.maybe_settle(f)  # 拒动可能已先行声明

    # ---- 拒动流 ----
    def refusal(self, seq, pname):
        if pname not in self.protections:
            self.error("[拒动] 保护%s不存在 (故障%s)" % (pname, seq))
            return
        self.refusals.setdefault(seq, set()).add(pname)
        f = self.faults.get(seq)
        if f is not None:
            self.maybe_settle(f)
        # 故障尚未注入的拒动先挂起, 结束时统一校验故障是否存在

    def maybe_settle(self, f):
        """线路全部保护被拒动 => 立即越级结束, 记录动作链快照。"""
        if f.settled:
            return
        chain = self.line_chain(f.line)
        refused = self.refusals.get(f.seq, set())
        if chain and all(p.name in refused for p in chain):
            f.settled = True
            f.result = ("trip", snapshot(chain))

    # ---- 输入结束: 校验 + 结算未结束故障 ----
    def finish(self):
        for seq in self.refusals:
            if seq not in self.faults:
                self.error("[拒动] 故障%s不存在" % seq)
        for seq in self.fault_order:
            f = self.faults[seq]
            if f.settled:
                continue
            chain = self.line_chain(f.line)
            refused = self.refusals.get(seq, set())
            actor = next((p for p in chain if p.name not in refused), None)
            if actor is not None:
                f.result = ("act", actor.name, actor.delay, snapshot(chain))
            else:
                f.result = ("trip", snapshot(chain))
            f.settled = True

    def render(self):
        out = ["===== 保护动作结果 ====="]
        for seq in self.fault_order:
            f = self.faults[seq]
            refused = self.refusals.get(seq, set())
            res = f.result
            if res[0] == "act":
                _, name, delay, snap = res
                out.append("故障%s 线路%s: %s" % (seq, f.line, fmt_chain(snap, refused)))
                out.append("  结果: 保护 %s 动作, 动作时间 %.2fs" % (name, delay))
            else:
                _, snap = res
                out.append("故障%s 线路%s: 越级! 全部保护拒动" % (seq, f.line))
                out.append("  越级报告: 线路%s 动作链: %s"
                           % (f.line, fmt_chain(snap, refused)))
        out.append("")
        out.append("===== 整定后未结束故障动作链级联重算 =====")
        out.extend(self.recalc_log if self.recalc_log else ["(无)"])
        out.append("")
        out.append("===== 错误清单 =====")
        if self.errors:
            for i, e in enumerate(self.errors, 1):
                out.append("%d. %s" % (i, e))
        else:
            out.append("(无错误)")
        return "\n".join(out)


def parse_and_run(text, grade=0.3):
    eng = Engine(grade)
    section = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in SECTIONS:
                eng.error("[输入] 第%d行: 未知节 [%s]" % (lineno, section))
                section = None
            continue
        parts = line.split()
        try:
            if section == "protection" and len(parts) == 4:
                eng.define(parts[0], parts[1], parts[2], float(parts[3]))
            elif section == "setting" and len(parts) == 2:
                eng.set_time(parts[0], float(parts[1]))
            elif section == "fault" and len(parts) == 2:
                eng.fault(parts[0], parts[1])
            elif section == "refusal" and len(parts) == 2:
                eng.refusal(parts[0], parts[1])
            elif section is None:
                eng.error("[输入] 第%d行: 内容不在任何节内: %s" % (lineno, raw.strip()))
            else:
                eng.error("[输入] 第%d行: 字段个数错误: %s" % (lineno, raw.strip()))
        except ValueError:
            eng.error("[输入] 第%d行: 数字格式错误: %s" % (lineno, raw.strip()))
    eng.finish()
    return eng


SAMPLE = """\
# ===== 自测样例: 覆盖全部需求点 =====
[protection]
M1 L1 主 0.2
B1 L1 后备 0.6
M2 L2 主 0.3
B2 L2 后备 0.5
M3 L3 主 0.1
B3 L3 后备 0.4

[setting]
B1 0.45
NOPE 1.0

[fault]
L3 F5
L1 F1
L2 F2
L1 F3
L1 F1

[refusal]
F2 M2
F3 M1
F3 B1
F9 M1
F2 NOPE

[setting]
M1 0.7
"""


def run_selftest():
    eng = parse_and_run(SAMPLE, grade=0.3)
    n = 0

    def check(cond, label):
        nonlocal n
        assert cond, "自测失败: " + label
        n += 1

    # 1. 主保护动作(动作时间最短): F5 无拒动 -> M3 @0.10s
    r = eng.faults["F5"].result
    check(r[0] == "act" and r[1] == "M3" and abs(r[2] - 0.1) < EPS, "主保护最短时限动作")
    # 2. 主保护拒动 -> 后备级联: F2 拒 M2 -> B2 @0.50s
    r = eng.faults["F2"].result
    check(r[0] == "act" and r[1] == "B2" and abs(r[2] - 0.5) < EPS, "主拒动后备级联")
    # 3. 全部拒动 -> 越级, 动作链快照为结算时的 M1(0.20)->B1(0.45)
    r = eng.faults["F3"].result
    check(r[0] == "trip", "全部拒动越级")
    check([c[0] for c in r[1]] == ["M1", "B1"], "越级动作链顺序")
    snap = {c[0]: c[2] for c in r[1]}
    check(abs(snap["M1"] - 0.2) < EPS and abs(snap["B1"] - 0.45) < EPS,
          "越级链快照不受后续整定影响")
    # 4. 整定变更后未结束故障级联重算: M1->0.7 后 F1 待动作变为 B1 @0.45s
    r = eng.faults["F1"].result
    check(r[0] == "act" and r[1] == "B1" and abs(r[2] - 0.45) < EPS, "整定后动作链重算")
    check(any("F1" in s and "B1" in s for s in eng.recalc_log), "重算记录包含F1")
    check(not any("F3" in s for s in eng.recalc_log), "已越级故障不再重算")
    # 5. 级差: L2 定义时 0.20 不足; L1 整定 B1->0.45 后 0.25 不足; M1->0.7 后仍不足
    errs = "\n".join(eng.errors)
    check("上级B2 下级M2 差值0.20" in errs, "定义时级差不足报告")
    check("上级B1 下级M1 差值0.25" in errs, "整定后级差重算报告")
    check("上级M1 下级B1 差值0.25" in errs, "二次整定级差重算报告")
    # 6. 整定引用不存在的保护
    check("[整定] 保护NOPE不存在" in errs, "整定引用不存在保护")
    # 7. 故障重复注入
    check("重复注入: 序号F1" in errs, "故障重复注入")
    # 8. 拒动引用不存在的故障 / 保护
    check("[拒动] 故障F9不存在" in errs, "拒动引用不存在故障")
    check("[拒动] 保护NOPE不存在" in errs, "拒动引用不存在保护")
    print("自测全部通过 (%d 项断言)" % n)
    print()
    print(eng.render())


def main(argv=None):
    ap = argparse.ArgumentParser(description="变电站保护故障重放工具(纯标准库单文件)")
    ap.add_argument("file", nargs="?", help="输入文件; 用 - 表示标准输入")
    ap.add_argument("--grade", type=float, default=0.3, help="上下级时间级差阈值(秒), 默认 0.3")
    ap.add_argument("--demo", action="store_true", help="运行内置样例并输出报告")
    ap.add_argument("--selftest", action="store_true", help="运行内置自测")
    args = ap.parse_args(argv)

    if args.selftest:
        run_selftest()
        return 0
    if args.demo:
        print(parse_and_run(SAMPLE, args.grade).render())
        return 0
    if args.file:
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        print(parse_and_run(text, args.grade).render())
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
