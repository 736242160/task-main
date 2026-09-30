#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集装箱船配载校验工具（纯 Python 标准库，单文件）。

功能：
  - 校验装卸流：不存在的舱位/集装箱、重复装卸、卸未装、舱满继续装；
  - 累计装重量超舱位最大承重（报告 舱位 + 超重值）；
  - 全船左右舷重量差（横倾）超限校验；
  - 目的港在箱量随卸下操作级联更新；
  - 跨操作状态延续，最终输出船舶状态 + 错误清单。

用法：
  python3 stowage.py plan.json          # 校验一个配载计划
  python3 stowage.py --demo             # 运行内置演示
  echo '{"bays":...}' | python3 stowage.py -   # 从标准输入读 JSON

输入 JSON 格式：
{
  "bays": [
    {"name": "B01-P", "capacity": 2, "max_weight": 50.0},
    {"name": "B01-S", "capacity": 2, "max_weight": 50.0}
  ],
  "containers": [
    {"id": "C001", "weight": 25.0, "dest": "SHA"},
    {"id": "C002", "weight": 30.0, "dest": "SIN"}
  ],
  "operations": [
    {"op": "load",   "bay": "B01-P", "container": "C001"},
    {"op": "unload", "bay": "B01-P", "container": "C001"}
  ]
}

舷侧判定规则：
  舱位名以 "P" 结尾（或包含“左”）视为左舷(port)；以 "S" 结尾（或包含“右”）
  视为右舷(starboard)；未标记的舱位按定义顺序奇偶交替分配（0->左, 1->右 ...）。

横倾（左右舷重量差）规则与理由：
  |W左 - W右| > max(50.0, 0.10 * 当前在船总重) 即判定超限。
  取“当前总重 10%”作为相对限值，依据是船舶横倾角与两侧重量矩差成正比，
  船越重可承受的绝对偏差越大；同时设 50 吨绝对下限，避免轻载/空载时
  单个箱子就触发误报。阈值可在 HEEL_MIN_TONS / HEEL_RATIO 中调整。

错误处理策略：
  - 拒绝型错误（舱位/箱不存在、舱满、重复装、卸未装、卸错舱、操作类型非法）：
    该操作不执行，状态不变；
  - 报告型错误（舱位超重、左右舷差超限）：操作照常执行并记录，
    以便观察后续操作能否把状态修正回来（如再装另一侧）。
"""

import json
import sys
from collections import OrderedDict

HEEL_MIN_TONS = 50.0   # 左右舷重量差绝对下限（吨）
HEEL_RATIO = 0.10      # 左右舷重量差占当前在船总重的比例上限


def _side_of(name, index):
    """根据舱位名/顺序判定左右舷。"""
    upper = name.upper()
    if upper.endswith("P") or "左" in name:
        return "P"
    if upper.endswith("S") or "右" in name:
        return "S"
    return "P" if index % 2 == 0 else "S"


class Bay:
    __slots__ = ("name", "capacity", "max_weight", "side", "containers", "weight")

    def __init__(self, name, capacity, max_weight, side):
        self.name = name
        self.capacity = int(capacity)
        self.max_weight = float(max_weight)
        self.side = side
        self.containers = []   # 按装入顺序保存箱号
        self.weight = 0.0


class StowagePlanner:
    def __init__(self, ship_def):
        self.bays = OrderedDict()
        for i, b in enumerate(ship_def.get("bays", [])):
            name = str(b["name"])
            side = b.get("side") or _side_of(name, i)
            if side not in ("P", "S"):
                raise ValueError("舱位 %s 的 side 只能是 P 或 S" % name)
            self.bays[name] = Bay(name, b["capacity"], b["max_weight"], side)

        self.containers = {}   # 箱号 -> (重量, 目的港)
        for c in ship_def.get("containers", []):
            cid = str(c["id"])
            if cid in self.containers:
                raise ValueError("集装箱编号重复定义: %s" % cid)
            self.containers[cid] = (float(c["weight"]), str(c["dest"]))

        self.location = {}     # 箱号 -> 所在舱位名（在船才有）
        self.port_remaining = {}  # 目的港 -> 当前在船箱量（级联统计）
        self.errors = []

    # ---- 状态查询 ----

    def _side_weights(self):
        wp = ws = 0.0
        for bay in self.bays.values():
            if bay.side == "P":
                wp += bay.weight
            else:
                ws += bay.weight
        return wp, ws

    def _check_heel(self, op_index):
        wp, ws = self._side_weights()
        total = wp + ws
        if total <= 0:
            return
        limit = max(HEEL_MIN_TONS, HEEL_RATIO * total)
        diff = abs(wp - ws)
        if diff > limit:
            heavy = "左舷" if wp > ws else "右舷"
            self._record(op_index, "HEEL_EXCEEDED",
                         "左右舷重量差 %.2f 吨超过限值 %.2f 吨"
                         "（左舷 %.2f / 右舷 %.2f，%s偏重）"
                         % (diff, limit, wp, ws, heavy), False)

    def _record(self, op_index, etype, message, reject):
        self.errors.append({
            "op": op_index,
            "type": etype,
            "rejected": reject,
            "message": message,
        })

    # ---- 操作执行 ----

    def run(self, operations):
        for i, raw in enumerate(operations):
            action = str(raw.get("op", "")).lower()
            bay_name = raw.get("bay")
            cid = str(raw.get("container", ""))

            if action not in ("load", "unload"):
                self._record(i, "INVALID_OP",
                             "未知操作类型 %r（只支持 load/unload）" % action, True)
                continue
            if bay_name not in self.bays:
                self._record(i, "BAY_NOT_FOUND",
                             "%s 操作引用了不存在的舱位 %r（箱 %s）"
                             % (action, bay_name, cid), True)
                continue
            if cid not in self.containers:
                self._record(i, "CONTAINER_NOT_FOUND",
                             "%s 操作引用了不存在的集装箱 %r" % (action, cid), True)
                continue

            if action == "load":
                self._load(i, self.bays[bay_name], cid)
            else:
                self._unload(i, self.bays[bay_name], cid)
        return self.report()

    def _load(self, op_index, bay, cid):
        weight, dest = self.containers[cid]

        if cid in self.location:
            self._record(op_index, "DUPLICATE_LOAD",
                         "集装箱 %s 已在舱位 %s，不能重复装船"
                         % (cid, self.location[cid]), True)
            return
        if len(bay.containers) >= bay.capacity:
            self._record(op_index, "BAY_FULL",
                         "舱位 %s 已满（容量 %d），集装箱 %s 无法装入"
                         % (bay.name, bay.capacity, cid), True)
            return

        # 执行装船
        bay.containers.append(cid)
        bay.weight += weight
        self.location[cid] = bay.name
        self.port_remaining[dest] = self.port_remaining.get(dest, 0) + 1

        # 报告型检查：超重 + 横倾
        if bay.weight > bay.max_weight:
            self._record(op_index, "BAY_OVERWEIGHT",
                         "舱位 %s 累计装重量 %.2f 吨超过最大承重 %.2f 吨，超重 %.2f 吨"
                         % (bay.name, bay.weight, bay.max_weight,
                            bay.weight - bay.max_weight), False)
        self._check_heel(op_index)

    def _unload(self, op_index, bay, cid):
        weight, dest = self.containers[cid]

        if cid not in self.location:
            self._record(op_index, "UNLOAD_NOT_LOADED",
                         "集装箱 %s 当前不在船上，不能卸下（重复卸或从未装船）" % cid,
                         True)
            return
        if self.location[cid] != bay.name:
            self._record(op_index, "UNLOAD_WRONG_BAY",
                         "集装箱 %s 实际在舱位 %s，不能从 %s 卸下"
                         % (cid, self.location[cid], bay.name), True)
            return

        # 执行卸船，目的港在船箱量级联 -1
        bay.containers.remove(cid)
        bay.weight -= weight
        del self.location[cid]
        remaining = self.port_remaining.get(dest, 0) - 1
        if remaining <= 0:
            self.port_remaining.pop(dest, None)
        else:
            self.port_remaining[dest] = remaining

        self._check_heel(op_index)

    # ---- 报告 ----

    def report(self):
        bays_state = OrderedDict()
        for name, b in self.bays.items():
            bays_state[name] = {
                "side": "左舷" if b.side == "P" else "右舷",
                "used_slots": len(b.containers),
                "capacity": b.capacity,
                "weight": round(b.weight, 3),
                "max_weight": b.max_weight,
                "containers": list(b.containers),
            }
        wp, ws = self._side_weights()
        return {
            "bays": bays_state,
            "onboard_total_weight": round(wp + ws, 3),
            "side_weight": {"port_P": round(wp, 3), "starboard_S": round(ws, 3)},
            "onboard_count": len(self.location),
            "port_remaining": dict(sorted(self.port_remaining.items())),
            "errors": self.errors,
        }


# ---------------- 内置演示 / CLI ----------------

DEMO_PLAN = {
    "bays": [
        {"name": "B01-P", "capacity": 2, "max_weight": 60.0},
        {"name": "B01-S", "capacity": 2, "max_weight": 60.0},
        {"name": "B02-P", "capacity": 2, "max_weight": 60.0},
        {"name": "B02-S", "capacity": 2, "max_weight": 60.0},
    ],
    "containers": [
        {"id": "C001", "weight": 40.0, "dest": "SHA"},
        {"id": "C002", "weight": 35.0, "dest": "SIN"},
        {"id": "C003", "weight": 30.0, "dest": "SHA"},
        {"id": "C004", "weight": 30.0, "dest": "SIN"},
    ],
    "operations": [
        {"op": "load", "bay": "B01-P", "container": "C001"},
        {"op": "load", "bay": "B01-P", "container": "C002"},   # 超重 15t
        {"op": "load", "bay": "B01-P", "container": "C003"},   # 舱满拒绝
        {"op": "load", "bay": "B99",   "container": "C003"},   # 舱不存在
        {"op": "load", "bay": "B01-S", "container": "C999"},   # 箱不存在
        {"op": "load", "bay": "B01-P", "container": "C001"},   # 重复装
        {"op": "load", "bay": "B02-S", "container": "C003"},
        {"op": "load", "bay": "B02-S", "container": "C004"},   # 均衡右舷重量
        {"op": "unload", "bay": "B02-S", "container": "C003"}, # SHA 2->1，级联更新
        {"op": "unload", "bay": "B02-S", "container": "C003"}, # 重复卸
        {"op": "unload", "bay": "B01-P", "container": "C004"}, # 不在该舱
    ],
}


def render_text(report):
    lines = []
    lines.append("=" * 64)
    lines.append("船舶最终状态")
    lines.append("=" * 64)
    for name, st in report["bays"].items():
        lines.append(
            "  %-7s [%s] 箱位 %d/%d  重量 %7.2f/%7.2f t  箱: %s"
            % (name, st["side"], st["used_slots"], st["capacity"],
               st["weight"], st["max_weight"],
               ", ".join(st["containers"]) or "(空)"))
    sw = report["side_weight"]
    lines.append("-" * 64)
    lines.append("  在船箱数: %d   在船总重: %.2f t"
                 % (report["onboard_count"], report["onboard_total_weight"]))
    lines.append("  左舷(P): %.2f t   右舷(S): %.2f t   差值: %.2f t"
                 % (sw["port_P"], sw["starboard_S"],
                    abs(sw["port_P"] - sw["starboard_S"])))
    pr = report["port_remaining"]
    lines.append("  目的港在船箱量: "
                 + (", ".join("%s=%d" % kv for kv in pr.items()) or "(无)"))

    lines.append("=" * 64)
    errs = report["errors"]
    lines.append("错误/告警清单（共 %d 条）" % len(errs))
    lines.append("=" * 64)
    if not errs:
        lines.append("  无错误，配载计划合法。")
    for e in errs:
        tag = "[拒绝]" if e["rejected"] else "[告警]"
        lines.append("  op#%d %-18s %s %s"
                     % (e["op"], e["type"], tag, e["message"]))
    return "\n".join(lines)


def main(argv):
    if len(argv) == 2 and argv[1] == "--demo":
        plan = DEMO_PLAN
    elif len(argv) == 2:
        source = sys.stdin if argv[1] == "-" else open(argv[1], encoding="utf-8")
        with source:
            plan = json.load(source)
    else:
        print(__doc__.strip())
        return 1

    report = StowagePlanner(plan).run(plan.get("operations", []))
    print(render_text(report))
    print()
    print("机器可读 JSON：")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
