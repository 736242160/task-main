#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港口岸桥装卸作业工具（纯 Python 标准库，单文件）

用法：
    python3 quay_crane.py 输入文件        # 从文件读取
    python3 quay_crane.py                 # 从标准输入读取
    python3 quay_crane.py --demo          # 运行内置示例

================ 输入格式（行式文本，# 后为注释） ================
岸桥   <名称> <轨道> <吊具类型:标准|冷藏>
集装箱 <编号> <尺寸> <类型:普通|冷藏> [重量吨]      # 重量可省略，按尺寸取默认值
作业   <岸桥名> <集装箱编号> <装|卸>
故障   <岸桥名>                                   # 岸桥发生故障
修复   <岸桥名>                                   # 岸桥修复
推进   <岸桥名|全部>                              # 完成当前作业并启动排队作业

================ 自定规则（及理由） ================
1. 吊具匹配：冷藏箱必须由冷藏吊具作业；标准吊具吊冷藏箱 -> 报错。
   冷藏吊具向下兼容普通箱（冷藏吊具具备标准扭锁，可吊普通箱）。
2. 轨道冲突：同一轨道上任一时刻只允许一台岸桥处于“执行中”。
   理由：同轨岸桥共用一条轨道、无法互相穿越，同时作业作业区域必然
   重叠、存在碰撞风险，故后到作业顺延（排队等待）并报告冲突。
3. 额定载荷（自定）：标准吊具 65t，冷藏吊具 41t；超重 -> 报错。
   箱重缺省值：20尺=24t，40尺=30.5t，45尺=32.5t，可在箱定义第4列覆盖。
4. 重复作业：同一集装箱已有进行中作业，或同方向作业已完成过 -> 报错。
5. 故障联动：岸桥故障时，其“执行中 + 排队”的未完成作业按顺序级联
   重新分配给 健康、吊具匹配、不超重 的其他岸桥（优先负载最轻者）；
   无可用岸桥则作业失败并报告。故障期间该桥不接受新作业 -> 报错。
6. 状态延续：集装箱位置（堆场/船上）、岸桥忙闲与故障状态跨作业延续；
   装船要求箱在堆场，卸船要求箱在船上，否则报错。

================ 输入输出示例 ================
输入（另存为 input.txt，或直接运行 --demo）：
----------------------------------------------
岸桥 QC1 T1 标准
岸桥 QC2 T1 冷藏
岸桥 QC3 T2 冷藏
集装箱 C001 40 普通
集装箱 C002 40 冷藏
集装箱 C003 20 普通 70
集装箱 C004 20 冷藏
集装箱 C005 40 普通
集装箱 C006 40 冷藏
作业 QC1 C001 装      # 正常，QC1 开始执行
作业 QC2 C002 装      # 与 QC1 同轨 T1 -> 轨道冲突，顺延
作业 QC1 C003 装      # 70t 超 QC1 额定 65t -> 超重
作业 QC2 C004 装      # 排队
故障 QC2              # C002/C004 级联重新分配给 QC3
作业 QC2 C005 装      # 故障期间不接受新作业
作业 QC9 C005 装      # 引用不存在的岸桥
作业 QC1 C099 装      # 引用不存在的集装箱
推进 QC1
作业 QC1 C001 装      # 重复作业
作业 QC1 C006 装      # 标准吊具吊冷藏箱
推进 QC3
推进 QC3
作业 QC3 C002 卸      # C002 已在船上，正常卸船
修复 QC2
作业 QC2 C006 装
作业 QC1 C005 卸      # C005 从未装船 -> 执行时报状态错误
----------------------------------------------
输出（python3 quay_crane.py --demo 的实测结果）：
----------------------------------------------
========== 作业结果 ==========
#01 [成功] 作业 QC1 C001 装
#02 [成功] 作业 QC2 C002 装（岸桥 QC2 故障，级联重新分配 QC2 -> QC3）
#03 [失败] 作业 QC1 C003 装（超重：箱 C003 重 70t，超岸桥 QC1 额定载荷 65t）
#04 [成功] 作业 QC2 C004 装（岸桥 QC2 故障，级联重新分配 QC2 -> QC3）
#05 [失败] 作业 QC2 C005 装（岸桥 QC2 故障期间不接受新作业（C005 装））
#06 [失败] 作业 QC9 C005 装（作业引用不存在的岸桥 QC9）
#07 [失败] 作业 QC1 C099 装（作业引用不存在的集装箱 C099）
#08 [失败] 作业 QC1 C001 装（重复作业同一集装箱 C001（装））
#09 [失败] 作业 QC1 C006 装（吊具不匹配：QC1 为标准吊具，不能吊冷藏箱 C006）
#10 [成功] 作业 QC3 C002 卸
#11 [成功] 作业 QC2 C006 装
#12 [失败] 作业 QC1 C005 卸（集装箱 C005 不在船上（位于堆场），无法卸船）

========== 错误报告 ==========
E1. #2 轨道冲突：岸桥 QC2 与 QC1 同处轨道 T1，作业区域重叠，#2 顺延等待
E2. #3 超重：箱 C003 重 70t，超岸桥 QC1 额定载荷 65t
E3. #5 岸桥 QC2 故障期间不接受新作业（C005 装）
E4. #6 作业引用不存在的岸桥 QC9
E5. #7 作业引用不存在的集装箱 C099
E6. #8 重复作业同一集装箱 C001（装）
E7. #9 吊具不匹配：QC1 为标准吊具，不能吊冷藏箱 C006
E8. #12 轨道冲突：岸桥 QC1 与 QC2 同处轨道 T1，作业区域重叠，#12 顺延等待
E9. #12 集装箱 C005 不在船上（位于堆场），无法卸船

========== 最终状态 ==========
岸桥 QC1: 轨道T1 标准吊具 正常 空闲
岸桥 QC2: 轨道T1 冷藏吊具 正常 空闲
岸桥 QC3: 轨道T2 冷藏吊具 正常 空闲
集装箱 C001: 40尺 普通 30.5t 位于船上
集装箱 C002: 40尺 冷藏 30.5t 位于堆场
集装箱 C003: 20尺 普通 70t 位于堆场
集装箱 C004: 20尺 冷藏 24t 位于船上
集装箱 C005: 40尺 普通 30.5t 位于堆场
集装箱 C006: 40尺 冷藏 30.5t 位于船上
----------------------------------------------
"""

import sys
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------- 自定参数 ----------------
RATED_LOAD = {"标准": 65.0, "冷藏": 41.0}          # 各吊具额定载荷(t)
DEFAULT_WEIGHT = {"20": 24.0, "40": 30.5, "45": 32.5}  # 按尺寸的默认箱重(t)
VALID_SPREADER = ("标准", "冷藏")
VALID_CTYPE = ("普通", "冷藏")
VALID_ACTION = ("装", "卸")


@dataclass
class Operation:
    seq: int
    crane: str
    cid: str
    action: str
    status: str = "等待"          # 等待 / 执行中 / 成功 / 失败
    note: str = ""
    conflict_reported: bool = False


@dataclass
class Crane:
    name: str
    track: str
    spreader: str
    faulty: bool = False
    current: Optional[Operation] = None
    queue: List[Operation] = field(default_factory=list)


@dataclass
class Container:
    cid: str
    size: str
    ctype: str
    weight: float
    location: str = "堆场"        # 堆场 / 船上
    done: set = field(default_factory=set)   # 已完成的作业方向
    pending: bool = False                     # 是否有进行中的作业


class Engine:
    def __init__(self):
        self.cranes = {}
        self.containers = {}
        self.ops: List[Operation] = []
        self.errors: List[str] = []
        self._seq = 0

    # ---------- 基础工具 ----------
    def error(self, msg):
        self.errors.append(msg)

    def fail(self, op, msg):
        op.status = "失败"
        op.note = msg
        self.error(f"#{op.seq} {msg}")

    @staticmethod
    def spreader_ok(spreader, ctype):
        # 冷藏箱必须冷藏吊具；冷藏吊具兼容普通箱
        return ctype == "普通" or spreader == "冷藏"

    # ---------- 定义 ----------
    def add_crane(self, name, track, spreader):
        if name in self.cranes:
            self.error(f"岸桥 {name} 重复定义")
        elif spreader not in VALID_SPREADER:
            self.error(f"岸桥 {name}: 非法吊具类型 {spreader}")
        else:
            self.cranes[name] = Crane(name, track, spreader)

    def add_container(self, cid, size, ctype, weight=None):
        if cid in self.containers:
            self.error(f"集装箱 {cid} 重复定义")
            return
        if ctype not in VALID_CTYPE:
            self.error(f"集装箱 {cid}: 非法类型 {ctype}")
            return
        if weight is None:
            key = "".join(ch for ch in size if ch.isdigit())
            weight = DEFAULT_WEIGHT.get(key, 30.0)
        self.containers[cid] = Container(cid, size, ctype, float(weight))

    # ---------- 作业下达 ----------
    def new_op(self, crane_name, cid, action):
        self._seq += 1
        op = Operation(self._seq, crane_name, cid, action)
        self.ops.append(op)
        crane = self.cranes.get(crane_name)
        if crane is None:
            self.fail(op, f"作业引用不存在的岸桥 {crane_name}")
            return
        box = self.containers.get(cid)
        if box is None:
            self.fail(op, f"作业引用不存在的集装箱 {cid}")
            return
        if action not in VALID_ACTION:
            self.fail(op, f"非法作业类型 {action}（应为 装/卸）")
            return
        if box.pending or action in box.done:
            self.fail(op, f"重复作业同一集装箱 {cid}（{action}）")
            return
        if crane.faulty:
            self.fail(op, f"岸桥 {crane_name} 故障期间不接受新作业（{cid} {action}）")
            return
        if not self.spreader_ok(crane.spreader, box.ctype):
            self.fail(op, f"吊具不匹配：{crane_name} 为{crane.spreader}吊具，不能吊{box.ctype}箱 {cid}")
            return
        rated = RATED_LOAD[crane.spreader]
        if box.weight > rated:
            self.fail(op, f"超重：箱 {cid} 重 {box.weight:g}t，超岸桥 {crane_name} 额定载荷 {rated:g}t")
            return
        box.pending = True
        crane.queue.append(op)
        self.try_start(crane)

    # ---------- 调度 ----------
    def try_start(self, crane):
        """岸桥空闲时尝试启动队首作业；同轨冲突则顺延并报告。"""
        if crane.faulty or crane.current is not None or not crane.queue:
            return
        op = crane.queue[0]
        for other in self.cranes.values():
            if (other.name != crane.name and other.track == crane.track
                    and other.current is not None):
                if not op.conflict_reported:
                    op.conflict_reported = True
                    self.error(
                        f"#{op.seq} 轨道冲突：岸桥 {crane.name} 与 {other.name} 同处轨道 "
                        f"{crane.track}，作业区域重叠，#{op.seq} 顺延等待")
                return
        crane.queue.pop(0)
        crane.current = op
        op.status = "执行中"

    def complete_current(self, crane):
        op = crane.current
        if op is None:
            return
        box = self.containers[op.cid]
        if op.action == "装" and box.location != "堆场":
            self.fail(op, f"集装箱 {op.cid} 不在堆场（位于{box.location}），无法装船")
        elif op.action == "卸" and box.location != "船上":
            self.fail(op, f"集装箱 {op.cid} 不在船上（位于{box.location}），无法卸船")
        else:
            box.location = "船上" if op.action == "装" else "堆场"
            box.done.add(op.action)
            op.status = "成功"
        box.pending = False
        crane.current = None
        self.try_start(crane)

    def tick(self, target):
        names = list(self.cranes) if target == "全部" else [target]
        for name in names:
            crane = self.cranes.get(name)
            if crane is None:
                self.error(f"推进指令引用不存在的岸桥 {name}")
                continue
            if crane.faulty:
                continue
            if crane.current is not None:
                self.complete_current(crane)
            else:
                self.try_start(crane)

    # ---------- 故障联动 ----------
    def fault(self, name):
        crane = self.cranes.get(name)
        if crane is None:
            self.error(f"故障指令引用不存在的岸桥 {name}")
            return
        if crane.faulty:
            return
        crane.faulty = True
        unfinished = ([crane.current] if crane.current else []) + crane.queue
        crane.current = None
        crane.queue = []
        for op in unfinished:  # 级联重新分配
            target = self.find_reassign(op, exclude=name)
            box = self.containers[op.cid]
            if target is None:
                box.pending = False
                self.fail(op, f"岸桥 {name} 故障，箱 {op.cid} 无可用岸桥，作业取消")
            else:
                op.note = f"岸桥 {name} 故障，级联重新分配 {name} -> {target.name}"
                op.status = "等待"
                op.conflict_reported = False
                target.queue.append(op)
                self.try_start(target)

    def find_reassign(self, op, exclude):
        box = self.containers[op.cid]
        cands = []
        for c in self.cranes.values():
            if c.name == exclude or c.faulty:
                continue
            if not self.spreader_ok(c.spreader, box.ctype):
                continue
            if box.weight > RATED_LOAD[c.spreader]:
                continue
            load = len(c.queue) + (1 if c.current else 0)
            cands.append((load, c.name, c))
        if not cands:
            return None
        cands.sort(key=lambda t: (t[0], t[1]))
        return cands[0][2]

    def repair(self, name):
        crane = self.cranes.get(name)
        if crane is None:
            self.error(f"修复指令引用不存在的岸桥 {name}")
            return
        crane.faulty = False
        self.try_start(crane)

    # ---------- 收尾：排空可完成的作业 ----------
    def finish(self):
        moved = True
        while moved:
            moved = False
            for c in self.cranes.values():
                if c.faulty:
                    continue
                if c.current is not None:
                    self.complete_current(c)
                    moved = True
                elif c.queue:
                    self.try_start(c)
                    if c.current is not None:
                        moved = True

    # ---------- 输出 ----------
    def report(self):
        out = ["========== 作业结果 =========="]
        if not self.ops:
            out.append("（无作业）")
        for op in self.ops:
            tag = op.status if op.status in ("成功", "失败") else "未完成"
            line = f"#{op.seq:02d} [{tag}] 作业 {op.crane} {op.cid} {op.action}"
            if op.note:
                line += f"（{op.note}）"
            if tag == "未完成":
                line += f"―― 停留状态：{op.status}"
            out.append(line)
        out.append("")
        out.append("========== 错误报告 ==========")
        if not self.errors:
            out.append("（无错误）")
        for i, e in enumerate(self.errors, 1):
            out.append(f"E{i}. {e}")
        out.append("")
        out.append("========== 最终状态 ==========")
        for c in self.cranes.values():
            if c.current is not None:
                busy = f"执行中#{c.current.seq}"
            elif c.queue:
                busy = f"排队{len(c.queue)}项"
            else:
                busy = "空闲"
            out.append(f"岸桥 {c.name}: 轨道{c.track} {c.spreader}吊具 "
                       f"{'故障' if c.faulty else '正常'} {busy}")
        for b in self.containers.values():
            out.append(f"集装箱 {b.cid}: {b.size}尺 {b.ctype} {b.weight:g}t 位于{b.location}")
        return "\n".join(out)


def run(engine, lines):
    for ln, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, n = parts[0], len(parts)
        try:
            if cmd == "岸桥" and n == 4:
                engine.add_crane(parts[1], parts[2], parts[3])
            elif cmd == "集装箱" and n in (4, 5):
                w = float(parts[4]) if n == 5 else None
                engine.add_container(parts[1], parts[2], parts[3], w)
            elif cmd == "作业" and n == 4:
                engine.new_op(parts[1], parts[2], parts[3])
            elif cmd == "故障" and n == 2:
                engine.fault(parts[1])
            elif cmd == "修复" and n == 2:
                engine.repair(parts[1])
            elif cmd == "推进" and n == 2:
                engine.tick(parts[1])
            else:
                engine.error(f"第{ln}行无法解析: {raw.strip()}")
        except ValueError:
            engine.error(f"第{ln}行数值非法: {raw.strip()}")


DEMO_INPUT = """\
# ===== 岸桥定义：岸桥 <名称> <轨道> <吊具类型:标准|冷藏> =====
岸桥 QC1 T1 标准
岸桥 QC2 T1 冷藏
岸桥 QC3 T2 冷藏

# ===== 集装箱定义：集装箱 <编号> <尺寸> <类型:普通|冷藏> [重量吨] =====
集装箱 C001 40 普通
集装箱 C002 40 冷藏
集装箱 C003 20 普通 70
集装箱 C004 20 冷藏
集装箱 C005 40 普通
集装箱 C006 40 冷藏

# ===== 作业流 =====
作业 QC1 C001 装      # 正常，QC1 开始执行
作业 QC2 C002 装      # 与 QC1 同轨 T1 -> 轨道冲突，顺延
作业 QC1 C003 装      # 70t 超 QC1 额定 65t -> 超重报错
作业 QC2 C004 装      # 冷藏吊具吊冷藏箱，排队
故障 QC2              # C002/C004 级联重新分配给 QC3
作业 QC2 C005 装      # 故障期间不接受新作业 -> 报错
作业 QC9 C005 装      # 不存在的岸桥 -> 报错
作业 QC1 C099 装      # 不存在的集装箱 -> 报错
推进 QC1              # QC1 完成 C001 装船
作业 QC1 C001 装      # 重复作业 -> 报错
作业 QC1 C006 装      # 标准吊具吊冷藏箱 -> 吊具不匹配
推进 QC3              # QC3 完成 C002 装船，开始 C004
推进 QC3              # QC3 完成 C004 装船
作业 QC3 C002 卸      # C002 已在船上 -> 正常卸船（状态延续）
修复 QC2
作业 QC2 C006 装      # 修复后恢复接活，冷藏吊具吊冷藏箱
作业 QC1 C005 卸      # C005 从未装船 -> 执行时报状态错误
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        lines = DEMO_INPUT.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    engine = Engine()
    run(engine, lines)
    engine.finish()
    print(engine.report())


if __name__ == "__main__":
    main(sys.argv)
