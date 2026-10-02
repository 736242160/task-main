#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fire_dispatch.py — 消防队出动模拟与校验工具（纯 Python 标准库，单文件）

用法:
    python3 fire_dispatch.py 输入文件     # 从文件读取指令流
    python3 fire_dispatch.py             # 从标准输入读取
    python3 fire_dispatch.py --demo      # 运行内置示例（覆盖全部错误类型）

输入格式（每行一条指令，# 之后为注释）:
    车    编号 类型(水罐|云梯|泡沫) 状态(可用|检修)
    站    名称 车辆编号...
    警情  名称 类型(火灾|救援|危化) 所需车辆类型...
    出动  警情名 站名 车辆编号...

约定:
    * 同名「警情」再次出现视为警情升级，会对已出动车辆做级联重检。
    * 同名「车」再次出现视为状态变更（可用于中途转检修/恢复可用）。
    * 状态跨出动指令延续：出动成功后车辆变为「出动中」并绑定警情。

车辆争抢规则（自定）:
    先派先占 —— 按出动流的时间顺序，先成功出动的警情占用车辆；
    后续警情争抢同一车辆时报错并拒绝该车辆。
    理由：警情按到达时序处理符合真实调度（先接警先处置），
    且避免后到的出动单悄悄抢走正在执行任务的车辆。

类型匹配规则:
    火灾 <- 水罐/泡沫    救援 <- 云梯    危化 <- 泡沫
"""

import sys
from collections import Counter
from dataclasses import dataclass, field

VEHICLE_TYPES = ("水罐", "云梯", "泡沫")
INCIDENT_TYPES = ("火灾", "救援", "危化")
VEHICLE_STATUS = ("可用", "检修")

MATCH_RULES = {
    "火灾": {"水罐", "泡沫"},
    "救援": {"云梯"},
    "危化": {"泡沫"},
}


@dataclass
class Vehicle:
    vid: str
    vtype: str
    status: str = "可用"      # 可用 / 检修 / 出动中
    incident: str = ""        # 出动中绑定的警情


@dataclass
class Station:
    name: str
    vehicles: list = field(default_factory=list)


@dataclass
class Incident:
    name: str
    itype: str
    required: list = field(default_factory=list)
    dispatched: list = field(default_factory=list)  # 已出动车辆编号（跨出动累积）


class Engine:
    def __init__(self):
        self.vehicles = {}
        self.stations = {}
        self.incidents = {}
        self.errors = []   # (行号, 类别, 消息)
        self.results = []  # 结果流水

    def error(self, lineno, category, msg):
        self.errors.append((lineno, category, msg))

    # ---------- 车 ----------
    def cmd_vehicle(self, lineno, args):
        if len(args) != 3:
            self.error(lineno, "格式错误", "车 指令需要 3 个参数：编号 类型 状态")
            return
        vid, vtype, status = args
        if vtype not in VEHICLE_TYPES:
            self.error(lineno, "格式错误",
                       f"车辆 {vid} 类型非法：{vtype}（应为 {'/'.join(VEHICLE_TYPES)}）")
            return
        if status not in VEHICLE_STATUS:
            self.error(lineno, "格式错误", f"车辆 {vid} 状态非法：{status}（应为 可用/检修）")
            return
        old = self.vehicles.get(vid)
        if old is None:
            self.vehicles[vid] = Vehicle(vid, vtype, status)
            return
        if old.vtype != vtype:
            self.error(lineno, "格式错误",
                       f"车辆 {vid} 重复定义且类型不一致：{old.vtype} vs {vtype}")
            return
        if old.status == "出动中":
            self.error(lineno, "状态冲突",
                       f"车辆 {vid} 正在出动（警情 {old.incident}），不能变更为 {status}")
            return
        old.status = status
        self.results.append(f"[行{lineno}] 车辆 {vid} 状态变更为 {status}")

    # ---------- 站 ----------
    def cmd_station(self, lineno, args):
        if len(args) < 2:
            self.error(lineno, "格式错误", "站 指令需要：名称 车辆编号...")
            return
        name, vids = args[0], args[1:]
        if name in self.stations:
            self.error(lineno, "格式错误", f"消防站 {name} 重复定义")
            return
        dups = [v for v, n in Counter(vids).items() if n > 1]
        if dups:
            self.error(lineno, "格式错误",
                       f"消防站 {name} 车辆列表含重复编号：{'、'.join(dups)}")
        self.stations[name] = Station(name, list(dict.fromkeys(vids)))

    # ---------- 警情（同名再现为升级） ----------
    def cmd_incident(self, lineno, args):
        if len(args) < 2:
            self.error(lineno, "格式错误", "警情 指令需要：名称 类型 [所需车辆类型...]")
            return
        name, itype, reqs = args[0], args[1], args[2:]
        if itype not in INCIDENT_TYPES:
            self.error(lineno, "格式错误", f"警情 {name} 类型非法：{itype}")
            return
        bad = [t for t in reqs if t not in VEHICLE_TYPES]
        if bad:
            self.error(lineno, "格式错误",
                       f"警情 {name} 所需车辆类型非法：{'、'.join(bad)}")
            return
        inc = self.incidents.get(name)
        if inc is None:
            self.incidents[name] = Incident(name, itype, reqs)
            self.results.append(
                f"[行{lineno}] 警情 {name}（{itype}）登记，需求：{' '.join(reqs) or '无'}")
            return
        old = f"{inc.itype}/需求:{' '.join(inc.required) or '无'}"
        inc.itype, inc.required = itype, reqs
        self.results.append(
            f"[行{lineno}] 警情 {name} 升级（原 {old} -> 新 {itype}/需求:{' '.join(reqs) or '无'}），"
            f"触发已出动车辆级联重检")
        self.recheck_incident(lineno, inc)

    def recheck_incident(self, lineno, inc):
        allowed = MATCH_RULES[inc.itype]
        for vid in inc.dispatched:
            v = self.vehicles[vid]
            if v.vtype not in allowed:
                self.error(lineno, "升级重检",
                           f"警情 {inc.name} 升级为 {inc.itype}，已出动车辆 {vid}（{v.vtype}）"
                           f"不再匹配，需重新调度")
        self.check_shortage(lineno, inc, category="升级重检")

    # ---------- 运力核查 ----------
    def check_shortage(self, lineno, inc, category="运力不足"):
        have = Counter(self.vehicles[vid].vtype for vid in inc.dispatched)
        short = False
        for t, n in Counter(inc.required).items():
            miss = n - have.get(t, 0)
            if miss > 0:
                short = True
                self.error(lineno, category,
                           f"警情 {inc.name} 缺少 {t} 车 × {miss}"
                           f"（需 {n}，已到 {have.get(t, 0)}）")
        return short

    # ---------- 出动 ----------
    def cmd_dispatch(self, lineno, args):
        if len(args) < 2:
            self.error(lineno, "格式错误", "出动 指令需要：警情名 站名 [车辆编号...]")
            return
        iname, sname, vids = args[0], args[1], args[2:]
        inc = self.incidents.get(iname)
        if inc is None:
            self.error(lineno, "引用错误", f"出动失败：警情 {iname} 不存在")
            self.results.append(f"[行{lineno}] 出动 {iname} @ {sname}：失败（警情不存在）")
            return
        st = self.stations.get(sname)
        if st is None:
            self.error(lineno, "引用错误", f"出动失败：消防站 {sname} 不存在")
            self.results.append(f"[行{lineno}] 出动 {iname} @ {sname}：失败（站不存在）")
            return

        accepted, rejected = [], 0
        seen = set()
        for vid in vids:
            if vid in seen:
                self.error(lineno, "重复出动", f"车辆 {vid} 在本次出动单内重复出现")
                rejected += 1
                continue
            seen.add(vid)
            v = self.vehicles.get(vid)
            if v is None:
                self.error(lineno, "引用错误", f"车辆 {vid} 不存在")
                rejected += 1
                continue
            if vid not in st.vehicles:
                self.error(lineno, "引用错误", f"车辆 {vid} 不属于消防站 {sname}")
                rejected += 1
                continue
            if v.status == "检修":
                self.error(lineno, "检修出动", f"车辆 {vid} 正在检修，不得出动")
                rejected += 1
                continue
            if v.status == "出动中":
                if v.incident == iname:
                    self.error(lineno, "重复出动",
                               f"车辆 {vid} 已出动至警情 {iname}，不能重复出动")
                else:
                    self.error(lineno, "车辆争抢",
                               f"车辆 {vid} 已出动至警情 {v.incident}，警情 {iname} 争抢失败"
                               f"（规则：先派先占）")
                rejected += 1
                continue
            if v.vtype not in MATCH_RULES[inc.itype]:
                self.error(lineno, "类型不匹配",
                           f"警情 {iname}（{inc.itype}）不接受 {v.vtype} 车 {vid}")
                rejected += 1
                continue
            accepted.append(v)

        for v in accepted:  # 级联更新：可用 -> 出动中，并绑定警情
            v.status = "出动中"
            v.incident = iname
            inc.dispatched.append(v.vid)

        short = self.check_shortage(lineno, inc)
        names = "、".join(v.vid for v in accepted) or "无"
        if not accepted:
            verdict = "失败"
        elif rejected or short:
            verdict = "部分出动"
        else:
            verdict = "成功"
        self.results.append(
            f"[行{lineno}] 出动 {iname} @ {sname}：{verdict}；本次出动车辆：{names}"
            + (f"；被拒 {rejected} 辆" if rejected else ""))

    # ---------- 主循环与报告 ----------
    def run(self, lines):
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            if cmd == "车":
                self.cmd_vehicle(lineno, args)
            elif cmd == "站":
                self.cmd_station(lineno, args)
            elif cmd == "警情":
                self.cmd_incident(lineno, args)
            elif cmd == "出动":
                self.cmd_dispatch(lineno, args)
            else:
                self.error(lineno, "格式错误", f"未知指令：{cmd}")

    def report(self):
        out = ["===== 出动结果 ====="]
        out.extend(self.results or ["（无）"])
        out.append("")
        out.append("===== 车辆最终状态 =====")
        for vid in sorted(self.vehicles):
            v = self.vehicles[vid]
            extra = f"（警情 {v.incident}）" if v.status == "出动中" else ""
            out.append(f"  {vid}  {v.vtype}  {v.status}{extra}")
        out.append("")
        out.append("===== 错误清单 =====")
        if not self.errors:
            out.append("（无错误）")
        else:
            for lineno, cat, msg in self.errors:
                out.append(f"[行{lineno}] 【{cat}】{msg}")
        return "\n".join(out)


DEMO = """\
# ---- 车辆定义：编号 类型 状态 ----
车 水罐1 水罐 可用
车 水罐2 水罐 可用
车 云梯1 云梯 可用
车 泡沫1 泡沫 可用
车 泡沫2 泡沫 检修
# ---- 消防站定义：名称 车辆列表 ----
站 东城站 水罐1 水罐2 云梯1
站 西城站 泡沫1 泡沫2
# ---- 警情流：名称 类型 所需车辆类型 ----
警情 仓库大火 火灾 水罐 水罐
警情 化工泄漏 危化 泡沫 泡沫
警情 城南车祸 救援 云梯
警情 高楼救人 救援 云梯
# ---- 出动流 ----
出动 仓库大火 东城站 水罐1 水罐2      # 成功，水罐1/2 级联变为出动中
出动 仓库大火 东城站 云梯1            # 类型不匹配（火灾不接受云梯），拒绝
出动 化工泄漏 西城站 泡沫1 泡沫2      # 泡沫2 检修不得出动；缺泡沫×1
出动 化工泄漏 西城站 泡沫1 泡沫1      # 单内重复 + 同警情重复出动
出动 城南车祸 东城站 云梯1            # 成功（云梯1 此前被拒仍可用）
出动 高楼救人 东城站 云梯1            # 争抢失败：云梯1 已出动至城南车祸
警情 仓库大火 危化 泡沫 泡沫          # 升级：重检水罐1/2 不再匹配，缺泡沫×2
出动 幻影警情 东城站 水罐1            # 警情不存在
出动 仓库大火 幽灵站 水罐1            # 站不存在
出动 城南车祸 东城站 云梯9            # 车辆不存在
"""


def main(argv):
    if "--demo" in argv:
        print("----- 示例输入 -----")
        print(DEMO)
        lines = DEMO.splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    eng = Engine()
    eng.run(lines)
    print(eng.report())


if __name__ == "__main__":
    main(sys.argv[1:])
