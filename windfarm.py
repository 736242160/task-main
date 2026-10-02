#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
windfarm.py — 风电场运行模拟与错误报告工具（纯 Python 标准库，单文件）

用法:
    python3 windfarm.py 输入.json [-o 输出.json] [--conflict-threshold 2.0]

输入 JSON 格式:
{
  "turbines": [
    {"name": "T1", "rated_power": 2000, "cut_in": 3.0, "cut_out": 25.0}
  ],
  "masts": [
    {"name": "M1", "turbines": ["T1", "T2"]}
  ],
  "events": [
    {"time": 1, "wind": {"M1": 10.5}},
    {"time": 2, "wind": {"M1": 12.0}, "fault": ["T1"]},
    {"time": 3, "wind": {"M1": 12.0}, "repair": ["T1"],
     "curtail": [{"turbine": "T2", "limit": 1000}]},
    {"time": 4, "wind": {"M1": 8.0},
     "curtail": [{"turbine": "T2", "limit": null}]}
  ]
}

字段说明:
  turbines: name 名称, rated_power 额定功率(kW), cut_in 切入风速(m/s), cut_out 切出风速(m/s)
  masts:    name 名称, turbines 该塔服务的风机列表（一塔可服务多机，一机可被多塔服务）
  events:   time 时刻; wind 各塔风速; fault/repair 故障/修复风机列表;
            curtail 限载指令（limit 为数值则叠加一条限载，为 null 则清除该风机全部限载）

规则说明（自定规则及理由）:
  1. 功率曲线: 风速低于切入或高于切出功率为 0；切入到额定风速之间按三次方
     曲线上升（近似风能正比于 v^3 的空气动力学规律）；额定风速定义为
     cut_in + 0.4*(cut_out - cut_in)（典型机组额定风速约为切出的 45%~50%），
     额定风速到切出之间恒为额定功率。
  2. 多塔矛盾裁决: 同一风机的多个服务塔风速极差超过阈值（默认 2.0 m/s，
     可用 --conflict-threshold 调整）即判定数据矛盾并报告；裁决取最小风速
     参与计算——保守估计，既不高估发电量，也不会漏判低风速停机。
  3. 故障: fault 指令后风机进入故障状态，故障期间每个时刻功率为 0 并报告；
     repair 后恢复。故障状态跨时刻延续。
  4. 限载: 多条限载指令叠加生效，实际上限为所有未清除限制的最小值；
     限载跨时刻持续有效，直到 limit=null 指令清除。限载后全场功率级联重算。
  5. 引用校验: 测风塔定义、故障/修复/限载指令、运行流中引用不存在的
     风机或测风塔均报告错误；非法数值（负风速、负限载、切入>=切出等）同样报告。
  6. 状态延续: 故障集合与限载列表在时刻之间保持，不在时刻边界重置。
"""

import argparse
import json
import sys

DEFAULT_CONFLICT_THRESHOLD = 2.0


def power_curve(wind, rated_power, cut_in, cut_out):
    """三次方功率曲线：切入~额定风速间 P = P_rated * ((v-v_in)/(v_r-v_in))^3。"""
    if wind < cut_in or wind > cut_out:
        return 0.0
    rated_wind = cut_in + 0.4 * (cut_out - cut_in)
    if wind >= rated_wind:
        return float(rated_power)
    ratio = (wind - cut_in) / (rated_wind - cut_in)
    return rated_power * ratio ** 3


class Simulator:
    def __init__(self, config, conflict_threshold):
        self.threshold = conflict_threshold
        self.errors = []          # {"time", "level", "message"}
        self.steps = []           # 每时刻运行结果
        self.turbines = {}        # name -> {rated_power, cut_in, cut_out}
        self.masts = {}           # name -> [turbine names]
        self.turbine_masts = {}   # turbine -> [mast names]
        self.faulted = set()      # 故障中的风机（跨时刻延续）
        self.curtails = {}        # turbine -> [生效中的限载值]（跨时刻延续）
        self._load_definitions(config)
        self.events = config.get("events", [])

    def report(self, time, level, message):
        self.errors.append({"time": time, "level": level, "message": message})

    # ---------- 定义加载与校验 ----------
    def _load_definitions(self, config):
        for t in config.get("turbines", []):
            name = t.get("name")
            if not name:
                self.report("定义", "错误", "存在未命名的风机定义，已忽略")
                continue
            if name in self.turbines:
                self.report("定义", "错误", f"风机 {name} 重复定义，后者被忽略")
                continue
            rated, cut_in, cut_out = t.get("rated_power"), t.get("cut_in"), t.get("cut_out")
            bad = next((label for label, val in
                        (("额定功率", rated), ("切入风速", cut_in), ("切出风速", cut_out))
                        if not isinstance(val, (int, float)) or isinstance(val, bool)), None)
            if bad:
                self.report("定义", "错误", f"风机 {name} 的{bad}缺失或不是数值，该风机被忽略")
                continue
            if rated <= 0:
                self.report("定义", "错误", f"风机 {name} 额定功率必须为正，该风机被忽略")
                continue
            if cut_in < 0 or cut_out <= cut_in:
                self.report("定义", "错误", f"风机 {name} 风速区间非法（需 0<=切入<切出），该风机被忽略")
                continue
            self.turbines[name] = {"rated_power": float(rated),
                                   "cut_in": float(cut_in), "cut_out": float(cut_out)}
            self.turbine_masts[name] = []

        for m in config.get("masts", []):
            name = m.get("name")
            if not name:
                self.report("定义", "错误", "存在未命名的测风塔定义，已忽略")
                continue
            if name in self.masts:
                self.report("定义", "错误", f"测风塔 {name} 重复定义，后者被忽略")
                continue
            valid = []
            for tn in m.get("turbines", []):
                if tn not in self.turbines:
                    self.report("定义", "错误", f"测风塔 {name} 引用了不存在的风机 {tn}")
                else:
                    valid.append(tn)
            self.masts[name] = valid
            for tn in valid:
                self.turbine_masts[tn].append(name)

        for tn, masts in self.turbine_masts.items():
            if not masts:
                self.report("定义", "错误", f"风机 {tn} 没有任何测风塔服务，运行时将按无数据停机")

    # ---------- 主流程 ----------
    def run(self):
        events = self.events if isinstance(self.events, list) else []
        ordered = []
        for i, ev in enumerate(events):
            if not isinstance(ev, dict) or not isinstance(ev.get("time"), (int, float)):
                self.report("定义", "错误", f"第 {i + 1} 条运行事件缺少合法 time，已忽略")
                continue
            ordered.append(ev)
        ordered.sort(key=lambda e: e["time"])  # 稳定排序，同时刻按书写顺序
        for ev in ordered:
            self._step(ev)
        return {"steps": self.steps, "errors": self.errors}

    def _step(self, ev):
        time = ev["time"]
        self._apply_commands(time, ev)
        mast_wind = self._collect_wind(time, ev.get("wind") or {})
        records, total = [], 0.0
        for tn in self.turbines:
            rec = self._compute_turbine(time, tn, mast_wind)
            records.append(rec)
            total += rec["power"]
        self.steps.append({"time": time, "turbines": records,
                           "farm_power": round(total, 3)})

    def _apply_commands(self, time, ev):
        for tn in ev.get("fault") or []:
            if tn not in self.turbines:
                self.report(time, "错误", f"故障指令引用了不存在的风机 {tn}")
            elif tn in self.faulted:
                self.report(time, "警告", f"风机 {tn} 已处于故障状态，故障指令被忽略")
            else:
                self.faulted.add(tn)
                self.report(time, "信息", f"风机 {tn} 进入故障状态")
        for tn in ev.get("repair") or []:
            if tn not in self.turbines:
                self.report(time, "错误", f"修复指令引用了不存在的风机 {tn}")
            elif tn not in self.faulted:
                self.report(time, "警告", f"风机 {tn} 未处于故障状态，修复指令被忽略")
            else:
                self.faulted.discard(tn)
                self.report(time, "信息", f"风机 {tn} 故障修复，恢复运行")
        for cmd in ev.get("curtail") or []:
            tn = cmd.get("turbine") if isinstance(cmd, dict) else None
            if tn not in self.turbines:
                self.report(time, "错误", f"限载指令引用了不存在的风机 {tn}")
                continue
            limit = cmd.get("limit")
            if limit is None:
                if self.curtails.get(tn):
                    self.report(time, "信息", f"风机 {tn} 的全部限载指令已清除")
                self.curtails.pop(tn, None)
            elif isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit >= 0:
                self.curtails.setdefault(tn, []).append(float(limit))
                self.report(time, "信息",
                            f"风机 {tn} 新增限载 {limit} kW，当前生效上限 {min(self.curtails[tn]):g} kW")
            else:
                self.report(time, "错误", f"风机 {tn} 的限载值非法: {limit!r}，指令被忽略")

    def _collect_wind(self, time, wind):
        mast_wind = {}
        for mn, v in wind.items():
            if mn not in self.masts:
                self.report(time, "错误", f"运行流引用了不存在的测风塔 {mn}")
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                self.report(time, "错误", f"测风塔 {mn} 的风速值非法: {v!r}，本时刻该塔数据作废")
                continue
            mast_wind[mn] = float(v)
        return mast_wind

    # ---------- 单风机计算 ----------
    def _compute_turbine(self, time, tn, mast_wind):
        spec = self.turbines[tn]
        rec = {"name": tn, "wind": None, "status": "", "power": 0.0,
               "raw_power": None, "limit": None}

        if tn in self.faulted:
            rec["status"] = "故障停机"
            self.report(time, "警告", f"风机 {tn} 处于故障期间，本时刻不发电")
            return rec

        masts = self.turbine_masts[tn]
        available = [(mn, mast_wind[mn]) for mn in masts if mn in mast_wind]
        for mn in masts:
            if mn not in mast_wind:
                self.report(time, "警告", f"测风塔 {mn} 本时刻无风速数据（影响风机 {tn}）")
        if not available:
            rec["status"] = "无测风数据停机"
            self.report(time, "错误", f"风机 {tn} 无任何可用测风数据，按停机处理")
            return rec

        speeds = [v for _, v in available]
        if len(available) > 1:
            spread = max(speeds) - min(speeds)
            if spread > self.threshold:
                detail = ", ".join(f"{mn}={v:g}" for mn, v in available)
                self.report(time, "错误",
                            f"风机 {tn} 测风数据矛盾（{detail}，极差 {spread:.2f} m/s "
                            f"超过阈值 {self.threshold:g}），按最小风速保守裁决")
        v = min(speeds)
        rec["wind"] = v

        if v < spec["cut_in"]:
            rec["status"] = "低于切入风速停机"
            self.report(time, "信息",
                        f"风机 {tn} 风速 {v:g} m/s 低于切入风速 {spec['cut_in']:g}，停机")
            return rec
        if v > spec["cut_out"]:
            rec["status"] = "高于切出风速停机"
            self.report(time, "信息",
                        f"风机 {tn} 风速 {v:g} m/s 高于切出风速 {spec['cut_out']:g}，停机")
            return rec

        raw = power_curve(v, spec["rated_power"], spec["cut_in"], spec["cut_out"])
        rec["raw_power"] = round(raw, 3)
        limits = self.curtails.get(tn)
        if limits:
            eff = min(limits)
            rec["limit"] = eff
            power = min(raw, eff)
            if power < raw:
                rec["status"] = "限载运行"
                self.report(time, "信息",
                            f"风机 {tn} 被限载：理论 {raw:.1f} kW 限制为 {power:.1f} kW")
            else:
                rec["status"] = "运行"
        else:
            power = raw
            rec["status"] = "运行"
        rec["power"] = round(power, 3)
        return rec


def format_report(result):
    lines = ["=" * 64, "运行结果", "=" * 64]
    for step in result["steps"]:
        lines.append(f"\n[时刻 {step['time']:g}] 全场总功率: {step['farm_power']:.1f} kW")
        for rec in step["turbines"]:
            wind = "-" if rec["wind"] is None else f"{rec['wind']:.1f} m/s"
            extra = f"（限载上限 {rec['limit']:g} kW）" if rec["limit"] is not None else ""
            lines.append(f"  {rec['name']:<8} 风速 {wind:<10} 状态 {rec['status']:<10} "
                         f"功率 {rec['power']:>9.1f} kW {extra}")
    counts = {}
    for e in result["errors"]:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
    summary = "，".join(f"{k} {v} 条" for k, v in sorted(counts.items())) or "无"
    lines += ["", "=" * 64, f"错误与事件清单（{summary}）", "=" * 64]
    for e in result["errors"]:
        lines.append(f"[{e['level']}] 时刻 {e['time']}: {e['message']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="风电场运行模拟与错误报告工具（纯标准库）")
    ap.add_argument("input", help="输入 JSON 文件（风机/测风塔定义 + 运行流）")
    ap.add_argument("-o", "--output", help="将完整结果（含每时刻明细与错误清单）写为 JSON")
    ap.add_argument("--conflict-threshold", type=float, default=DEFAULT_CONFLICT_THRESHOLD,
                    help="多塔风速矛盾判定阈值 m/s（默认 %(default)s）")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        config = json.load(f)

    sim = Simulator(config, args.conflict_threshold)
    result = sim.run()
    print(format_report(result))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n完整结果已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
