#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
变电站保护故障重放与整定联动模拟工具（单文件、纯 Python 标准库）。

输入为按行处理的指令流（# 后为注释，支持中文/英文关键字）：
    保护  <名称> <所在线路> <主|后备> <动作时间秒>     (PROT ...)
    整定  <保护名称> <新动作时间秒>                    (SET ...)
    故障  <线路> <序号>                               (FAULT ...)
    拒动  <故障序号> <保护名称>                        (REFUSE ...)

语义：
  * 状态跨指令流延续：故障注入后保持“未结束”，其动作链按当前整定实时重算；
  * 动作链 = 本线路主保护按动作时间升序，再接后备保护按动作时间升序；
  * 故障结算（输入结束）时，链上第一个未拒动的保护动作；全部拒动则越级；
  * 每次整定后重算同线路“后备(上级) - 主(下级)”时间级差，不足即报告；
  * 整定/拒动引用不存在的对象、故障重复注入等均记入错误清单。

用法：
    python protection_replay.py 输入文件     # 处理指令流文件
    python protection_replay.py              # 运行内置样例
    python protection_replay.py --selftest   # 运行自测
"""

import sys

GRADE_MIN = 0.3  # 上下级最小时间级差（秒）

MAIN = "主"
BACKUP = "后备"

KINDS = {"主": MAIN, "M": MAIN, "MAIN": MAIN,
         "后备": BACKUP, "B": BACKUP, "BACKUP": BACKUP}

COMMANDS = {"保护": "PROT", "PROT": "PROT",
            "整定": "SET", "SET": "SET",
            "故障": "FAULT", "FAULT": "FAULT",
            "拒动": "REFUSE", "REFUSE": "REFUSE"}


class Protection:
    __slots__ = ("name", "line", "kind", "time")

    def __init__(self, name, line, kind, time):
        self.name = name
        self.line = line
        self.kind = kind
        self.time = time


class Fault:
    __slots__ = ("seq", "line", "refused", "open")

    def __init__(self, seq, line):
        self.seq = seq
        self.line = line
        self.refused = set()
        self.open = True


class Simulator:
    def __init__(self):
        self.protections = {}      # 名称 -> Protection
        self.faults = {}           # 序号 -> Fault
        self.open_seqs = []        # 未结束故障序号（注入顺序）
        self.results = []          # 动作结果 / 越级报告（按发生顺序）
        self.errors = []           # 错误清单
        self._violations = set()   # 当前仍处于级差不足的 (上级, 下级) 对，用于去重

    # ---- 动作链：主保护按时间升序，再后备按时间升序（按当前整定实时计算）----
    def chain_of(self, line):
        prots = [p for p in self.protections.values() if p.line == line]
        mains = sorted((p for p in prots if p.kind == MAIN),
                       key=lambda p: (p.time, p.name))
        backs = sorted((p for p in prots if p.kind == BACKUP),
                       key=lambda p: (p.time, p.name))
        return mains + backs

    # ---- 指令：保护定义 ----
    def cmd_prot(self, name, line, kind, time):
        if name in self.protections:
            self.errors.append("保护重复定义: %s" % name)
            return
        self.protections[name] = Protection(name, line, kind, time)

    # ---- 指令：整定（改动作时间并重算级差）----
    def _settle_no_refusal(self):
        # 无拒动记录的未结束故障视为主保护已正常动作，按当前整定立即结算，
        # 避免后续整定改写历史故障；带拒动的级联故障继续保持未结束。
        for seq in list(self.open_seqs):
            fault = self.faults[seq]
            if not fault.refused:
                chain = self.chain_of(fault.line)
                actor = chain[0] if chain else None
                fault.open = False
                self.open_seqs.remove(seq)
                if actor is None:
                    self._escalate(fault, chain)
                else:
                    self.results.append(
                        "故障#%d 线路%s: %s保护 %s 动作, 动作时间 %gs"
                        % (seq, fault.line, actor.kind, actor.name, actor.time))

    def cmd_set(self, name, new_time):
        self._settle_no_refusal()
        prot = self.protections.get(name)
        if prot is None:
            self.errors.append("整定引用不存在的保护: %s" % name)
            return
        prot.time = new_time
        self._recheck_grades()

    def _recheck_grades(self):
        lines = {p.line for p in self.protections.values()}
        current = set()
        for line in sorted(lines):
            prots = [p for p in self.protections.values() if p.line == line]
            mains = sorted((p for p in prots if p.kind == MAIN),
                           key=lambda p: (p.time, p.name))
            backs = sorted((p for p in prots if p.kind == BACKUP),
                           key=lambda p: (p.time, p.name))
            for upper in backs:      # 上级 = 后备
                for lower in mains:  # 下级 = 主
                    diff = round(upper.time - lower.time, 9)
                    if diff < GRADE_MIN - 1e-9:
                        current.add((upper.name, lower.name, diff))
        for upper, lower, diff in sorted(current):
            if (upper, lower) not in self._violations:
                self.errors.append(
                    "级差不足: 上级%s 下级%s 差值%.3gs (要求级差>=%gs)"
                    % (upper, lower, diff, GRADE_MIN))
        self._violations = {(u, l) for u, l, _ in current}

    # ---- 指令：故障注入 ----
    def cmd_fault(self, line, seq):
        if seq in self.faults:
            self.errors.append("故障重复注入: 序号%d" % seq)
            return
        self._settle_no_refusal()
        fault = Fault(seq, line)
        self.faults[seq] = fault
        self.open_seqs.append(seq)  # 线路暂无保护也保持打开，结束时按越级结算

    # ---- 指令：拒动 ----
    def cmd_refuse(self, seq, name):
        fault = self.faults.get(seq)
        if fault is None:
            self.errors.append("拒动引用不存在的故障: 序号%d" % seq)
            return
        if name not in self.protections:
            self.errors.append("拒动引用不存在的保护: %s" % name)
            return
        if not fault.open:
            self.errors.append("拒动引用已结束的故障: 序号%d" % seq)
            return
        chain = self.chain_of(fault.line)
        if name not in [p.name for p in chain]:
            self.errors.append("保护%s不在故障#%d(线路%s)的动作链中"
                               % (name, seq, fault.line))
            return
        if name in fault.refused:
            self.errors.append("重复拒动: 故障#%d 保护%s" % (seq, name))
            return
        fault.refused.add(name)
        chain = self.chain_of(fault.line)  # 按当前整定重算动作链
        if all(p.name in fault.refused for p in chain):
            self._escalate(fault, chain)   # 全部保护拒动 -> 越级

    def _escalate(self, fault, chain):
        fault.open = False
        if fault.seq in self.open_seqs:
            self.open_seqs.remove(fault.seq)
        chain_str = "->".join(p.name for p in chain) or "(空)"
        self.results.append("越级: 线路%s 动作链 %s 全部保护拒动"
                            % (fault.line, chain_str))

    # ---- 输入结束：结算所有未结束故障（动作链按最终整定级联重算）----
    def finish(self):
        for seq in list(self.open_seqs):
            fault = self.faults[seq]
            chain = self.chain_of(fault.line)
            actor = next((p for p in chain if p.name not in fault.refused),
                         None)
            fault.open = False
            self.open_seqs.remove(seq)
            if actor is None:
                self._escalate(fault, chain)
            else:
                self.results.append(
                    "故障#%d 线路%s: %s保护 %s 动作, 动作时间 %gs"
                    % (seq, fault.line, actor.kind, actor.name, actor.time))

    def render(self):
        out = ["=== 保护动作结果 ==="]
        out.extend(self.results or ["(无)"])
        out.append("")
        out.append("=== 错误报告 ===")
        if self.errors:
            out.extend("%d. %s" % (i, e) for i, e in
                       enumerate(self.errors, 1))
        else:
            out.append("(无)")
        return "\n".join(out)


def run_text(text):
    sim = Simulator()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        cmd = COMMANDS.get(tokens[0].upper(), COMMANDS.get(tokens[0]))
        try:
            if cmd == "PROT" and len(tokens) == 5:
                kind = KINDS.get(tokens[3].upper(), KINDS.get(tokens[3]))
                if kind is None:
                    raise ValueError("未知保护类型 %r" % tokens[3])
                sim.cmd_prot(tokens[1], tokens[2], kind, float(tokens[4]))
            elif cmd == "SET" and len(tokens) == 3:
                sim.cmd_set(tokens[1], float(tokens[2]))
            elif cmd == "FAULT" and len(tokens) == 3:
                sim.cmd_fault(tokens[1], int(tokens[2]))
            elif cmd == "REFUSE" and len(tokens) == 3:
                sim.cmd_refuse(int(tokens[1]), tokens[2])
            else:
                raise ValueError("无法识别的指令")
        except ValueError as exc:
            sim.errors.append("第%d行解析错误: %s (%s)"
                              % (lineno, exc, raw.strip()))
    sim.finish()
    return sim


DEMO = """
# ---- 保护定义: 名称 线路 类型 动作时间 ----
保护 M1 L1 主 0.10
保护 M2 L1 主 0.20
保护 B1 L1 后备 0.50
保护 M3 L2 主 0.15
保护 B2 L2 后备 0.60

# ---- 故障1: 无拒动, 动作时间最短的主保护 M1 动作 ----
故障 L1 1

# ---- 故障2: M1/M2 拒动, 后备 B1 级联动作 ----
故障 L1 2
拒动 2 M1
拒动 2 M2

# ---- 故障3: 全部保护拒动 -> 越级 ----
故障 L2 3
拒动 3 M3
拒动 3 B2
拒动 3 M3          # 故障已结束

# ---- 整定: B1 改为 0.25, 与 M1/M2 级差不足; 引用不存在的保护 ----
整定 B1 0.25
整定 NOPE 1.0

# ---- 各类引用错误 ----
故障 L1 2          # 重复注入
拒动 99 M1         # 不存在的故障
拒动 1 NOPE        # 不存在的保护
拒动 2 M3          # 不在本线路动作链中

# ---- 故障4: 注入后整定变更, 未结束故障动作链级联重算 ----
故障 L1 4
拒动 4 M1
整定 M2 0.05       # M2 变为最快未拒动主保护
"""


def selftest():
    sim = run_text(DEMO)
    out = sim.render()
    print(out)
    checks = [
        # 故障1: 最短动作时间主保护动作
        "故障#1 线路L1: 主保护 M1 动作, 动作时间 0.1s",
        # 故障2: 主保护拒动 -> 后备级联动作（时间为整定后的 0.25）
        "故障#2 线路L1: 后备保护 B1 动作, 动作时间 0.25s",
        # 故障3: 全部拒动 -> 越级报告（线路 + 动作链）
        "越级: 线路L2 动作链 M3->B2 全部保护拒动",
        # 故障4: 整定变更后未结束故障动作链重算, M2(0.05) 动作
        "故障#4 线路L1: 主保护 M2 动作, 动作时间 0.05s",
        # 级差不足报告（上级、下级、差值）
        "级差不足: 上级B1 下级M1 差值0.15s",
        "级差不足: 上级B1 下级M2 差值0.05s",
        # 引用与重复类错误
        "整定引用不存在的保护: NOPE",
        "故障重复注入: 序号2",
        "拒动引用不存在的故障: 序号99",
        "拒动引用不存在的保护: NOPE",
        "拒动引用已结束的故障: 序号3",
        "保护M3不在故障#2(线路L1)的动作链中",
    ]
    for expected in checks:
        assert expected in out, "缺少输出: %s" % expected

    # 专项: 线路上无保护 -> 立即越级
    sim2 = run_text("故障 LX 7\n")
    assert "越级: 线路LX 动作链 (空) 全部保护拒动" in sim2.render()

    # 专项: 级差恢复后再次不足会重新报告
    sim3 = run_text(
        "保护 A L 主 0.1\n保护 B L 后备 0.5\n"
        "整定 B 0.2\n整定 B 0.5\n整定 B 0.2\n")
    assert sim3.errors.count(
        "级差不足: 上级B 下级A 差值0.1s (要求级差>=0.3s)") == 2

    # 专项: 跨流状态延续——故障注入后再定义的保护进入动作链
    sim4 = run_text("故障 L 5\n保护 P1 L 主 0.1\n保护 P2 L 后备 0.6\n"
                    "拒动 5 P1\n")
    assert "故障#5 线路L: 后备保护 P2 动作, 动作时间 0.6s" in sim4.render()

    print("\n自测全部通过 ✔")


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    else:
        print("(未指定输入文件，运行内置样例；"
              "用法: python protection_replay.py <输入文件> | --selftest)\n")
        text = DEMO
    print(run_text(text).render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
