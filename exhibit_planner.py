#!/usr/bin/env python3
"""展览布展/撤展级联管理工具（纯 Python 标准库，单文件）。

用法：
    python3 exhibit_planner.py 脚本文件      # 从文件读取定义与布展流
    python3 exhibit_planner.py < 脚本文件    # 从标准输入读取
    python3 exhibit_planner.py --demo        # 运行内置演示场景（覆盖全部规则）

脚本格式（# 之后为注释，空行忽略）：
    zone    名称 容量 承重 热度等级          # 定义展区（亦可写中文关键字：展区）
    exhibit 编号 尺寸 重量 热度 偏好1,偏好2  # 定义展品（亦可写：展品；偏好可省略）
    place   展区 展品                        # 布展（亦可写：放）
    remove  展区 展品                        # 撤展（亦可写：撤）

设计取舍（自定规则及理由）：
1. 展区邻接：按定义顺序构成一条线性参观动线，下标相差 1 即相邻。
   理由：展厅多沿走廊串联，定义顺序即动线顺序，简单且可预期。
2. 热度冲突：热度 >= 4 视为高热度、<= 2 视为低热度；相邻展区内一高一低即判
   动线冲突（高热度展品吸引的人流会淹没隔壁低热度展品的参观空间）。
3. 偏好展区满时的级联：先按展品偏好列表依次尝试；偏好耗尽后，按“剩余容量
   最大者优先”挑选任意可容纳展区。理由：偏好列表表达策展意图应优先尊重；
   兜底选最空展区可均衡负载、减少碎片。
4. 冲突调位：移动热度较低的一方。理由：高热度展品是动线锚点（客流主要目的
   地），动它代价大；低热度展品让位损失最小。调位目标不能与冲突高热度展品
   所在展区相邻，且试放后不得产生新冲突（试放-校验-失败回滚）。
5. 超限报告：凡容量/承重不足即报告（展区、展品、超限量），随后继续级联；
   调位时目标展区超限同样报告并尝试下一备选。
6. 状态延续：展区已用容量/承重、展品落位表为全局状态，跨操作延续；撤展即
   扣减恢复。所有校验失败只报告、不破坏既有状态。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

HIGH_HEAT = 4  # 高热度阈值
LOW_HEAT = 2   # 低热度阈值


@dataclass
class Zone:
    name: str
    capacity: int   # 可容纳的总尺寸
    load: int       # 可承受的总重量
    heat: int       # 展区热度等级
    used_capacity: int = 0
    used_load: int = 0
    exhibits: list = field(default_factory=list)

    @property
    def free_capacity(self) -> int:
        return self.capacity - self.used_capacity

    @property
    def free_load(self) -> int:
        return self.load - self.used_load


@dataclass
class Exhibit:
    eid: str
    size: int
    weight: int
    heat: int
    prefs: list = field(default_factory=list)


class Museum:
    """布展全局状态：跨操作延续，所有指令在其上顺序执行。"""

    def __init__(self):
        self.zones = {}          # 名称 -> Zone
        self.zone_order = []     # 定义顺序 = 动线顺序（邻接关系）
        self.exhibits = {}       # 编号 -> Exhibit
        self.placement = {}      # 展品编号 -> 展区名称
        self.errors = []
        self.notes = []
        self.cur_tag = "定义"

    # ---------- 报告 ----------
    def err(self, msg):
        self.errors.append("[{}] {}".format(self.cur_tag, msg))

    def note(self, msg):
        self.notes.append("[{}] {}".format(self.cur_tag, msg))

    # ---------- 定义 ----------
    def define_zone(self, name, capacity, load, heat):
        if name in self.zones:
            self.err("展区重复定义：{}，忽略新定义".format(name))
            return
        self.zones[name] = Zone(name, capacity, load, heat)
        self.zone_order.append(name)

    def define_exhibit(self, eid, size, weight, heat, prefs):
        if eid in self.exhibits:
            self.err("展品重复定义：{}，忽略新定义".format(eid))
            return
        self.exhibits[eid] = Exhibit(eid, size, weight, heat, prefs)

    # ---------- 基础工具 ----------
    def adjacent_zones(self, zone_name):
        idx = self.zone_order.index(zone_name)
        for j in (idx - 1, idx + 1):
            if 0 <= j < len(self.zone_order):
                yield self.zones[self.zone_order[j]]

    @staticmethod
    def fits(zone, ex):
        return ex.size <= zone.free_capacity and ex.weight <= zone.free_load

    @staticmethod
    def overflow_detail(zone, ex):
        parts = []
        cap_over = zone.used_capacity + ex.size - zone.capacity
        load_over = zone.used_load + ex.weight - zone.load
        if cap_over > 0:
            parts.append("容量超限 {}".format(cap_over))
        if load_over > 0:
            parts.append("承重超限 {}".format(load_over))
        return "、".join(parts)

    def do_place(self, zone, ex):
        zone.exhibits.append(ex.eid)
        zone.used_capacity += ex.size
        zone.used_load += ex.weight
        self.placement[ex.eid] = zone.name

    def do_remove(self, zone, ex):
        zone.exhibits.remove(ex.eid)
        zone.used_capacity -= ex.size
        zone.used_load -= ex.weight
        del self.placement[ex.eid]

    def candidate_zones(self, ex, first=None):
        """级联候选顺序：指定展区 -> 偏好列表 -> 其余展区（剩余容量大者优先）。"""
        ordered = []

        def add(name):
            if name and name not in ordered:
                ordered.append(name)

        add(first)
        for p in ex.prefs:
            add(p)
        rest = sorted(self.zones.values(),
                      key=lambda z: (z.free_capacity, z.free_load), reverse=True)
        for z in rest:
            add(z.name)
        return ordered

    def conflicts_in(self, zone):
        """该展区与相邻展区之间的高低热度冲突对 (本区, 本区展品, 邻区, 邻区展品)。"""
        result = []
        for az in self.adjacent_zones(zone.name):
            for eid1 in zone.exhibits:
                e1 = self.exhibits[eid1]
                for eid2 in az.exhibits:
                    e2 = self.exhibits[eid2]
                    hi, lo = (e1, e2) if e1.heat >= e2.heat else (e2, e1)
                    if hi.heat >= HIGH_HEAT and lo.heat <= LOW_HEAT:
                        result.append((zone, e1, az, e2))
        return result

    # ---------- 布展 ----------
    def cmd_place(self, zone_name, eid):
        zone = self.zones.get(zone_name)
        if zone is None:
            self.err("布展失败：展区不存在：{}（展品 {}）".format(zone_name, eid))
            return
        ex = self.exhibits.get(eid)
        if ex is None:
            self.err("布展失败：展品不存在：{}（展区 {}）".format(eid, zone_name))
            return
        if eid in self.placement:
            self.err("重复布展：展品 {} 已在展区 {}，本次对 {} 的布展被忽略".format(
                eid, self.placement[eid], zone_name))
            return
        if ex.size > zone.capacity:
            self.err("展品尺寸超限：展品 {} 尺寸 {} 大于展区 {} 容量 {}".format(
                eid, ex.size, zone_name, zone.capacity))

        placed_zone = None
        for cand in self.candidate_zones(ex, zone_name):
            cz = self.zones.get(cand)
            if cz is None:
                self.err("偏好展区不存在：{}（展品 {} 的偏好），跳过".format(cand, eid))
                continue
            if ex.size > cz.capacity:
                if cand != zone_name:
                    self.err("展品尺寸超限：展品 {} 尺寸 {} 大于备选展区 {} 容量 {}，跳过".format(
                        eid, ex.size, cand, cz.capacity))
                continue
            if self.fits(cz, ex):
                placed_zone = cz
                break
            if cand == zone_name:
                self.err("展区超限：展区 {} 无法容纳展品 {}（{}），尝试级联备选展区".format(
                    cand, eid, self.overflow_detail(cz, ex)))

        if placed_zone is None:
            self.err("布展失败：无任何展区可容纳展品 {}".format(eid))
            return
        if placed_zone.name != zone_name:
            self.note("级联移区：展品 {} 由 {} 移至备选展区 {}".format(
                eid, zone_name, placed_zone.name))
        self.do_place(placed_zone, ex)
        self.resolve_conflicts(placed_zone)

    # ---------- 冲突级联调位 ----------
    def resolve_conflicts(self, zone):
        for z, e1, az, e2 in list(self.conflicts_in(zone)):
            # 之前的调位可能已消除该冲突
            if e1.eid not in z.exhibits or e2.eid not in az.exhibits:
                continue
            hi, lo = (e1, e2) if e1.heat >= e2.heat else (e2, e1)
            hi_zone = z if hi is e1 else az
            lo_zone = az if hi is e1 else z
            self.err("动线冲突：高热度展品 {}（热度{}，展区 {}）与低热度展品 {}（热度{}，展区 {}）相邻".format(
                hi.eid, hi.heat, hi_zone.name, lo.eid, lo.heat, lo_zone.name))
            self.relocate(lo, lo_zone, avoid_zone=hi_zone)

    def relocate(self, ex, from_zone, avoid_zone):
        """把低热度展品级联调位：优先其偏好列表，兜底最空展区；
        目标不得与 avoid_zone 相邻，且不得引入新冲突。"""
        for cand in self.candidate_zones(ex):
            if cand == from_zone.name or cand == avoid_zone.name:
                continue
            cz = self.zones.get(cand)
            if cz is None:
                continue
            if cz in self.adjacent_zones(avoid_zone.name):
                continue  # 调过去仍然相邻，无法消除冲突
            if ex.size > cz.capacity:
                continue
            if not self.fits(cz, ex):
                self.err("调位目标超限：展区 {} 无法容纳调位展品 {}（{}），尝试下一备选".format(
                    cand, ex.eid, self.overflow_detail(cz, ex)))
                continue
            # 试放 -> 校验是否产生新冲突 -> 失败则回滚
            self.do_remove(from_zone, ex)
            self.do_place(cz, ex)
            if self.conflicts_in(cz):
                self.do_remove(cz, ex)
                self.do_place(from_zone, ex)
                continue
            self.note("级联调位：展品 {} 由 {} 移至 {} 以消除动线冲突".format(
                ex.eid, from_zone.name, cz.name))
            return True
        self.err("调位失败：展品 {} 无可用展区，冲突保留".format(ex.eid))
        return False

    # ---------- 撤展 ----------
    def cmd_remove(self, zone_name, eid):
        zone = self.zones.get(zone_name)
        if zone is None:
            self.err("撤展失败：展区不存在：{}（展品 {}）".format(zone_name, eid))
            return
        ex = self.exhibits.get(eid)
        if ex is None:
            self.err("撤展失败：展品不存在：{}（展区 {}）".format(eid, zone_name))
            return
        actual = self.placement.get(eid)
        if actual is None:
            self.err("撤展失败：展品 {} 未布展".format(eid))
            return
        if actual != zone_name:
            self.err("撤展失败：展品 {} 实际位于展区 {}，而非 {}".format(eid, actual, zone_name))
            return
        self.do_remove(zone, ex)
        self.note("撤展完成：展品 {} 已撤出 {}，容量/承重已级联恢复".format(eid, zone_name))


# ---------- 脚本解析 ----------
def run_script(museum, lines):
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        museum.cur_tag = "行{}".format(lineno)
        parts = line.split()
        head, args = parts[0].lower(), parts[1:]
        try:
            if head in ("zone", "展区"):
                museum.define_zone(args[0], int(args[1]), int(args[2]), int(args[3]))
            elif head in ("exhibit", "展品"):
                prefs = []
                if len(args) > 4:
                    prefs = [p for p in ",".join(args[4:]).replace("，", ",").split(",") if p]
                museum.define_exhibit(args[0], int(args[1]), int(args[2]), int(args[3]), prefs)
            elif head in ("place", "放"):
                museum.cmd_place(args[0], args[1])
            elif head in ("remove", "撤"):
                museum.cmd_remove(args[0], args[1])
            else:
                museum.err("无法识别的指令：{}".format(head))
        except (IndexError, ValueError) as exc:
            museum.err("指令解析失败：{}（{}）".format(line, exc))


# ---------- 输出 ----------
def render(museum):
    out = ["=== 布展状态 ==="]
    for name in museum.zone_order:
        z = museum.zones[name]
        exs = "、".join(z.exhibits) if z.exhibits else "（空）"
        out.append("展区 {}（热度{}）：容量 {}/{}，承重 {}/{}，展品：{}".format(
            z.name, z.heat, z.used_capacity, z.capacity,
            z.used_load, z.load, exs))
    out.append("")
    out.append("=== 级联调整记录 ===")
    out.extend(museum.notes or ["（无）"])
    out.append("")
    out.append("=== 错误报告 ===")
    out.extend(museum.errors or ["（无）"])
    return "\n".join(out)


DEMO = """
# 展区：名称 容量 承重 热度等级（定义顺序即动线顺序，相邻下标互为邻区）
zone A厅 4 1000 5
zone B厅 3 800 3
zone C厅 2 400 1
zone D厅 3 600 2

# 展品：编号 尺寸 重量 热度 区域偏好（逗号分隔，可省略）
exhibit E1 2 300 5 A厅
exhibit E2 1 100 1 A厅
exhibit E3 2 300 4 A厅,B厅
exhibit E4 2 200 2 B厅
exhibit E5 5 100 3 A厅
exhibit E6 1 100 2 B厅
exhibit E7 1 900 3 C厅

place A厅 E1     # 正常布展
place A厅 E2     # 正常布展（同区不判冲突）
place A厅 E3     # A厅容量不足 -> 报告超限 -> 级联到偏好 B厅 -> 与 A厅 E2 高低热度相邻 -> 报告冲突 -> E2 级联调位到 D厅
place B厅 E4     # B厅容量不足 -> 报告超限 -> 级联兜底到 D厅
place C厅 E7     # C厅承重不足 -> 报告超限 -> 所有展区承重都不够 -> 布展失败
place A厅 E1     # 重复布展 -> 报告
place X厅 E1     # 展区不存在 -> 报告
place A厅 E9     # 展品不存在 -> 报告
place A厅 E5     # 尺寸超 A厅容量 -> 报告；所有备选也放不下 -> 布展失败
place B厅 E6     # 与 A厅 E1 高低热度相邻 -> 报告冲突 -> 无合适展区可调 -> 调位失败
remove A厅 E1    # 正常撤展 -> A厅容量/承重级联恢复（同时消除 E6 的冲突）
remove A厅 E1    # 已撤 -> 报告未布展
remove B厅 E4    # E4 实际在 D厅 -> 报告位置不符
"""


def main(argv):
    museum = Museum()
    if "--demo" in argv:
        lines = DEMO.strip().splitlines()
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    run_script(museum, lines)
    print(render(museum))
    return 1 if museum.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
