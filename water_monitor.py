#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
water_monitor.py — 水厂水质监测处置工具（纯 Python 标准库，单文件）

功能：
  读取监测点定义、指标定义与多轮监测流，输出逐轮监测结果、超标处置、
  片区供水状态级联变化，以及完整错误清单。

输入格式（UTF-8 文本，支持空行与 # 注释）：
  point  <点名> <服务片区>             # 监测点定义
  metric <指标名> <阈值> <停供|预警>    # 指标定义
  round  <轮次标识>                    # 开始一轮监测
  <点名> <指标名> <实测值>             # 监测记录（必须出现在 round 之后）

用法：
  python3 water_monitor.py 输入文件
  python3 water_monitor.py < 输入文件
  python3 water_monitor.py --demo      # 运行内置示例（覆盖全部规则）

自定规则说明：
  1. 处置合并：同点同轮多指标超标时"就高不就低"——任一超标指标为停供类，
     则该点本轮合并处置为停供，否则为预警。
  2. 恢复条件：停供类指标【连续 3 轮】实测正常方可恢复供水。
     理由：单次正常可能来自采样波动或仪器误差，连续 3 轮可覆盖不同时段/
     批次，既避免"停供-恢复"频繁抖动（flapping），又不至于过度延长停供。
  3. 数据矛盾：同片区、同轮、同指标两监测点实测值之差超过该指标阈值的
     50% 时判为数据矛盾并报告。
     理由：同片区水力停留时间短、水质应相近，差值超过阈值一半通常意味着
     采样或仪表异常，需人工核查。
  4. 重复监测：同轮同点同指标以首次值为准，后续记录报告错误并忽略。
"""

import argparse
import sys
from collections import defaultdict

RECOVERY_NORMAL_ROUNDS = 3   # 恢复供水所需停供类指标连续正常轮数
CONFLICT_RATIO = 0.5         # 同片区两点差值超过阈值该比例即判数据矛盾

DEMO_INPUT = """\
# ===== 监测点定义 =====
point A1 城东片区
point A2 城东片区
point B1 城西片区

# ===== 指标定义 =====
metric 浊度 1.0 停供
metric 余氯 0.3 预警
metric pH  8.5 预警

# ===== 监测流 =====
round 1
A1 浊度 1.6
A1 余氯 0.5
A2 浊度 0.4
A2 浊度 0.45
B1 pH 9.0
C9 浊度 0.2
A1 硬度 0.1

round 2
A1 浊度 0.2
A1 余氯 0.1
A2 浊度 0.9
B1 pH 7.4

round 3
A1 浊度 0.3
A2 浊度 0.35

round 4
A1 浊度 0.5
A2 浊度 0.6
"""


def parse(text):
    """解析输入文本，返回 (监测点, 指标, 轮次列表, 解析错误清单)。"""
    points = {}          # 点名 -> 服务片区
    metrics = {}         # 指标名 -> (阈值, 处置类型)
    rounds = []          # [(轮次标识, [(行号, 点, 指标, 实测值原文)])]
    errors = []
    current = None
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        head = parts[0]
        if head == "point":
            if len(parts) != 3:
                errors.append("第%d行: point 定义格式错误（应为: point 名称 服务片区）: %s" % (lineno, raw_line.strip()))
            elif parts[1] in points:
                errors.append("第%d行: 监测点重复定义: %s" % (lineno, parts[1]))
            else:
                points[parts[1]] = parts[2]
        elif head == "metric":
            if len(parts) != 4:
                errors.append("第%d行: metric 定义格式错误（应为: metric 名称 阈值 停供|预警）: %s" % (lineno, raw_line.strip()))
                continue
            name, th_raw, disp = parts[1], parts[2], parts[3]
            try:
                threshold = float(th_raw)
            except ValueError:
                errors.append("第%d行: 指标 '%s' 阈值无法解析为数值: '%s'" % (lineno, name, th_raw))
                continue
            if disp not in ("停供", "预警"):
                errors.append("第%d行: 指标 '%s' 处置类型非法（仅允许 停供/预警）: '%s'" % (lineno, name, disp))
                continue
            if name in metrics:
                errors.append("第%d行: 指标重复定义: %s" % (lineno, name))
                continue
            metrics[name] = (threshold, disp)
        elif head == "round":
            if len(parts) != 2:
                errors.append("第%d行: round 格式错误（应为: round 轮次标识）: %s" % (lineno, raw_line.strip()))
                continue
            current = []
            rounds.append((parts[1], current))
        else:
            if current is None:
                errors.append("第%d行: 监测记录出现在任何 round 之前，已忽略: %s" % (lineno, raw_line.strip()))
                continue
            if len(parts) != 3:
                errors.append("第%d行: 监测记录格式错误（应为: 点 指标 实测值）: %s" % (lineno, raw_line.strip()))
                continue
            current.append((lineno, parts[0], parts[1], parts[2]))
    return points, metrics, rounds, errors


class Engine:
    """监测处置引擎：逐轮处理监测记录并维护停供/恢复状态。"""

    def __init__(self, points, metrics):
        self.points = points
        self.metrics = metrics
        self.stopped = set()                        # 当前处于停供状态的点
        self.normal_streak = defaultdict(int)       # 点 -> 停供类指标连续正常轮数
        self.errors = []

    def run(self, rounds):
        out = []
        for round_id, records in rounds:
            self._run_round(round_id, records, out)
        return out

    def _run_round(self, rid, records, out):
        out.append("===== 第 %s 轮监测 =====" % rid)

        # 1) 校验记录：未知点/指标、非法数值、重复监测
        seen = {}
        valid = []  # [(点, 指标, 实测值)]
        for lineno, point, metric, raw in records:
            if point not in self.points:
                self.errors.append("第%s轮(第%d行): 监测记录引用了不存在的监测点 '%s'" % (rid, lineno, point))
                continue
            if metric not in self.metrics:
                self.errors.append("第%s轮(第%d行): 监测记录引用了不存在的指标 '%s'" % (rid, lineno, metric))
                continue
            try:
                value = float(raw)
            except ValueError:
                self.errors.append("第%s轮(第%d行): 实测值无法解析为数值: '%s'" % (rid, lineno, raw))
                continue
            key = (point, metric)
            if key in seen:
                self.errors.append(
                    "第%s轮(第%d行): 重复监测 点'%s' 指标'%s'，已采用首次值 %s，忽略本次值 %s"
                    % (rid, lineno, point, metric, seen[key], value))
                continue
            seen[key] = value
            valid.append((point, metric, value))

        # 2) 判定超标
        exceeded = defaultdict(list)  # 点 -> [(指标, 实测值, 阈值, 超限量, 处置类型)]
        for point, metric, value in valid:
            threshold, _ = self.metrics[metric]
            note = "（停供期间延续监测）" if point in self.stopped else ""
            if value > threshold:
                over = value - threshold
                exceeded[point].append((metric, value, threshold, over, self.metrics[metric][1]))
                out.append("  [超标] 点 %s 指标 %s: 实测 %g > 阈值 %g，超限量 %g %s"
                           % (point, metric, value, threshold, over, note))
            else:
                out.append("  [正常] 点 %s 指标 %s: 实测 %g <= 阈值 %g %s"
                           % (point, metric, value, threshold, note))

        # 3) 同点同轮多指标超标：处置合并（就高不就低）
        for point in sorted(exceeded):
            items = exceeded[point]
            merged = "停供" if any(d == "停供" for *_, d in items) else "预警"
            if len(items) > 1:
                names = "、".join(m for m, *_ in items)
                out.append("  [处置合并] 点 %s 同轮 %d 项指标超标（%s），合并处置: %s"
                           % (point, len(items), names, merged))
            else:
                out.append("  [处置] 点 %s 指标 %s 超标，处置: %s" % (point, items[0][0], merged))
            if merged == "停供":
                if point not in self.stopped:
                    self.stopped.add(point)
                    out.append("  [停供] 点 %s 触发停供，片区 %s 供水状态级联更新为【停供】"
                               % (point, self.points[point]))
                self.normal_streak[point] = 0

        # 4) 恢复判定：停供类指标连续 RECOVERY_NORMAL_ROUNDS 轮正常方可恢复
        stop_metrics = {m for m, (_, d) in self.metrics.items() if d == "停供"}
        for point in sorted(list(self.stopped)):
            measured_stop = [m for p, m, _ in valid if p == point and m in stop_metrics]
            exceeded_stop = [m for m, *_ in exceeded.get(point, []) if m in stop_metrics]
            if exceeded_stop:
                self.normal_streak[point] = 0
            elif measured_stop:
                self.normal_streak[point] += 1
                out.append("  [恢复观察] 点 %s 停供类指标连续正常 %d/%d 轮"
                           % (point, self.normal_streak[point], RECOVERY_NORMAL_ROUNDS))
                if self.normal_streak[point] >= RECOVERY_NORMAL_ROUNDS:
                    self.stopped.discard(point)
                    self.normal_streak[point] = 0
                    out.append("  [恢复] 点 %s 停供类指标连续 %d 轮正常，片区 %s 恢复供水"
                               % (point, RECOVERY_NORMAL_ROUNDS, self.points[point]))

        # 5) 数据矛盾检测：同片区同轮同指标两点差值超阈值的 CONFLICT_RATIO
        by_area_metric = defaultdict(dict)  # (片区, 指标) -> {点: 值}
        for point, metric, value in valid:
            by_area_metric[(self.points[point], metric)][point] = value
        for (area, metric), pv in sorted(by_area_metric.items()):
            pts = sorted(pv)
            limit = CONFLICT_RATIO * self.metrics[metric][0]
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    a, b = pts[i], pts[j]
                    diff = abs(pv[a] - pv[b])
                    if diff > limit:
                        self.errors.append(
                            "第%s轮: 数据矛盾 片区'%s' 点'%s'(%g) 与点'%s'(%g) 指标'%s' "
                            "差值 %g 超过允许限值 %g（阈值的 %.0f%%）"
                            % (rid, area, a, pv[a], b, pv[b], metric, diff, limit, CONFLICT_RATIO * 100))

        # 6) 片区供水状态（级联结果）
        warned = {p for p in exceeded if all(d == "预警" for *_, d in exceeded[p])}
        areas = defaultdict(list)
        for point, area in self.points.items():
            areas[area].append(point)
        for area in sorted(areas):
            ps = areas[area]
            if any(p in self.stopped for p in ps):
                status = "停供"
            elif any(p in warned for p in ps):
                status = "预警"
            else:
                status = "正常"
            out.append("  [片区状态] %s: %s" % (area, status))
        out.append("")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="水厂水质监测处置工具（纯 Python 标准库，单文件）",
        epilog="输入格式见源文件 docstring；--demo 可运行内置示例。")
    ap.add_argument("input", nargs="?", help="输入文件（缺省读取标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    if args.demo:
        text = DEMO_INPUT
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    points, metrics, rounds, parse_errors = parse(text)
    if not points or not metrics:
        print("警告: 未解析到监测点或指标定义，请检查输入格式。")
    engine = Engine(points, metrics)
    report = engine.run(rounds)

    print("===== 监测结果 =====")
    print("\n".join(report))
    print("===== 错误清单 =====")
    errors = parse_errors + engine.errors
    if not errors:
        print("  无")
    else:
        for i, e in enumerate(errors, 1):
            print("  %d. %s" % (i, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
