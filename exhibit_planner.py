#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
展览布展/撤展模拟工具（纯 Python 标准库，单文件）。

用法:
    python3 exhibit_planner.py [输入文件]      # 省略文件则从 stdin 读取

输入格式（行式文本，# 开头为注释，空行忽略）:
    展区 <名称> <容量> <承重> <热度等级>
    展品 <编号> <尺寸> <重量> <热度> <偏好区1,偏好区2,...>
    放 <展区> <展品>
    撤 <展区> <展品>

设计取舍（自定规则及理由）:
 1. 备选展区级联: 请求展区放不下时，按展品声明的偏好顺序依次尝试，
    第一个「剩余容量与剩余承重都够」的展区胜出。
    理由: 尊重展品方意愿、结果确定可复现、实现简单。
 2. 动线冲突: 同一展区内同时存在高热度(>=4)与低热度(<=2)展品即判定冲突
    （冷热观众动线互相干扰）。冲突时调走「新放入」的那件展品，
    理由: 先到先得不扰动既有布局，级联影响最小。
 3. 调位目标: 继续沿偏好顺序找可容纳区；目标区超限要报告；
    找不到任何可容纳区则留在原区并报「无法调位」。
 4. 超限报告: 容量/承重超限均给出展区、展品、超限量（超出多少）。
 5. 尺寸硬约束: 展品单件尺寸超过展区总容量，直接报错且不参与级联
    （结构性不可放，级联无意义，但仍会尝试能容纳它的备选区）。
 6. 撤展: 按展品实际所在展区级联恢复容量/承重；若操作指定的展区
    与实际不符，给出提示但仍按实际展区撤（状态一致性优先）。
 7. 跨操作状态延续: 所有操作共享同一状态机，顺序执行。
"""

import sys
from dataclasses import dataclass, field

HIGH_HEAT = 4   # 高热度阈值
LOW_HEAT = 2    # 低热度阈值


@dataclass
class Zone:
    name: str
    capacity: float
    max_weight: float
    heat: int
    used_size: float = 0.0
    used_weight: float = 0.0
    exhibits: list = field(default_factory=list)


@dataclass
class Exhibit:
    eid: str
    size: float
    weight: float
    heat: int
    prefs: list


class Planner:
    def __init__(self):
        self.zones = {}       # name -> Zone
        self.exhibits = {}    # eid -> Exhibit
        self.placed = {}      # eid -> zone name（跨操作延续的布展状态）
        self.errors = []

    # ---------- 报告 ----------
    def report(self, opno, kind, msg):
        self.errors.append("[操作%d] %s: %s" % (opno, kind, msg))

    # ---------- 定义 ----------
    def add_zone(self, opno, name, capacity, max_weight, heat):
        if name in self.zones:
            self.report(opno, "重复定义", "展区 '%s' 已存在，忽略新定义" % name)
            return
        self.zones[name] = Zone(name, capacity, max_weight, heat)

    def add_exhibit(self, opno, eid, size, weight, heat, prefs):
        if eid in self.exhibits:
            self.report(opno, "重复定义", "展品 '%s' 已存在，忽略新定义" % eid)
            return
        self.exhibits[eid] = Exhibit(eid, size, weight, heat, prefs)

    # ---------- 内部工具 ----------
    def fits(self, zone, ex):
        """展品放入该区后容量与承重是否都不超限。"""
        return (zone.used_size + ex.size <= zone.capacity and
                zone.used_weight + ex.weight <= zone.max_weight)

    def overflow_msg(self, zone, ex):
        """生成超限量描述（展区、展品、超限量）。"""
        parts = []
        over_cap = zone.used_size + ex.size - zone.capacity
        over_wt = zone.used_weight + ex.weight - zone.max_weight
        if over_cap > 0:
            parts.append("容量超限 %.2f（%s 放入后 %.2f/%.2f）"
                         % (over_cap, ex.eid, zone.used_size + ex.size, zone.capacity))
        if over_wt > 0:
            parts.append("承重超限 %.2f（%s 放入后 %.2f/%.2f）"
                         % (over_wt, ex.eid, zone.used_weight + ex.weight, zone.max_weight))
        return "；".join(parts)

    def has_flow_conflict(self, zone):
        """同区同时存在高热度与低热度展品 -> 动线冲突。"""
        heats = [self.exhibits[e].heat for e in zone.exhibits]
        return any(h >= HIGH_HEAT for h in heats) and any(h <= LOW_HEAT for h in heats)

    def do_place(self, zone, ex):
        zone.exhibits.append(ex.eid)
        zone.used_size += ex.size
        zone.used_weight += ex.weight
        self.placed[ex.eid] = zone.name

    def do_remove(self, zone, ex):
        zone.exhibits.remove(ex.eid)
        zone.used_size -= ex.size        # 级联恢复容量
        zone.used_weight -= ex.weight    # 级联恢复承重
        del self.placed[ex.eid]

    # ---------- 布展 ----------
    def place(self, opno, zname, eid):
        ex = self.exhibits.get(eid)
        if ex is None:
            self.report(opno, "引用错误", "展品 '%s' 不存在" % eid)
            return
        if zname not in self.zones:
            self.report(opno, "引用错误", "展区 '%s' 不存在" % zname)
            return
        if eid in self.placed:
            self.report(opno, "重复布展",
                        "展品 '%s' 已布置在展区 '%s'，忽略本次操作"
                        % (eid, self.placed[eid]))
            return

        # 候选顺序: 请求展区优先，其后按展品偏好级联（去重、剔除请求区）
        candidates = [zname] + [p for p in ex.prefs if p != zname]
        requested_overflow_reported = False

        for cand in candidates:
            zone = self.zones.get(cand)
            if zone is None:
                self.report(opno, "引用错误",
                            "展品 '%s' 的偏好展区 '%s' 不存在，跳过" % (eid, cand))
                continue
            if ex.size > zone.capacity:
                # 单件尺寸超展区总容量：结构性不可放
                self.report(opno, "尺寸超限",
                            "展品 '%s' 尺寸 %.2f 超过展区 '%s' 总容量 %.2f，跳过"
                            % (eid, ex.size, cand, zone.capacity))
                continue
            if not self.fits(zone, ex):
                # 只对「请求展区」与「调位目标」报超限，避免级联途中刷屏
                if cand == zname or not requested_overflow_reported:
                    self.report(opno, "超限",
                                "展区 '%s' 无法容纳展品 '%s'：%s"
                                % (cand, eid, self.overflow_msg(zone, ex)))
                    requested_overflow_reported = True
                continue

            # 找到可容纳区，执行放置
            self.do_place(zone, ex)
            if cand != zname:
                self.report(opno, "级联移区",
                            "展区 '%s' 不可用，展品 '%s' 按偏好级联移至备选展区 '%s'"
                            % (zname, eid, cand))
            # 放置后检查动线冲突，必要时级联调位
            self.resolve_conflict(opno, zone, ex, candidates, cand)
            return

        self.report(opno, "布展失败",
                    "展品 '%s' 在请求展区及所有备选展区均无法布置" % eid)

    def resolve_conflict(self, opno, zone, ex, candidates, current):
        """高低热度同区 -> 报告并级联调位新放入的展品。"""
        if not self.has_flow_conflict(zone):
            return
        self.report(opno, "动线冲突",
                    "展区 '%s' 内高热度与低热度展品并存（新放入 '%s' 热度 %d），尝试调位"
                    % (zone.name, ex.eid, ex.heat))

        # 调位目标: 偏好顺序中排在当前区之后的候选
        rest = candidates[candidates.index(current) + 1:]
        for cand in rest:
            target = self.zones.get(cand)
            if target is None:
                continue
            if ex.size > target.capacity:
                continue
            if not self.fits(target, ex):
                self.report(opno, "调位超限",
                            "调位目标展区 '%s' 无法容纳展品 '%s'：%s"
                            % (cand, ex.eid, self.overflow_msg(target, ex)))
                continue
            self.do_remove(zone, ex)      # 原区级联恢复
            self.do_place(target, ex)
            self.report(opno, "级联调位",
                        "展品 '%s' 因动线冲突由 '%s' 调至 '%s'"
                        % (ex.eid, zone.name, cand))
            return
        self.report(opno, "无法调位",
                    "展品 '%s' 无可用备选展区，保留在 '%s'（冲突仍存在）"
                    % (ex.eid, zone.name))

    # ---------- 撤展 ----------
    def remove(self, opno, zname, eid):
        if zname not in self.zones:
            self.report(opno, "引用错误", "展区 '%s' 不存在" % zname)
            return
        ex = self.exhibits.get(eid)
        if ex is None:
            self.report(opno, "引用错误", "展品 '%s' 不存在" % eid)
            return
        actual = self.placed.get(eid)
        if actual is None:
            self.report(opno, "撤展错误", "展品 '%s' 未在布展，无法撤展" % eid)
            return
        if actual != zname:
            self.report(opno, "撤展提示",
                        "展品 '%s' 实际在展区 '%s'（非指定的 '%s'），按实际展区撤展"
                        % (eid, actual, zname))
        self.do_remove(self.zones[actual], ex)

    # ---------- 输出 ----------
    def dump(self):
        out = ["===== 布展状态 ====="]
        for name in self.zones:
            z = self.zones[name]
            out.append("展区 %s | 容量 %.2f/%.2f | 承重 %.2f/%.2f | 热度 %d | 展品: %s"
                       % (name, z.used_size, z.capacity, z.used_weight, z.max_weight,
                          z.heat, ", ".join(z.exhibits) if z.exhibits else "（空）"))
        unplaced = [e for e in self.exhibits if e not in self.placed]
        if unplaced:
            out.append("未布展展品: " + ", ".join(unplaced))
        out.append("")
        out.append("===== 错误/事件报告（共 %d 条）=====" % len(self.errors))
        out.extend(self.errors if self.errors else ["（无）"])
        return "\n".join(out)


def parse_number(opno, planner, text, what):
    try:
        return float(text)
    except ValueError:
        planner.report(opno, "格式错误", "%s '%s' 不是数字" % (what, text))
        return None


def run(lines):
    planner = Planner()
    opno = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        opno += 1
        parts = line.split()
        head = parts[0]
        try:
            if head == "展区" and len(parts) == 5:
                cap = parse_number(opno, planner, parts[2], "容量")
                wt = parse_number(opno, planner, parts[3], "承重")
                ht = parse_number(opno, planner, parts[4], "热度")
                if None not in (cap, wt, ht):
                    planner.add_zone(opno, parts[1], cap, wt, int(ht))
            elif head == "展品" and len(parts) >= 5:
                size = parse_number(opno, planner, parts[2], "尺寸")
                wt = parse_number(opno, planner, parts[3], "重量")
                ht = parse_number(opno, planner, parts[4], "热度")
                prefs = parts[5].replace("，", ",").split(",") if len(parts) > 5 else []
                prefs = [p for p in (s.strip() for s in prefs) if p]
                if None not in (size, wt, ht):
                    planner.add_exhibit(opno, parts[1], size, wt, int(ht), prefs)
            elif head == "放" and len(parts) == 3:
                planner.place(opno, parts[1], parts[2])
            elif head == "撤" and len(parts) == 3:
                planner.remove(opno, parts[1], parts[2])
            else:
                planner.report(opno, "格式错误", "无法识别的指令: %s" % line)
        except Exception as exc:  # 单行出错不中断后续操作（跨操作状态延续）
            planner.report(opno, "内部错误", "%s: %s" % (line, exc))
    return planner


def main(argv):
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()
    planner = run(lines)
    print(planner.dump())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
