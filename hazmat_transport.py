#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""危化品运输任务校验与事故联动工具（纯 Python 标准库，单文件）。

用法：
    python3 hazmat_transport.py run [输入文件]   # 省略文件则从标准输入读取
    python3 hazmat_transport.py demo             # 运行内置样例并打印结果
    python3 hazmat_transport.py selftest         # 运行内置自测（断言状态与错误）

输入格式（每行一条指令，字段以空白分隔，# 开头为注释）：
    SEGMENT  <路段>                                        定义路段（BAN 中的路段也会自动登记）
    VEHICLE  <车辆> <罐体类型>
    PERSON   <人员> <资质>...                               资质取值：司机 / 押运员
    BAN      <规则> <路段> <起>-<止> <罐体类型>              禁行规则，时间形如 08:00-10:00
    TASK     <任务> <货物> <罐体类型> <车辆> <司机> <押运员|-> <路段,路段,...> <起>-<止>
    ACCIDENT <任务> <路段>                                  事故：级联暂停同路段同罐体类型的任务
    CLEAR    <路段> <罐体类型>                               事故解除：满足条件的暂停任务恢复

业务规则说明（自定部分）：
    1. 押运员规则：依据《危险化学品安全管理条例》第四十八条，通过道路运输危险化学品的
       应当配备押运人员。故每个任务必须指定一名在册且具备“押运员”资质的人员；
       未指定（用 - 表示）或资质不符均报错。
    2. 恢复条件（自定）：仅当对应 (路段, 罐体类型) 的事故被 CLEAR 指令解除，且任务
       自身无校验错误（状态为“暂停”）时，任务恢复为“已恢复”；其余情况保持原状态。
    3. 状态跨流延续：TASK / ACCIDENT / CLEAR 按文件出现顺序处理，事故造成的暂停
       对之后定义的任务同样生效（新任务若命中未解除的事故路段，直接置为“暂停”）。

任务状态：正常 / 暂停 / 已恢复 / 异常（存在校验错误）。
"""

import argparse
import sys
from dataclasses import dataclass, field

STATUS_OK = "正常"
STATUS_PAUSED = "暂停"
STATUS_RESUMED = "已恢复"
STATUS_ERROR = "异常"


def parse_period(text):
    """解析 'HH:MM-HH:MM' 为 (起始分钟, 结束分钟)。"""
    start_s, end_s = text.split("-", 1)
    def to_min(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    return to_min(start_s), to_min(end_s)


def fmt_period(p):
    return "%02d:%02d-%02d:%02d" % (p[0] // 60, p[0] % 60, p[1] // 60, p[1] % 60)


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


@dataclass
class Ban:
    rid: str
    segment: str
    period: tuple
    tank: str


@dataclass
class Task:
    tid: str
    cargo: str
    tank: str
    vehicle: str
    driver: str
    escort: str
    route: list
    period: tuple
    status: str = STATUS_OK


class Engine:
    def __init__(self):
        self.segments = set()
        self.vehicles = {}          # 车辆 -> 罐体类型
        self.persons = {}           # 人员 -> 资质集合
        self.bans = {}              # 规则 -> Ban
        self.tasks = {}             # 任务 -> Task
        self.task_order = []
        self.errors = []            # (来源任务或"全局", 错误描述)
        self.active_accidents = []  # 未解除的事故：(路段, 罐体类型)

    # ---------- 指令处理 ----------

    def process_line(self, line, lineno):
        text = line.strip()
        if not text or text.startswith("#"):
            return
        parts = text.split()
        cmd, args = parts[0].upper(), parts[1:]
        handler = getattr(self, "do_" + cmd, None)
        if handler is None:
            self.errors.append(("全局", "第%d行：无法识别的指令 %s" % (lineno, cmd)))
            return
        try:
            handler(args)
        except (ValueError, IndexError) as exc:
            self.errors.append(("全局", "第%d行：指令 %s 格式错误（%s）" % (lineno, cmd, exc)))

    def do_SEGMENT(self, args):
        for seg in args:
            self.segments.add(seg)

    def do_VEHICLE(self, args):
        vid, tank = args
        if vid in self.vehicles:
            self.errors.append(("全局", "车辆 %s 重复定义" % vid))
        self.vehicles[vid] = tank

    def do_PERSON(self, args):
        pid, quals = args[0], set(args[1:])
        if pid in self.persons:
            self.errors.append(("全局", "人员 %s 重复定义" % pid))
        self.persons[pid] = quals

    def do_BAN(self, args):
        rid, segment, period_s, tank = args
        if rid in self.bans:
            self.errors.append(("全局", "禁行规则 %s 重复定义" % rid))
        self.segments.add(segment)
        self.bans[rid] = Ban(rid, segment, parse_period(period_s), tank)

    def do_TASK(self, args):
        tid, cargo, tank, vid, driver, escort, route_s, period_s = args
        if tid in self.tasks:
            self.errors.append((tid, "任务 %s 重复定义" % tid))
            return
        route = route_s.split(",")
        task = Task(tid, cargo, tank, vid, driver, escort, route, parse_period(period_s))
        self.tasks[tid] = task
        self.task_order.append(tid)

        errs = []
        # 1. 引用存在性与罐体匹配
        if vid not in self.vehicles:
            errs.append("引用的车辆 %s 不存在" % vid)
        elif self.vehicles[vid] != tank:
            errs.append("罐体类型不匹配：任务需要 %s，车辆 %s 为 %s"
                        % (tank, vid, self.vehicles[vid]))
        if driver not in self.persons:
            errs.append("引用的司机 %s 不存在" % driver)
        elif "司机" not in self.persons[driver]:
            errs.append("人员 %s 不具备司机资质" % driver)
        # 2. 押运员规则（必须配备在册且具押运员资质的人员）
        if escort == "-":
            errs.append("押运员缺失：危化品道路运输必须配备押运员")
        elif escort not in self.persons:
            errs.append("引用的押运员 %s 不存在" % escort)
        elif "押运员" not in self.persons[escort]:
            errs.append("押运员 %s 不具备押运员资质" % escort)
        # 3. 路段存在性
        for seg in route:
            if seg not in self.segments:
                errs.append("路线引用的路段 %s 不存在" % seg)
        # 4. 禁行规则（任务、路段、规则）
        for seg in route:
            for ban in self.bans.values():
                if (ban.segment == seg and ban.tank == tank
                        and overlap(ban.period, task.period)):
                    errs.append("命中禁行：任务 %s 路段 %s 规则 %s（%s 禁行 %s）"
                                % (tid, seg, ban.rid, fmt_period(ban.period), ban.tank))
        # 5. 同车辆同时段多任务
        for other_id in self.task_order:
            if other_id == tid:
                continue
            other = self.tasks[other_id]
            if other.vehicle == vid and overlap(other.period, task.period):
                errs.append("同车辆同时段多任务：%s 与 %s 同时使用车辆 %s（%s）"
                            % (tid, other_id, vid, fmt_period(task.period)))

        if errs:
            task.status = STATUS_ERROR
            for e in errs:
                self.errors.append((tid, e))
        else:
            # 跨流状态延续：命中未解除事故的新任务直接暂停
            if any(seg in route and t == tank for seg, t in self.active_accidents):
                task.status = STATUS_PAUSED

    def do_ACCIDENT(self, args):
        tid, segment = args
        if tid not in self.tasks:
            self.errors.append(("全局", "事故引用的任务 %s 不存在" % tid))
            return
        if segment not in self.segments:
            self.errors.append((tid, "事故引用的路段 %s 不存在" % segment))
            return
        tank = self.tasks[tid].tank
        self.active_accidents.append((segment, tank))
        # 级联暂停：同路段、同罐体类型且当前可运行的任务
        for other_id in self.task_order:
            other = self.tasks[other_id]
            if (other.status in (STATUS_OK, STATUS_RESUMED)
                    and other.tank == tank and segment in other.route):
                other.status = STATUS_PAUSED

    def do_CLEAR(self, args):
        segment, tank = args
        self.active_accidents = [
            a for a in self.active_accidents if a != (segment, tank)]
        # 恢复条件：事故已解除且任务无校验错误（状态为“暂停”）
        for tid in self.task_order:
            task = self.tasks[tid]
            if (task.status == STATUS_PAUSED and task.tank == tank
                    and segment in task.route):
                task.status = STATUS_RESUMED

    # ---------- 输出 ----------

    def report(self, out):
        out.write("===== 任务状态 =====\n")
        for tid in self.task_order:
            task = self.tasks[tid]
            out.write("%-6s 货物:%-6s 罐体:%-4s 车辆:%-4s 时段:%-11s 状态:%s\n"
                      % (tid, task.cargo, task.tank, task.vehicle,
                         fmt_period(task.period), task.status))
        out.write("===== 错误报告（%d 条） =====\n" % len(self.errors))
        if not self.errors:
            out.write("（无错误）\n")
        for i, (src, msg) in enumerate(self.errors, 1):
            out.write("%d. [%s] %s\n" % (i, src, msg))


SAMPLE = """\
# ---- 基础定义 ----
SEGMENT S1 S2 S3 S4
VEHICLE V1 罐A
VEHICLE V2 罐B
PERSON P1 司机
PERSON P2 押运员
PERSON P3 司机 押运员
BAN R1 S1 08:00-10:00 罐A

# ---- 任务流 ----
TASK T1 汽油 罐A V1 P3 P2 S2,S3 09:00-11:00
TASK T2 柴油 罐B V1 P1 P2 S3 10:00-12:00
TASK T3 液氨 罐A V1 P1 - S4 13:00-15:00
TASK T4 汽油 罐A V1 P3 P2 S1 09:30-10:30
TASK T5 汽油 罐A V1 P3 P2 S2 15:00-16:00
TASK T6 苯 罐A V9 P3 P2 S2 16:00-17:00
TASK T7 苯 罐A V1 P3 P2 S9 16:00-17:00
TASK T8 汽油 罐A V1 P3 P2 S2 17:00-18:00

# ---- 事故流 ----
ACCIDENT T1 S2
TASK T9 汽油 罐A V1 P3 P2 S2,S3 18:00-19:00
CLEAR S2 罐A

# ---- 补充错误场景 ----
TASK T10 苯 罐A V1 P9 P2 S3 20:00-21:00
TASK T11 苯 罐A V1 P3 P1 S3 21:00-22:00
"""

EXPECTED_STATUS = {
    "T1": STATUS_RESUMED,   # 事故暂停后被 CLEAR 恢复
    "T2": STATUS_ERROR,     # 罐体不匹配 + 车辆同时段冲突
    "T3": STATUS_ERROR,     # 押运员缺失
    "T4": STATUS_ERROR,     # 命中禁行 R1 + 车辆同时段冲突
    "T5": STATUS_RESUMED,
    "T6": STATUS_ERROR,     # 车辆不存在
    "T7": STATUS_ERROR,     # 路段不存在
    "T8": STATUS_RESUMED,
    "T9": STATUS_RESUMED,   # 事故未解除时定义 -> 暂停 -> 解除后恢复（跨流延续）
    "T10": STATUS_ERROR,    # 司机不存在
    "T11": STATUS_ERROR,    # 押运员资质不符
}

EXPECTED_ERRORS = [
    ("T2", "罐体类型不匹配"),
    ("T2", "同车辆同时段多任务"),
    ("T3", "押运员缺失"),
    ("T4", "命中禁行"),
    ("T4", "规则 R1"),
    ("T6", "车辆 V9 不存在"),
    ("T7", "路段 S9 不存在"),
    ("T10", "司机 P9 不存在"),
    ("T11", "不具备押运员资质"),
]


def run_text(text, out):
    engine = Engine()
    for lineno, line in enumerate(text.splitlines(), 1):
        engine.process_line(line, lineno)
    engine.report(out)
    return engine


def cmd_selftest():
    import io
    buf = io.StringIO()
    engine = run_text(SAMPLE, buf)
    failures = []
    for tid, want in EXPECTED_STATUS.items():
        got = engine.tasks[tid].status
        if got != want:
            failures.append("任务 %s 状态应为 %s，实际为 %s" % (tid, want, got))
    for src, needle in EXPECTED_ERRORS:
        if not any(s == src and needle in m for s, m in engine.errors):
            failures.append("缺少预期错误：[%s] 包含“%s”" % (src, needle))
    sys.stdout.write(buf.getvalue())
    print("===== 自测 =====")
    if failures:
        for f in failures:
            print("FAIL:", f)
        print("自测未通过（%d 项）" % len(failures))
        return 1
    print("全部 %d 项状态断言、%d 项错误断言通过。"
          % (len(EXPECTED_STATUS), len(EXPECTED_ERRORS)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="危化品运输任务校验与事故联动工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="从文件（或标准输入）读取指令并输出报告")
    p_run.add_argument("file", nargs="?", help="输入文件，省略则读标准输入")
    sub.add_parser("demo", help="运行内置样例并打印报告")
    sub.add_parser("selftest", help="运行内置自测断言")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        else:
            text = sys.stdin.read()
        run_text(text, sys.stdout)
        return 0
    if args.cmd == "demo":
        print("----- 样例输入 -----")
        print(SAMPLE)
        print("----- 运行结果 -----")
        run_text(SAMPLE, sys.stdout)
        return 0
    if args.cmd == "selftest":
        return cmd_selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
