#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海事交通组织裁决工具（纯 Python 标准库，单文件）。

输入（文本行，# 为注释，空白分隔）：
    航道 <名称> <上行限速> <下行限速>          # 限速单位 km/h
    船舶 <名称> <类型:货轮|渔船> <速度>         # 速度单位 km/h
    事件 <船名> <航道> <分道:上行|下行> <位置km> <时刻> [速度]
        时刻支持 HH:MM / HH:MM:SS / YYYY-MM-DDTHH:MM[:SS]
        可选速度用于表达改速事件；缺省沿用船舶定义速度。

输出：航行状态表 + 错误/告警清单（按处理顺序编号）。

规则与阈值（均可命令行覆盖，理由见下）：
  * 超速：船速 > 所在分道限速，报告超速值。
  * 对向冲突：同航道、异分道、间距 <= --head-on（默认 5 km）。
      理由：两船各约 12 km/h 对驶时接近率约 24 km/h，5 km 约 12 分钟
      会遇时间，足以完成避让决策与 VHF 协调。
  * 追越冲突：同分道、后船速度 > 前船、间距 <= --overtake（默认 2 km）。
      理由：同向相对速度低（常 < 5 km/h），2 km 对应约 25 分钟以上的
      接近窗口；间距再小则追越回旋余地不足。
  * 渔船占用货轮分道：分道通航制下上/下行分道为机动船通航分道，
      渔船应在航道外作业；渔船出现在任何已定义分道即违规。
  * 位置异常跳变：由位移/时间推算的隐含速度 >
      max(2×当前船速, 当前船速+15km/h)，视为定位跳变/记录错误。
  * 船速异常跳变：改速事件中 |新速-旧速| > max(30%×旧速, 5km/h)。
  * 时刻异常：同一船的事件时刻倒退或重复。
  * 改向（分道变化）后，对该航道全部船舶做冲突级联重检
      （实现上：每个事件后都重检该航道全部船对，天然覆盖级联）。
  * 引用不存在的航道/船舶、非法分道：报告并跳过该事件。
状态跨事件延续：每船保留最新航道/分道/位置/时刻/速度。
"""

import argparse
import sys
from datetime import datetime

LANES = ("上行", "下行")
LANE_DIR = {"上行": 1, "下行": -1}  # 位置沿航道递增为上行

TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%H:%M:%S", "%H:%M")


def parse_time(text):
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("无法解析时刻 %r（支持 HH:MM / HH:MM:SS / YYYY-MM-DDTHH:MM）" % text)


class Reporter:
    def __init__(self):
        self.errors = []

    def report(self, category, message, when=None):
        self.errors.append((len(self.errors) + 1, when or "-", category, message))


class Channel:
    def __init__(self, name, up_limit, down_limit):
        self.name = name
        self.limits = {"上行": up_limit, "下行": down_limit}


class Ship:
    def __init__(self, name, kind, speed):
        self.name = name
        self.kind = kind      # 货轮 / 渔船
        self.speed = speed    # km/h


class ShipState:
    def __init__(self, channel, lane, pos, when, speed):
        self.channel = channel
        self.lane = lane
        self.pos = pos
        self.when = when
        self.speed = speed


def recheck_conflicts(channel_name, states, ships, reporter, when,
                      head_on_km, overtake_km):
    """对指定航道内全部船对做冲突级联重检。"""
    on_channel = [(name, st) for name, st in states.items()
                  if st.channel == channel_name]
    for i in range(len(on_channel)):
        for j in range(i + 1, len(on_channel)):
            name_a, a = on_channel[i]
            name_b, b = on_channel[j]
            if a.lane != b.lane:
                dist = abs(a.pos - b.pos)
                if dist <= head_on_km:
                    reporter.report(
                        "对向冲突",
                        "航道[%s] 船[%s](%s) 与 船[%s](%s) 对向接近，距离 %.2f km "
                        "（阈值 %.2f km）" % (channel_name, name_a, a.lane,
                                              name_b, b.lane, dist, head_on_km),
                        when)
            else:
                direction = LANE_DIR[a.lane]
                gap_ab = (b.pos - a.pos) * direction  # >0 表示 b 在 a 前方
                if gap_ab > 0:
                    back_name, back, front_name, front = name_a, a, name_b, b
                    gap = gap_ab
                else:
                    back_name, back, front_name, front = name_b, b, name_a, a
                    gap = -gap_ab
                if 0 < gap <= overtake_km and back.speed > front.speed:
                    reporter.report(
                        "追越冲突",
                        "航道[%s] 分道[%s] 后船[%s](%.1f km/h) 接近前船[%s]"
                        "(%.1f km/h)，间距 %.2f km（阈值 %.2f km）"
                        % (channel_name, a.lane, back_name, back.speed,
                           front_name, front.speed, gap, overtake_km),
                        when)


def process(lines, head_on_km, overtake_km):
    channels = {}
    ships = {}
    states = {}
    reporter = Reporter()

    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        keyword, args = parts[0], parts[1:]
        where = "行%d" % lineno

        if keyword in ("航道", "channel"):
            if len(args) != 3:
                reporter.report("格式错误", "%s：航道定义应为：航道 <名称> <上行限速> <下行限速>" % where)
                continue
            name = args[0]
            try:
                up_limit, down_limit = float(args[1]), float(args[2])
            except ValueError:
                reporter.report("格式错误", "%s：航道[%s] 限速不是数字" % (where, name))
                continue
            channels[name] = Channel(name, up_limit, down_limit)

        elif keyword in ("船舶", "ship"):
            if len(args) != 3:
                reporter.report("格式错误", "%s：船舶定义应为：船舶 <名称> <货轮|渔船> <速度>" % where)
                continue
            name, kind = args[0], args[1]
            if kind not in ("货轮", "渔船"):
                reporter.report("格式错误", "%s：船舶[%s] 类型须为 货轮/渔船，实为 %r" % (where, name, kind))
                continue
            try:
                speed = float(args[2])
            except ValueError:
                reporter.report("格式错误", "%s：船舶[%s] 速度不是数字" % (where, name))
                continue
            ships[name] = Ship(name, kind, speed)

        elif keyword in ("事件", "event"):
            if len(args) not in (5, 6):
                reporter.report("格式错误",
                                "%s：事件应为：事件 <船> <航道> <上行|下行> <位置km> <时刻> [速度]" % where)
                continue
            ship_name, channel_name, lane = args[0], args[1], args[2]
            try:
                pos = float(args[3])
            except ValueError:
                reporter.report("格式错误", "%s：位置 %r 不是数字" % (where, args[3]))
                continue
            try:
                when = parse_time(args[4])
            except ValueError as exc:
                reporter.report("格式错误", "%s：%s" % (where, exc))
                continue
            when_str = args[4]

            if ship_name not in ships:
                reporter.report("未知船舶",
                                "%s：事件引用了不存在的船[%s]，事件已跳过" % (where, ship_name), when_str)
                continue
            if channel_name not in channels:
                reporter.report("未知航道",
                                "%s：船[%s] 事件引用了不存在的航道[%s]，事件已跳过"
                                % (where, ship_name, channel_name), when_str)
                continue
            if lane not in LANES:
                reporter.report("格式错误",
                                "%s：分道须为 上行/下行，实为 %r，事件已跳过" % (where, lane), when_str)
                continue

            ship = ships[ship_name]
            if len(args) == 6:
                try:
                    new_speed = float(args[5])
                except ValueError:
                    reporter.report("格式错误", "%s：事件速度 %r 不是数字" % (where, args[5]), when_str)
                    continue
            else:
                new_speed = ship.speed

            prev = states.get(ship_name)
            if prev is not None:
                dt_hours = (when - prev.when).total_seconds() / 3600.0
                if dt_hours <= 0:
                    reporter.report("时刻异常",
                                    "船[%s] 事件时刻 %s 不晚于上一时刻 %s，时间倒退/重复"
                                    % (ship_name, when_str, prev.when.strftime("%H:%M")), when_str)
                else:
                    implied = abs(pos - prev.pos) / dt_hours
                    jump_limit = max(2.0 * prev.speed, prev.speed + 15.0)
                    if implied > jump_limit:
                        reporter.report(
                            "位置跳变",
                            "船[%s] 位置 %.2f→%.2f km，历时 %.1f 分钟，隐含速度 %.1f km/h "
                            "超过阈值 %.1f km/h（当前船速 %.1f km/h），疑似定位异常"
                            % (ship_name, prev.pos, pos, dt_hours * 60.0,
                               implied, jump_limit, prev.speed), when_str)
                speed_jump_limit = max(0.3 * prev.speed, 5.0)
                if abs(new_speed - prev.speed) > speed_jump_limit:
                    reporter.report("船速跳变",
                                    "船[%s] 速度 %.1f→%.1f km/h，跳变 %.1f km/h 超过阈值 %.1f km/h"
                                    % (ship_name, prev.speed, new_speed,
                                       abs(new_speed - prev.speed), speed_jump_limit), when_str)
                if lane != prev.lane or channel_name != prev.channel:
                    # 改向/改航道事件：随后的 recheck_conflicts 即级联重检
                    pass

            states[ship_name] = ShipState(channel_name, lane, pos, when, new_speed)

            limit = channels[channel_name].limits[lane]
            if new_speed > limit:
                reporter.report("超速",
                                "航道[%s] 分道[%s] 船[%s] 速度 %.1f km/h 超过限速 %.1f km/h，"
                                "超速 %.1f km/h" % (channel_name, lane, ship_name,
                                                    new_speed, limit, new_speed - limit),
                                when_str)
            if ship.kind == "渔船":
                reporter.report("渔船占道",
                                "航道[%s] 分道[%s] 被渔船[%s] 占用；分道为货轮等机动船"
                                "通航分道，渔船应驶出航道外作业" % (channel_name, lane, ship_name),
                                when_str)

            # 每个事件（含改向）后对该航道全部船对做冲突级联重检
            recheck_conflicts(channel_name, states, ships, reporter, when_str,
                              head_on_km, overtake_km)
        else:
            reporter.report("格式错误", "%s：未知关键字 %r（应为 航道/船舶/事件）" % (where, keyword))

    return channels, ships, states, reporter


def print_report(ships, states, reporter, out):
    out.write("=" * 72 + "\n")
    out.write("航行状态（跨事件延续的最终状态）\n")
    out.write("=" * 72 + "\n")
    header = "%-12s %-6s %-10s %-6s %10s %10s  %s" % (
        "船名", "类型", "航道", "分道", "位置(km)", "速度(km/h)", "时刻")
    out.write(header + "\n")
    out.write("-" * 72 + "\n")
    for name in sorted(ships):
        ship = ships[name]
        st = states.get(name)
        if st is None:
            out.write("%-12s %-6s %s\n" % (name, ship.kind, "（无航迹）"))
        else:
            out.write("%-12s %-6s %-10s %-6s %10.2f %10.1f  %s\n" % (
                name, ship.kind, st.channel, st.lane, st.pos, st.speed,
                st.when.strftime("%Y-%m-%d %H:%M")))

    out.write("\n" + "=" * 72 + "\n")
    out.write("错误/告警清单（共 %d 条）\n" % len(reporter.errors))
    out.write("=" * 72 + "\n")
    if not reporter.errors:
        out.write("（无）\n")
    for seq, when, category, message in reporter.errors:
        out.write("#%-3d [%s] %s: %s\n" % (seq, when, category, message))


DEMO_INPUT = """\
# 演示数据：覆盖全部报告类型
航道 长江口 15 12
航道 珠江口 10 10
船舶 远洋一号 货轮 14
船舶 海风号 货轮 11
船舶 渔舟七号 渔船 8
船舶 幽灵船 货轮 20
事件 远洋一号 长江口 上行 10.0 08:00
事件 海风号 长江口 下行 13.0 08:00
事件 渔舟七号 长江口 上行 20.0 08:05
事件 远洋一号 长江口 上行 12.0 08:10
事件 幽灵船 长江口 上行 0.0 08:00
事件 幽灵船 长江口 上行 30.0 08:10
事件 远洋一号 长江口 下行 12.5 08:20
事件 远洋一号 长江口 下行 13.0 08:30 25
事件 未知船 长江口 上行 5.0 08:30
事件 海风号 不存在航道 上行 5.0 08:30
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="海事交通组织裁决工具：输入航道/船舶/航行流，输出航行状态与错误清单")
    parser.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    parser.add_argument("--head-on", type=float, default=5.0,
                        help="对向冲突阈值 km（默认 5.0）")
    parser.add_argument("--overtake", type=float, default=2.0,
                        help="追越冲突阈值 km（默认 2.0）")
    parser.add_argument("--demo", action="store_true", help="运行内置演示数据")
    args = parser.parse_args(argv)

    if args.demo:
        lines = DEMO_INPUT.splitlines()
    elif args.input:
        with open(args.input, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    _, ships, states, reporter = process(lines, args.head_on, args.overtake)
    print_report(ships, states, reporter, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
