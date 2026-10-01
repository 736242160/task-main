#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
windfarm.py — 风电场风速限载运行模拟与测风矛盾裁决工具（纯 Python 标准库，单文件）

用法:
    python3 windfarm.py config.json          # 文本报告
    python3 windfarm.py config.json --json   # JSON 报告
    python3 windfarm.py --demo               # 运行内置示例（无需任何文件）

配置文件（JSON）结构:
{
  "conflict_threshold": 3.0,                 // 可选，多塔风速差矛盾阈值(m/s)，默认 3.0
  "turbines": [                              // 风机定义
    {"name": "WT1", "rated_power": 2000, "cut_in": 3.0, "cut_out": 25.0}
  ],
  "masts": [                                 // 测风塔定义
    {"name": "M1", "serves": ["WT1", "WT2"]}
  ],
  "operations": [                            // 运行流，按时刻顺序
    {"time": "08:00",
     "wind":    {"M1": 10.0},                // 各测风塔风速
     "fault":   ["WT2"],                     // 本时刻发生故障的风机（状态延续）
     "recover": ["WT2"],                     // 本时刻恢复的风机
     "limits":  [{"turbine": "WT1", "limit": 500},      // 追加限载（可叠加，取最小）
                 {"turbine": "WT1", "clear": true}]}    // 或清空该风机全部限载
  ]
}

裁决与计算规则（自定，理由如下）:
1. 功率曲线: 线性。v < 切入 或 v >= 切出 → 0；否则 P = 额定 × (v-切入)/(切出-切入)。
   理由: 输入未给厂商功率曲线，线性插值是单调、永超额定的最简保守近似。
2. 多塔裁决: 同风机各服务塔风速两两差 ≤ 阈值 → 取平均（数据一致，均值更稳健）；
   差 > 阈值 → 报"测风矛盾"并取最小值（保守原则: 宁可少发，不可超载）。
3. 数据缺失: 某塔某时刻无数据 → 报错；该风机还有其他塔数据则用其余塔，否则停机。
4. 故障: fault/recover 修改持久故障集，跨时刻延续；故障期间功率为 0 并逐时刻报告。
5. 限载: limit 指令压入该风机限载栈，生效上限 = 栈内最小值，clear 清空；
   状态跨时刻延续；每时刻按最新故障/限载状态对全场功率做级联重算。
6. 引用不存在的风机/测风塔 → 记入错误清单，不影响其余计算。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

DEFAULT_CONFLICT_THRESHOLD = 3.0  # m/s


@dataclass
class Turbine:
    name: str
    rated: float
    cut_in: float
    cut_out: float


def power_curve(tb: Turbine, v: float) -> float:
    """线性功率曲线：切入以下/切出及以上为 0，中间线性，截断到 [0, 额定]。"""
    if v < tb.cut_in or v >= tb.cut_out:
        return 0.0
    return max(0.0, min(tb.rated, tb.rated * (v - tb.cut_in) / (tb.cut_out - tb.cut_in)))


def _err(time, kind, msg):
    return {"time": time, "type": kind, "message": msg}


def build_model(config, errors):
    turbines = {}
    for idx, td in enumerate(config.get("turbines") or []):
        try:
            name = str(td["name"])
            rated = float(td["rated_power"])
            cut_in = float(td["cut_in"])
            cut_out = float(td["cut_out"])
        except (AttributeError, KeyError, TypeError, ValueError):
            errors.append(_err(None, "定义错误", f"风机定义 #{idx + 1} 字段缺失或非法: {td!r}"))
            continue
        if name in turbines:
            errors.append(_err(None, "定义错误", f"风机 {name} 重复定义，忽略后者"))
            continue
        if rated <= 0 or cut_in < 0 or cut_out <= cut_in:
            errors.append(_err(None, "定义错误",
                               f"风机 {name} 参数非法（要求 额定>0 且 0<=切入<切出）"))
            continue
        turbines[name] = Turbine(name, rated, cut_in, cut_out)

    masts = set()
    serving = {n: [] for n in turbines}
    for idx, md in enumerate(config.get("masts") or []):
        if not isinstance(md, dict) or "name" not in md:
            errors.append(_err(None, "定义错误", f"测风塔定义 #{idx + 1} 缺少 name: {md!r}"))
            continue
        mname = str(md["name"])
        if mname in masts:
            errors.append(_err(None, "定义错误", f"测风塔 {mname} 重复定义，忽略后者"))
            continue
        masts.add(mname)
        for tname in md.get("serves") or []:
            if tname not in turbines:
                errors.append(_err(None, "引用错误", f"测风塔 {mname} 引用了不存在的风机 {tname}"))
            else:
                serving[tname].append(mname)

    for tname, ms in serving.items():
        if not ms:
            errors.append(_err(None, "配置错误", f"风机 {tname} 没有任何测风塔服务，将始终停机"))
    return turbines, masts, serving


def simulate(config):
    errors = []
    if not isinstance(config, dict):
        return {"timesteps": [], "errors": [_err(None, "配置错误", "配置必须是 JSON 对象")]}

    turbines, masts, serving = build_model(config, errors)
    try:
        threshold = float(config.get("conflict_threshold", DEFAULT_CONFLICT_THRESHOLD))
        if threshold < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(_err(None, "配置错误",
                           f"conflict_threshold 非法，使用默认 {DEFAULT_CONFLICT_THRESHOLD:g} m/s"))
        threshold = DEFAULT_CONFLICT_THRESHOLD

    faulted = set()                              # 持久故障集（跨时刻延续）
    limit_stack = {n: [] for n in turbines}      # 持久限载栈（跨时刻延续）
    timesteps = []

    ops = config.get("operations") or []
    if not isinstance(ops, list):
        errors.append(_err(None, "配置错误", "operations 必须是数组"))
        ops = []

    for idx, op in enumerate(ops):
        label = f"T{idx + 1}"
        if not isinstance(op, dict):
            errors.append(_err(label, "配置错误", f"运行流条目必须是对象: {op!r}"))
            continue
        label = str(op.get("time", label))
        events = []

        # 1) 故障 / 恢复（先更新状态，再算功率）
        for name in op.get("fault") or []:
            if name not in turbines:
                errors.append(_err(label, "引用错误", f"fault 引用了不存在的风机 {name}"))
            elif name in faulted:
                events.append(f"{name}: 重复故障指令（已在故障中）")
            else:
                faulted.add(name)
                events.append(f"{name}: 发生故障，停机")
        for name in op.get("recover") or []:
            if name not in turbines:
                errors.append(_err(label, "引用错误", f"recover 引用了不存在的风机 {name}"))
            elif name in faulted:
                faulted.discard(name)
                events.append(f"{name}: 故障恢复")
            else:
                events.append(f"{name}: 恢复指令无效（未在故障中）")

        # 2) 限载指令（叠加：生效上限 = 栈内最小值）
        for cmd in op.get("limits") or []:
            if not isinstance(cmd, dict):
                errors.append(_err(label, "配置错误", f"限载指令必须是对象: {cmd!r}"))
                continue
            name = cmd.get("turbine")
            if name not in turbines:
                errors.append(_err(label, "引用错误", f"限载指令引用了不存在的风机 {name!r}"))
                continue
            if cmd.get("clear"):
                limit_stack[name].clear()
                events.append(f"{name}: 清除全部限载")
                continue
            try:
                val = float(cmd["limit"])
            except (KeyError, TypeError, ValueError):
                errors.append(_err(label, "配置错误", f"{name} 限载指令缺少合法 limit 值: {cmd!r}"))
                continue
            if val < 0:
                errors.append(_err(label, "参数错误", f"{name} 限载值 {val:g} 为负，按 0 处理"))
                val = 0.0
            limit_stack[name].append(val)
            events.append(f"{name}: 新增限载 {val:g} kW（当前生效上限 {min(limit_stack[name]):g} kW）")

        # 3) 校验风速数据引用
        wind = op.get("wind") or {}
        if not isinstance(wind, dict):
            errors.append(_err(label, "配置错误", "wind 必须是 {测风塔: 风速} 对象"))
            wind = {}
        for mname in wind:
            if mname not in masts:
                errors.append(_err(label, "引用错误", f"wind 引用了不存在的测风塔 {mname}"))

        # 4) 逐风机计算，级联汇总全场功率
        turbines_out = {}
        farm = 0.0
        for name, tb in turbines.items():
            if name in faulted:
                turbines_out[name] = {"wind": None, "power": 0.0, "status": "故障停机"}
                events.append(f"{name}: 故障期间不发电")
                continue
            ms = serving[name]
            if not ms:
                turbines_out[name] = {"wind": None, "power": 0.0, "status": "无测风塔服务"}
                continue
            vals = []
            for m in ms:
                if m not in wind:
                    errors.append(_err(label, "数据缺失", f"测风塔 {m}（服务 {name}）本时刻无风速数据"))
                    continue
                try:
                    vals.append(float(wind[m]))
                except (TypeError, ValueError):
                    errors.append(_err(label, "数据错误", f"测风塔 {m} 风速 {wind[m]!r} 非数值"))
            if not vals:
                turbines_out[name] = {"wind": None, "power": 0.0, "status": "无有效测风数据"}
                events.append(f"{name}: 无有效测风数据，停机")
                continue
            if len(vals) >= 2 and max(vals) - min(vals) > threshold:
                errors.append(_err(label, "测风矛盾",
                                   f"{name} 服务塔风速差 {max(vals) - min(vals):.2f} m/s "
                                   f"超阈值 {threshold:g}，保守取最小值 {min(vals):g} m/s"))
                v = min(vals)
            else:
                v = sum(vals) / len(vals)

            if v < tb.cut_in:
                p, st = 0.0, "低于切入风速停机"
                events.append(f"{name}: 风速 {v:.2f} m/s 低于切入 {tb.cut_in:g} m/s，停机")
            elif v >= tb.cut_out:
                p, st = 0.0, "高于切出风速停机"
                events.append(f"{name}: 风速 {v:.2f} m/s 达到/超过切出 {tb.cut_out:g} m/s，停机")
            else:
                p = power_curve(tb, v)
                st = "正常发电"
                if limit_stack[name]:
                    cap = min(limit_stack[name])
                    if p > cap:
                        events.append(f"{name}: 限载生效 {p:.2f} → {cap:g} kW")
                        p = cap
                        st = f"限载运行（上限 {cap:g} kW）"
            turbines_out[name] = {"wind": round(v, 3), "power": round(p, 2), "status": st}
            farm += p

        timesteps.append({"time": label, "farm_power": round(farm, 2),
                          "turbines": turbines_out, "events": events})
    return {"timesteps": timesteps, "errors": errors}


def render_text(result):
    lines = []
    for ts in result["timesteps"]:
        lines.append(f"== 时刻 {ts['time']} ==")
        for name, r in ts["turbines"].items():
            w = "-" if r["wind"] is None else f"{r['wind']:.2f} m/s"
            lines.append(f"  {name}: 风速 {w}  功率 {r['power']:.2f} kW  [{r['status']}]")
        lines.append(f"  全场总功率: {ts['farm_power']:.2f} kW")
        for ev in ts["events"]:
            lines.append(f"  * {ev}")
    lines.append("== 错误清单 ==")
    if not result["errors"]:
        lines.append("  （无）")
    for e in result["errors"]:
        lines.append(f"  [{e['type']}] {e['time'] or '定义阶段'}: {e['message']}")
    return "\n".join(lines)


DEMO_CONFIG = {
    "conflict_threshold": 3.0,
    "turbines": [
        {"name": "WT1", "rated_power": 2000, "cut_in": 3, "cut_out": 25},
        {"name": "WT2", "rated_power": 2000, "cut_in": 3, "cut_out": 25},
        {"name": "WT3", "rated_power": 1500, "cut_in": 3, "cut_out": 22},
    ],
    "masts": [
        {"name": "M1", "serves": ["WT1", "WT2"]},
        {"name": "M2", "serves": ["WT2", "WT3", "WT9"]},
    ],
    "operations": [
        {"time": "08:00", "wind": {"M1": 10, "M2": 10.5}},
        {"time": "09:00", "wind": {"M1": 2.0, "M2": 2.5}},
        {"time": "10:00", "wind": {"M1": 26, "M2": 27}},
        {"time": "11:00", "wind": {"M1": 8, "M2": 15}},
        {"time": "12:00", "wind": {"M1": 12, "M2": 12}, "fault": ["WT2"]},
        {"time": "13:00", "wind": {"M1": 12, "M2": 12},
         "limits": [{"turbine": "WT1", "limit": 700}]},
        {"time": "14:00", "wind": {"M1": 12, "M2": 12}, "recover": ["WT2"],
         "limits": [{"turbine": "WT1", "limit": 500}]},
        {"time": "15:00", "wind": {"M1": 12, "M2": 12, "M9": 5},
         "limits": [{"turbine": "WT1", "clear": True}, {"turbine": "WT8", "limit": 100}],
         "fault": ["WT9"]},
    ],
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="风电场风速限载运行模拟与测风矛盾裁决工具")
    ap.add_argument("config", nargs="?", help="运行配置文件（JSON）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        config = DEMO_CONFIG
    elif args.config:
        try:
            with open(args.config, encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"无法读取配置: {e}", file=sys.stderr)
            return 2
    else:
        ap.error("需要配置文件路径或 --demo")

    result = simulate(config)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
