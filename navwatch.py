#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
navwatch.py — 海事交通组织裁决工具（纯 Python 标准库，单文件）

输入为文本行（# 为注释，空白分隔），三类指令：

    航道 <名称> <上行|下行> <限速km/h>        # 每条分道一行
    船舶 <名称> <货轮|渔船> <速度km/h>
    事件 <船名> <航道名> <上行|下行> <位置km> <时刻HH:MM[:SS]>

用法：
    python3 navwatch.py 输入文件            # 或省略文件名从标准输入读取
    python3 navwatch.py demo.txt --head-on 6 --overtake 2.5

裁决规则与阈值（均可命令行覆盖，理由见报告头部）：
  * 超速        船速（优先取相邻事件推算速度，否则取申报速度）超过所在分道限速
  * 对向冲突    同航道、异分道、互相接近且距离 < 5km（--head-on）
  * 追越冲突    同航道同分道、后船速度快于前船且距离 < 2km（--overtake）
  * 渔船占用    渔船速度 < 4km/h（视为作业/漂泊）而占用货轮分道（--fish-speed）
  * 位置跳变    相邻事件推算速度 > 40km/h，超出物理极限（--max-speed）
  * 速度跳变    相邻事件推算速度突变 > 8km/h（--speed-jump）
  * 改向级联    船舶换分道/换航道后，对受影响航道内所有船舶重检冲突
  * 引用错误    事件引用未定义的船舶或航道/分道
状态跨事件延续：每船保留最新航道/分道/位置/时刻/推算速度。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

DIRECTION = {"上行": 1, "下行": -1}  # 上行=公里标增大方向，下行=减小方向


@dataclass
class Lane:
    channel: str
    name: str
    limit: float


@dataclass
class Ship:
    name: str
    kind: str   # 货轮 / 渔船
    speed: float


@dataclass
class State:
    channel: str
    lane: str
    pos: float
    t: float          # 分钟（自当日 00:00）
    t_str: str
    implied: float | None = None  # 由相邻事件推算的速度 km/h


def parse_time(s: str) -> float:
    parts = s.split(":")
    if len(parts) == 2:
        h, m, sec = parts[0], parts[1], "0"
    elif len(parts) == 3:
        h, m, sec = parts
    else:
        raise ValueError(f"时刻格式应为 HH:MM 或 HH:MM:SS：{s!r}")
    return int(h) * 60 + int(m) + int(sec) / 60.0


class Engine:
    def __init__(self, head_on=5.0, overtake=2.0, fish_speed=4.0,
                 max_speed=40.0, speed_jump=8.0):
        self.head_on = head_on
        self.overtake = overtake
        self.fish_speed = fish_speed
        self.max_speed = max_speed
        self.speed_jump = speed_jump
        self.lanes: dict[tuple[str, str], Lane] = {}
        self.ships: dict[str, Ship] = {}
        self.states: dict[str, State] = {}
        self.errors: list[tuple[str, str, str]] = []  # (时刻, 类别, 描述)
        self.active: set = set()                       # 活跃冲突键（去重用）

    # ---------------- 输入解析 ----------------
    def load(self, lines):
        events = []
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            kw = parts[0]
            try:
                if kw == "航道":
                    name, lane, limit = parts[1], parts[2], float(parts[3])
                    if lane not in DIRECTION:
                        raise ValueError("分道必须是 上行/下行")
                    self.lanes[(name, lane)] = Lane(name, lane, limit)
                elif kw == "船舶":
                    name, kind, speed = parts[1], parts[2], float(parts[3])
                    if kind not in ("货轮", "渔船"):
                        raise ValueError("类型必须是 货轮/渔船")
                    self.ships[name] = Ship(name, kind, speed)
                elif kw == "事件":
                    events.append((lineno, parts))
                else:
                    self.error("-", "格式错误", f"第{lineno}行：无法识别的指令 {kw!r}")
            except (IndexError, ValueError) as exc:
                self.error("-", "格式错误", f"第{lineno}行：{raw.strip()!r}（{exc}）")
        for lineno, parts in events:  # 先建全量定义，再按序处理事件流
            self.event(lineno, parts)

    # ---------------- 事件处理 ----------------
    def event(self, lineno, parts):
        try:
            ship_name, channel, lane = parts[1], parts[2], parts[3]
            pos = float(parts[4])
            t_str = parts[5]
            t = parse_time(t_str)
        except (IndexError, ValueError) as exc:
            self.error("-", "格式错误", f"第{lineno}行事件格式错误（{exc}）")
            return

        ok = True
        if ship_name not in self.ships:
            self.error(t_str, "未知船舶",
                       f"事件引用不存在的船舶 {ship_name!r}（第{lineno}行）")
            ok = False
        if (channel, lane) not in self.lanes:
            self.error(t_str, "未知航道",
                       f"事件引用不存在的航道/分道 {channel!r} {lane!r}（第{lineno}行）")
            ok = False
        if not ok:
            return

        ship = self.ships[ship_name]
        prev = self.states.get(ship_name)
        implied = None
        direction_change = False
        affected = {channel}

        if prev is not None:
            affected.add(prev.channel)
            if t < prev.t:
                self.error(t_str, "时间回退",
                           f"船={ship_name} 时刻 {t_str} 早于上一事件 {prev.t_str}")
            else:
                dt_h = (t - prev.t) / 60.0
                if dt_h > 0:
                    implied = abs(pos - prev.pos) / dt_h
                    if implied > self.max_speed:
                        self.error(t_str, "位置跳变",
                                   f"船={ship_name} 位置 {prev.pos}→{pos}km，"
                                   f"推算速度 {implied:.1f}km/h 超出物理上限 {self.max_speed}km/h")
                        # 物理上不可能的速度不可信，后续速度类规则回退用申报速度
                        implied = None
                    else:
                        base = prev.implied if prev.implied is not None else ship.speed
                        if abs(implied - base) > self.speed_jump:
                            self.error(t_str, "速度跳变",
                                       f"船={ship_name} 推算速度 {base:.1f}→{implied:.1f}km/h，"
                                       f"突变超过 {self.speed_jump}km/h")
            if prev.channel != channel or prev.lane != lane:
                direction_change = True

        self.states[ship_name] = State(channel, lane, pos, t, t_str, implied)

        eff = implied if implied is not None else ship.speed
        limit = self.lanes[(channel, lane)].limit
        if eff > limit:
            self.error(t_str, "超速",
                       f"航道={channel}({lane}) 船={ship_name} 速度={eff:.1f}km/h "
                       f"限速={limit:.1f}km/h 超速={eff - limit:.1f}km/h")

        if ship.kind == "渔船" and eff < self.fish_speed:
            self.error(t_str, "渔船占用",
                       f"航道={channel}({lane}) 船={ship_name} 渔船航速 {eff:.1f}km/h "
                       f"低于 {self.fish_speed}km/h（视为作业/漂泊），占用货轮分道")

        self.recheck(affected, t_str, direction_change)

    # ---------------- 冲突检测 ----------------
    def eff_speed(self, name: str) -> float:
        st = self.states[name]
        return st.implied if st.implied is not None else self.ships[name].speed

    def conflicts(self, channel: str):
        """返回 {冲突键: (类别, 描述)}，键含航道与船舶对，用于跨事件去重。"""
        res = {}
        names = [n for n, st in self.states.items() if st.channel == channel]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                sa, sb = self.states[a], self.states[b]
                da, db = DIRECTION[sa.lane], DIRECTION[sb.lane]
                dist = abs(sa.pos - sb.pos)
                if da != db:  # 对向：b 位于 a 航向前方（含同位相撞）则互相接近
                    closing = (sb.pos - sa.pos) * da >= 0
                    if closing and dist < self.head_on:
                        key = ("对向冲突", channel, tuple(sorted((a, b))))
                        res[key] = ("对向冲突",
                                    f"两船={a}({sa.lane}@{sa.pos}km) 与 {b}({sb.lane}@{sb.pos}km) "
                                    f"对向接近 距离={dist:.2f}km（阈值{self.head_on}km）")
                else:  # 同向：确定前后船，后船更快且距离小于阈值则追越冲突
                    if (sb.pos - sa.pos) * da > 0:
                        front_n, rear_n = b, a
                    else:
                        front_n, rear_n = a, b
                    vf, vr = self.eff_speed(front_n), self.eff_speed(rear_n)
                    if vr > vf and dist < self.overtake:
                        key = ("追越冲突", channel, (rear_n, front_n))
                        res[key] = ("追越冲突",
                                    f"两船=后船 {rear_n}({vr:.1f}km/h) 追 前船 {front_n}({vf:.1f}km/h) "
                                    f"距离={dist:.2f}km（阈值{self.overtake}km）")
        return res

    def recheck(self, channels, t_str, direction_change):
        """对受影响航道全量重检；只报告新出现的冲突（持续冲突不重复刷屏）。"""
        for ch in sorted(channels):
            current = self.conflicts(ch)
            for key in sorted(set(current) - self.active):
                kind, detail = current[key]
                note = "（改向事件级联重检发现）" if direction_change else ""
                self.error(t_str, kind, f"航道={ch} {detail}{note}")
            self.active = {k for k in self.active if k[1] != ch} | set(current)

    # ---------------- 输出 ----------------
    def error(self, t_str, category, message):
        self.errors.append((t_str, category, message))

    def report(self, out):
        w = out.write
        w("===== 裁决阈值与理由 =====\n")
        w(f"对向冲突阈值 {self.head_on} km：按限速12km/h、对向接近速度约24km/h 计，预留约12分钟避让窗口\n")
        w(f"追越冲突阈值 {self.overtake} km：同向速度差通常不超过6km/h，预留约20分钟反应余量\n")
        w(f"渔船占用判定 <{self.fish_speed} km/h：低于此速度视为作业/漂泊，不得滞留货轮分道\n")
        w(f"位置跳变上限 {self.max_speed} km/h：超出常规船舶物理极限即判数据异常\n")
        w(f"速度跳变阈值 {self.speed_jump} km/h：相邻事件推算速度突变超过此值判异常\n")

        w("\n===== 航行状态 =====\n")
        if not self.states:
            w("（无船舶状态）\n")
        else:
            w(f"{'船名':<10}{'类型':<6}{'航道':<10}{'分道':<6}{'位置km':>8}{'时刻':>8}{'推算速度km/h':>14}\n")
            for name in sorted(self.states):
                st = self.states[name]
                ship = self.ships[name]
                implied = f"{st.implied:.1f}" if st.implied is not None else "-"
                w(f"{ship.name:<10}{ship.kind:<6}{st.channel:<10}{st.lane:<6}"
                  f"{st.pos:>8.2f}{st.t_str:>8}{implied:>14}\n")

        w("\n===== 错误清单 =====\n")
        if not self.errors:
            w("（无错误）\n")
        for i, (t, cat, msg) in enumerate(self.errors, 1):
            w(f"{i:3d}. [{t}] {cat} | {msg}\n")

        w("\n===== 汇总 =====\n")
        counts: dict[str, int] = {}
        for _, cat, _ in self.errors:
            counts[cat] = counts.get(cat, 0) + 1
        w(f"船舶 {len(self.ships)} 艘，航道 {len({c for c, _ in self.lanes})} 条，"
          f"错误 {len(self.errors)} 条\n")
        for cat in sorted(counts):
            w(f"  {cat}: {counts[cat]} 条\n")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="海事交通组织裁决工具（纯标准库单文件）",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="输入文件（缺省读标准输入）")
    ap.add_argument("--head-on", type=float, default=5.0, help="对向冲突阈值 km（默认 5）")
    ap.add_argument("--overtake", type=float, default=2.0, help="追越冲突阈值 km（默认 2）")
    ap.add_argument("--fish-speed", type=float, default=4.0, help="渔船低速占用判定 km/h（默认 4）")
    ap.add_argument("--max-speed", type=float, default=40.0, help="位置跳变物理上限 km/h（默认 40）")
    ap.add_argument("--speed-jump", type=float, default=8.0, help="速度跳变阈值 km/h（默认 8）")
    args = ap.parse_args(argv)

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    eng = Engine(head_on=args.head_on, overtake=args.overtake,
                 fish_speed=args.fish_speed, max_speed=args.max_speed,
                 speed_jump=args.speed_jump)
    eng.load(lines)
    eng.report(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
