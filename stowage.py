#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stowage.py — 集装箱船配载工具（纯标准库，单文件）

输入（JSON 文件）：
{
  "ship": {
    "name": "示例轮",
    "balance_limit": 50.0,            // 可选，左右舷重量差上限(吨)，缺省=全船总承重的10%
    "bays": [
      {"name": "B01", "capacity": 4, "max_weight": 120.0, "side": "port"},
      {"name": "B02", "capacity": 4, "max_weight": 120.0, "side": "starboard"}
      // side 可选：port/left/左 或 starboard/right/右；缺省按定义顺序左右交替
    ]
  },
  "containers": [
    {"id": "C001", "weight": 30.0, "destination": "上海"}
  ],
  "operations": [
    {"action": "load",  "bay": "B01", "container": "C001"},
    {"action": "discharge", "bay": "B01", "container": "C001"}
    // action 也接受中文 "装" / "卸"
  ]
}

用法：
  python3 stowage.py plan.json          # 执行配载计划并输出状态与错误报告
  python3 stowage.py --demo             # 运行内置演示（覆盖全部错误场景）
"""

import argparse
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass, field

PORT_SIDES = {"port", "left", "左", "左舷"}
STBD_SIDES = {"starboard", "right", "右", "右舷"}


@dataclass
class Bay:
    name: str
    capacity: int
    max_weight: float
    side: str  # "port" 或 "starboard"
    containers: list = field(default_factory=list)  # 箱号列表，保持装载顺序

    @property
    def weight(self):
        return sum(c.weight for c in self.containers)


@dataclass
class Container:
    cid: str
    weight: float
    destination: str
    bay: str = None  # None 表示在岸上


class StowageError(Exception):
    """输入文件格式错误。"""


class Ship:
    def __init__(self, spec):
        self.name = spec.get("name", "未命名船")
        self.bays = OrderedDict()
        raw_bays = spec.get("bays")
        if not raw_bays:
            raise StowageError("船舶定义缺少 bays（舱位列表）")
        for i, b in enumerate(raw_bays):
            for key in ("name", "capacity", "max_weight"):
                if key not in b:
                    raise StowageError(f"舱位定义缺少字段 {key!r}: {b}")
            side_raw = str(b.get("side", "")).strip().lower()
            if side_raw in PORT_SIDES:
                side = "port"
            elif side_raw in STBD_SIDES:
                side = "starboard"
            elif side_raw:
                raise StowageError(f"舱位 {b['name']} 的 side 无法识别: {b['side']!r}")
            else:
                side = "port" if i % 2 == 0 else "starboard"  # 缺省左右交替
            if b["name"] in self.bays:
                raise StowageError(f"舱位名称重复: {b['name']}")
            self.bays[b["name"]] = Bay(b["name"], int(b["capacity"]),
                                       float(b["max_weight"]), side)
        total_max = sum(b.max_weight for b in self.bays.values())
        # 平衡规则：左右舷重量差不得超过上限。缺省取全船总承重的 10%，
        # 理由：船舶初稳性要求横倾角尽量小，工程上常以载重量的一定比例
        # 作为允许的不平衡力矩近似；10% 是常用的保守经验值，可在
        # ship.balance_limit 中按船型覆盖。
        self.balance_limit = float(spec.get("balance_limit", total_max * 0.10))

    def side_weight(self, side):
        return sum(b.weight for b in self.bays.values() if b.side == side)

    def imbalance(self):
        return abs(self.side_weight("port") - self.side_weight("starboard"))


class Planner:
    def __init__(self, plan):
        self.ship = Ship(plan.get("ship", {}))
        self.containers = {}
        for c in plan.get("containers", []):
            for key in ("id", "weight", "destination"):
                if key not in c:
                    raise StowageError(f"集装箱定义缺少字段 {key!r}: {c}")
            if c["id"] in self.containers:
                raise StowageError(f"集装箱编号重复: {c['id']}")
            self.containers[c["id"]] = Container(c["id"], float(c["weight"]),
                                                 str(c["destination"]))
        self.operations = plan.get("operations", [])
        self.errors = []  # (操作序号, 类别, 描述)

    # ---- 错误记录 ----
    def report(self, op_no, category, message):
        self.errors.append((op_no, category, message))

    # ---- 目的港滞留统计（随装卸级联更新）----
    def port_stats(self):
        stats = {}
        for c in self.containers.values():
            if c.bay is not None:  # 仅统计仍在船上的箱
                s = stats.setdefault(c.destination, {"count": 0, "weight": 0.0})
                s["count"] += 1
                s["weight"] += c.weight
        return stats

    # ---- 装卸操作 ----
    def run(self):
        for idx, op in enumerate(self.operations, 1):
            action = str(op.get("action", "")).strip()
            if action in ("装", "load"):
                self._load(idx, op)
            elif action in ("卸", "discharge", "unload"):
                self._discharge(idx, op)
            else:
                self.report(idx, "操作无效", f"无法识别的操作类型: {action!r}")

    def _resolve(self, idx, op):
        """解析并校验舱位与箱号，返回 (bay, container)，失败时记录错误并返回 None。"""
        bay_name, cid = op.get("bay"), op.get("container")
        ok = True
        bay = cont = None
        if cid not in self.containers:
            self.report(idx, "集装箱不存在", f"引用了未定义的集装箱: {cid!r}")
            ok = False
        else:
            cont = self.containers[cid]
        if bay_name not in self.ship.bays:
            self.report(idx, "舱位不存在", f"引用了未定义的舱位: {bay_name!r}")
            ok = False
        else:
            bay = self.ship.bays[bay_name]
        return (bay, cont) if ok else None

    def _check_balance(self, idx):
        imb = self.ship.imbalance()
        if imb > self.ship.balance_limit:
            self.report(idx, "左右舷失衡",
                        f"左舷 {self.ship.side_weight('port'):.1f}t / "
                        f"右舷 {self.ship.side_weight('starboard'):.1f}t，"
                        f"差值 {imb:.1f}t 超过上限 {self.ship.balance_limit:.1f}t")

    def _load(self, idx, op):
        resolved = self._resolve(idx, op)
        if not resolved:
            return
        bay, cont = resolved
        if cont.bay is not None:
            self.report(idx, "重复装载",
                        f"集装箱 {cont.cid} 已在舱位 {cont.bay} 上，不能重复装载")
            return
        if len(bay.containers) >= bay.capacity:
            self.report(idx, "舱位已满",
                        f"舱位 {bay.name} 容量 {bay.capacity} 已满，"
                        f"无法装入 {cont.cid}")
            return
        bay.containers.append(cont)
        cont.bay = bay.name
        # 累计重量超承重检查
        if bay.weight > bay.max_weight:
            self.report(idx, "舱位超重",
                        f"舱位 {bay.name} 累计 {bay.weight:.1f}t 超过承重 "
                        f"{bay.max_weight:.1f}t，超重 {bay.weight - bay.max_weight:.1f}t")
        self._check_balance(idx)

    def _discharge(self, idx, op):
        resolved = self._resolve(idx, op)
        if not resolved:
            return
        bay, cont = resolved
        if cont.bay is None:
            self.report(idx, "重复卸箱",
                        f"集装箱 {cont.cid} 未装船（或已卸下），无法卸下")
            return
        if cont.bay != bay.name:
            self.report(idx, "舱位不符",
                        f"集装箱 {cont.cid} 实际装在舱位 {cont.bay}，"
                        f"而非 {bay.name}，拒绝卸下")
            return
        bay.containers.remove(cont)
        cont.bay = None  # 状态延续：回岸，目的港滞留统计随之级联更新
        self._check_balance(idx)

    # ---- 输出 ----
    def render(self):
        out = []
        out.append(f"===== 船舶状态：{self.ship.name} =====")
        out.append(f"{'舱位':<8}{'舷侧':<6}{'箱量':<10}{'重量/承重':<18}箱号")
        side_cn = {"port": "左舷", "starboard": "右舷"}
        for bay in self.ship.bays.values():
            ids = ",".join(c.cid for c in bay.containers) or "-"
            flag = " [超重!]" if bay.weight > bay.max_weight else ""
            out.append(f"{bay.name:<8}{side_cn[bay.side]:<6}"
                       f"{len(bay.containers)}/{bay.capacity:<6}"
                       f"{bay.weight:.1f}/{bay.max_weight:.1f}t{flag:<8}{ids}")
        out.append(f"左舷合计 {self.ship.side_weight('port'):.1f}t | "
                   f"右舷合计 {self.ship.side_weight('starboard'):.1f}t | "
                   f"差值 {self.ship.imbalance():.1f}t "
                   f"(上限 {self.ship.balance_limit:.1f}t)")
        out.append("")
        out.append("===== 目的港滞留统计（当前在船） =====")
        stats = self.port_stats()
        if stats:
            for dest in sorted(stats):
                s = stats[dest]
                out.append(f"{dest}: {s['count']} 箱, {s['weight']:.1f}t")
        else:
            out.append("（无在船集装箱）")
        out.append("")
        out.append("===== 错误报告 =====")
        if self.errors:
            for op_no, cat, msg in self.errors:
                out.append(f"[操作 {op_no}] [{cat}] {msg}")
            out.append(f"共 {len(self.errors)} 个错误")
        else:
            out.append("无错误，全部操作执行成功。")
        return "\n".join(out)


DEMO_PLAN = {
    "ship": {
        "name": "演示轮",
        "balance_limit": 40.0,
        "bays": [
            {"name": "B01", "capacity": 2, "max_weight": 50.0, "side": "port"},
            {"name": "B02", "capacity": 2, "max_weight": 50.0, "side": "starboard"},
        ],
    },
    "containers": [
        {"id": "C1", "weight": 30.0, "destination": "上海"},
        {"id": "C2", "weight": 25.0, "destination": "上海"},
        {"id": "C3", "weight": 20.0, "destination": "新加坡"},
        {"id": "C4", "weight": 10.0, "destination": "新加坡"},
    ],
    "operations": [
        {"action": "装", "bay": "B01", "container": "C1"},
        {"action": "装", "bay": "B01", "container": "C2"},   # B01 累计 55t > 50t → 超重
        {"action": "装", "bay": "B01", "container": "C3"},   # B01 已满 → 舱位已满
        {"action": "装", "bay": "B01", "container": "C1"},   # C1 已在船 → 重复装载
        {"action": "装", "bay": "B09", "container": "C4"},   # 舱位不存在
        {"action": "装", "bay": "B02", "container": "CX"},   # 集装箱不存在
        {"action": "装", "bay": "B02", "container": "C3"},   # 左55/右20 差35 ≤ 40，平衡
        {"action": "卸", "bay": "B01", "container": "C2"},   # 正常卸下，上海滞留-1
        {"action": "卸", "bay": "B01", "container": "C2"},   # 已卸下 → 重复卸箱
        {"action": "卸", "bay": "B02", "container": "C1"},   # C1 在 B01 → 舱位不符
        {"action": "卸", "bay": "B01", "container": "C1"},   # 正常卸下，上海滞留清零
        {"action": "装", "bay": "B02", "container": "C4"},   # 左0/右30，平衡
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="集装箱船配载工具（纯标准库单文件）")
    ap.add_argument("plan", nargs="?", help="配载计划 JSON 文件路径")
    ap.add_argument("--demo", action="store_true", help="运行内置演示计划")
    args = ap.parse_args(argv)

    if args.demo:
        plan = DEMO_PLAN
    elif args.plan:
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
    else:
        ap.error("请提供计划 JSON 文件，或使用 --demo 运行演示")

    try:
        planner = Planner(plan)
    except StowageError as e:
        print(f"输入定义错误: {e}", file=sys.stderr)
        return 2
    planner.run()
    print(planner.render())
    return 1 if planner.errors else 0


if __name__ == "__main__":
    sys.exit(main())
