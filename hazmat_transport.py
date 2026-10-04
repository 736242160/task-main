#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
危化品运输任务管理工具（纯 Python 标准库，单文件）

功能：
  - 校验任务罐体类型与车辆罐体类型是否匹配
  - 校验押运员/司机资质（自定规则，见下）
  - 校验任务时段/路线是否命中禁行规则（报告 任务/路段/规则）
  - 校验任务引用的车辆、人员、路段是否存在
  - 事故发生后，同路段同罐体类型的任务级联暂停
  - 暂停任务按自定条件恢复
  - 同一车辆同时段多任务冲突检测
  - 任务流与事故流按顺序处理，状态跨流延续
  - 输出任务状态表与错误清单

-----------------------------------------------------------------------------
自定规则说明（题目要求"规则自定并说明理由"的部分）
-----------------------------------------------------------------------------

【押运员规则】
  每个运输任务必须同时配备：
    1. 一名持 "司机" 资质的驾驶员；
    2. 一名持 "押运员" 资质的押运员（不可缺省，输入用 "-" 表示未配备）；
    3. 司机与押运员不得为同一人。
  理由：依据《危险化学品安全管理条例》及道路运输安全惯例，危化品运输
  途中必须配备专职押运员负责货物监护与应急处置；驾驶与押运职责分离，
  避免一人分饰两角导致监督失效。

【暂停恢复条件】
  事故发生后，事故路段被登记为"封闭"状态（按 路段+罐体类型 记录）。
  当输入流中出现 CLEAR <路段> 事件（表示事故处置完毕、路段解封）时，
  该路段的封闭记录全部清除；所有处于"暂停"状态、且其路线与罐体类型
  不再受任何未解除封闭影响的任务，恢复为"已恢复"状态。
  理由：现实中事故路段由交管/应急部门处置完毕后发布解封通告，
  CLEAR 事件即对应解封通告；只有全部相关封闭都解除，任务才允许继续，
  防止多重事故场景下过早恢复。

-----------------------------------------------------------------------------
输入格式（文本文件或标准输入，# 开头为注释，空行忽略）
-----------------------------------------------------------------------------

  定义区（须出现在事件之前）：
    SEGMENT  <路段编号>                                # 路段定义（可选，规则中的路段自动登记）
    VEHICLE  <车辆编号> <罐体类型>
    PERSON   <人员编号> <资质1,资质2>                  # 资质：司机 / 押运员
    RULE     <规则编号> <路段> <禁行时段> <罐体类型1,罐体类型2>

  事件流（按顺序处理，状态跨事件延续）：
    TASK     <任务编号> <货物> <罐体类型> <车辆> <司机> <押运员> <路段1,路段2,...> <时段>
    ACCIDENT <任务编号> <路段>
    CLEAR    <路段>

  时段格式：HH:MM-HH:MM（当日，开始须早于结束）
  押运员缺省用 "-" 表示。

用法：
    python3 hazmat_transport.py 输入文件
    python3 hazmat_transport.py            # 从标准输入读取
    python3 hazmat_transport.py --demo     # 运行内置自测样例
"""

import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 数据模型

@dataclass
class Rule:
    rid: str
    segment: str
    start: int          # 分钟
    end: int
    tanks: set

@dataclass
class Task:
    tid: str
    cargo: str
    tank: str
    vehicle: str
    driver: str
    escort: str
    route: list
    start: int
    end: int
    status: str = "正常"     # 正常 / 暂停 / 已恢复 / 事故 / 异常
    errors: list = field(default_factory=list)

@dataclass
class World:
    vehicles: dict = field(default_factory=dict)   # 编号 -> 罐体类型
    persons: dict = field(default_factory=dict)    # 编号 -> 资质集合
    rules: dict = field(default_factory=dict)      # 编号 -> Rule
    segments: set = field(default_factory=set)
    tasks: dict = field(default_factory=dict)      # 编号 -> Task
    order: list = field(default_factory=list)      # 任务出现顺序
    errors: list = field(default_factory=list)     # (任务编号或"-", 类型, 详情)
    accidents: dict = field(default_factory=dict)  # 路段 -> [(事故任务, 罐体类型)]

# ---------------------------------------------------------------- 工具函数

def parse_time(text, lineno):
    """'HH:MM-HH:MM' -> (start_min, end_min)，非法抛 ValueError。"""
    try:
        a, b = text.split("-")
        sh, sm = a.split(":")
        eh, em = b.split(":")
        s = int(sh) * 60 + int(sm)
        e = int(eh) * 60 + int(em)
    except ValueError:
        raise ValueError("第%d行：时段格式错误 %r，应为 HH:MM-HH:MM" % (lineno, text))
    if not (0 <= s < 1440 and 0 <= e <= 1440 and s < e):
        raise ValueError("第%d行：时段 %r 非法（须在当日且开始早于结束）" % (lineno, text))
    return s, e

def fmt_time(m):
    return "%02d:%02d" % (m // 60, m % 60)

def overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end

# ---------------------------------------------------------------- 核心逻辑

def report(world, tid, etype, detail):
    world.errors.append((tid, etype, detail))
    if tid in world.tasks:
        world.tasks[tid].errors.append("%s: %s" % (etype, detail))

def add_task(world, lineno, f):
    if len(f) != 9:
        raise ValueError("第%d行：TASK 需要 8 个字段，实际 %d 个" % (lineno, len(f) - 1))
    _, tid, cargo, tank, veh, drv, esc, route_s, span = f
    if tid in world.tasks:
        raise ValueError("第%d行：任务编号 %s 重复" % (lineno, tid))
    start, end = parse_time(span, lineno)
    route = route_s.split(",")
    t = Task(tid, cargo, tank, veh, drv, esc, route, start, end)
    world.tasks[tid] = t
    world.order.append(tid)

    # 1) 引用存在性
    if veh not in world.vehicles:
        report(world, tid, "引用不存在", "车辆 %s 未定义" % veh)
    if drv not in world.persons:
        report(world, tid, "引用不存在", "司机 %s 未定义" % drv)
    if esc != "-" and esc not in world.persons:
        report(world, tid, "引用不存在", "押运员 %s 未定义" % esc)
    for seg in route:
        if seg not in world.segments:
            report(world, tid, "引用不存在", "路段 %s 未定义" % seg)

    # 2) 罐体类型匹配
    if veh in world.vehicles and world.vehicles[veh] != tank:
        report(world, tid, "罐体不匹配",
               "任务罐体 %s 与车辆 %s 的罐体 %s 不一致" % (tank, veh, world.vehicles[veh]))

    # 3) 司机/押运员资质（自定规则，见文件头说明）
    if drv in world.persons and "司机" not in world.persons[drv]:
        report(world, tid, "资质不符", "人员 %s 不具备司机资质" % drv)
    if esc == "-":
        report(world, tid, "缺少押运员", "危化品运输任务必须配备持押运员资质的押运员")
    elif esc in world.persons and "押运员" not in world.persons[esc]:
        report(world, tid, "资质不符", "人员 %s 不具备押运员资质" % esc)
    if esc != "-" and esc == drv:
        report(world, tid, "资质不符", "司机与押运员不得为同一人（%s）" % esc)

    # 4) 禁行规则命中（报告 任务/路段/规则）
    for seg in route:
        for r in world.rules.values():
            if (r.segment == seg and tank in r.tanks
                    and overlap(start, end, r.start, r.end)):
                report(world, tid, "禁行命中",
                       "任务 %s 时段 %s-%s 经过路段 %s，命中规则 %s"
                       "（禁行 %s-%s，限行罐体 %s）"
                       % (tid, fmt_time(start), fmt_time(end), seg, r.rid,
                          fmt_time(r.start), fmt_time(r.end),
                          "/".join(sorted(r.tanks))))

    # 5) 同车辆同时段冲突（只与有效任务比较：正常/暂停/已恢复）
    for oid in world.order:
        o = world.tasks[oid]
        if o is t or o.vehicle != veh or o.status == "异常":
            continue
        if overlap(start, end, o.start, o.end):
            report(world, tid, "车辆时段冲突",
                   "车辆 %s 在 %s-%s 已被任务 %s（%s-%s）占用"
                   % (veh, fmt_time(start), fmt_time(end), oid,
                      fmt_time(o.start), fmt_time(o.end)))

    if t.errors:
        t.status = "异常"
        return

    # 6) 跨流状态延续：若路线命中尚未解除的事故封闭，则直接暂停
    if accident_blocks(world, t):
        t.status = "暂停"

def accident_blocks(world, task):
    """任务是否受当前未解除的事故封闭影响。"""
    for seg in task.route:
        for _, atank in world.accidents.get(seg, []):
            if atank == task.tank:
                return True
    return False

def add_accident(world, lineno, f):
    if len(f) != 3:
        raise ValueError("第%d行：ACCIDENT 需要 2 个字段" % lineno)
    _, tid, seg = f
    if tid not in world.tasks:
        report(world, "-", "引用不存在", "事故引用了不存在的任务 %s" % tid)
        return
    if seg not in world.segments:
        report(world, tid, "引用不存在", "事故路段 %s 未定义" % seg)
        return
    t = world.tasks[tid]
    t.status = "事故"
    world.accidents.setdefault(seg, []).append((tid, t.tank))
    # 级联暂停：同路段、同罐体类型的在途任务
    for oid in world.order:
        o = world.tasks[oid]
        if (o.status == "正常" and o.tank == t.tank and seg in o.route):
            o.status = "暂停"

def add_clear(world, lineno, f):
    if len(f) != 2:
        raise ValueError("第%d行：CLEAR 需要 1 个字段" % lineno)
    _, seg = f
    world.accidents.pop(seg, None)   # 解除该路段全部封闭记录
    # 恢复：不再受任何封闭影响的暂停任务
    for oid in world.order:
        o = world.tasks[oid]
        if o.status == "暂停" and not accident_blocks(world, o):
            o.status = "已恢复"

# ---------------------------------------------------------------- 输入解析

def process(world, lines):
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hashpos = line.find("#")          # 支持行内注释
        if hashpos != -1:
            line = line[:hashpos].strip()
        if not line:
            continue
        f = line.split()
        cmd = f[0].upper()
        if cmd == "SEGMENT":
            world.segments.add(f[1])
        elif cmd == "VEHICLE":
            world.vehicles[f[1]] = f[2]
        elif cmd == "PERSON":
            world.persons[f[1]] = set(f[2].split(","))
        elif cmd == "RULE":
            s, e = parse_time(f[3], lineno)
            world.rules[f[1]] = Rule(f[1], f[2], s, e, set(f[4].split(",")))
            world.segments.add(f[2])
        elif cmd == "TASK":
            add_task(world, lineno, f)
        elif cmd == "ACCIDENT":
            add_accident(world, lineno, f)
        elif cmd == "CLEAR":
            add_clear(world, lineno, f)
        else:
            raise ValueError("第%d行：未知指令 %s" % (lineno, cmd))

# ---------------------------------------------------------------- 输出

def render(world):
    out = []
    out.append("=" * 64)
    out.append("任务状态")
    out.append("=" * 64)
    out.append("%-6s %-6s %-5s %-6s %-14s %s" % ("任务", "货物", "罐体", "车辆", "时段", "状态"))
    for tid in world.order:
        t = world.tasks[tid]
        out.append("%-6s %-6s %-5s %-6s %-14s %s"
                   % (t.tid, t.cargo, t.tank, t.vehicle,
                      "%s-%s" % (fmt_time(t.start), fmt_time(t.end)), t.status))
    out.append("")
    out.append("=" * 64)
    out.append("错误报告（共 %d 条）" % len(world.errors))
    out.append("=" * 64)
    if not world.errors:
        out.append("（无错误）")
    for i, (tid, etype, detail) in enumerate(world.errors, 1):
        out.append("[E%02d] 任务=%s 类型=%s" % (i, tid, etype))
        out.append("      %s" % detail)
    return "\n".join(out)

# ---------------------------------------------------------------- 自测样例

DEMO = r"""
# ---- 定义区 ----
SEGMENT  S3
VEHICLE  V1 罐A
VEHICLE  V2 罐B
VEHICLE  V3 罐A
PERSON   P1 司机
PERSON   P2 押运员
PERSON   P3 司机,押运员
RULE     R1 S1 08:00-10:00 罐A
RULE     R2 S2 12:00-14:00 罐A,罐B

# ---- 事件流 ----
TASK     T1 汽油 罐A V1 P1 P2 S1,S3 07:00-08:30   # 命中禁行 R1
TASK     T2 柴油 罐B V2 P3 P2 S2,S3 09:00-10:00   # 正常
TASK     T3 柴油 罐B V2 P3 P2 S3 09:30-10:30      # 与 T2 车辆时段冲突
TASK     T4 汽油 罐A V9 P1 P2 S3 10:00-11:00      # 车辆 V9 不存在
TASK     T5 汽油 罐A V1 P1 P2 S9 10:00-11:00      # 路段 S9 不存在
TASK     T6 汽油 罐A V1 P1 - S3 10:00-11:00       # 缺少押运员
TASK     T7 汽油 罐B V1 P1 P2 S3 10:00-11:00      # 罐体与车辆不匹配
TASK     T8 汽油 罐A V1 P2 P2 S3 10:00-11:00      # 司机无司机资质+司押同人
TASK     T9 汽油 罐A V1 P1 P2 S1,S3 11:00-12:00   # 正常
TASK     T10 汽油 罐A V3 P3 P2 S1,S3 11:15-12:15  # 正常
TASK     T11 汽油 罐B V2 P3 P2 S1 13:00-14:00     # 正常（罐B 不受 R1 限制）
ACCIDENT T9 S1                                    # T9 在 S1 发生事故
#        -> T9 变"事故"；T10（同路段 S1、同罐体 罐A）级联暂停；T11 罐B 不受影响
TASK     T12 汽油 罐A V3 P3 P2 S1 12:30-13:30     # 跨流延续：直接暂停
CLEAR    S1                                       # 事故处置完毕，路段解封
#        -> T10、T12 恢复为"已恢复"
TASK     T13 汽油 罐A V1 P1 P9 S3 13:00-14:00     # 押运员 P9 不存在
ACCIDENT T99 S1                                   # 事故引用不存在的任务
"""

def main(argv):
    if "--demo" in argv:
        print("---- 自测样例输入 ----")
        print(DEMO.strip())
        print()
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    world = World()
    try:
        process(world, lines)
    except ValueError as exc:
        print("输入解析失败：%s" % exc, file=sys.stderr)
        return 2
    print(render(world))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
