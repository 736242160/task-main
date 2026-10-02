#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水厂水质监测处置工具（纯 Python 标准库，单文件）。

用法:
    python3 water_quality_monitor.py [输入文件]
    省略输入文件时从标准输入读取。

输入格式（按行解析，# 开头为注释，空行忽略，字段以空白分隔）:
    POINT  <监测点名> <服务片区>       定义监测点
    METRIC <指标名> <阈值> <处置类型>   定义指标，处置类型为 停供 或 预警
    ROUND  [轮次名]                    结束上一轮并开始新一轮监测（可省略，默认第 1 轮）
    MON    <监测点> <指标> <实测值>     一条监测记录

规则说明:
    1. 实测值 > 阈值 判定为超标，报告（点、指标、超限量）。
    2. 同点同轮多指标超标时处置合并：任一超标指标为停供型则整体停供，否则预警。
    3. 监测引用不存在的点或指标时报告错误，该条记录忽略。
    4. 停供处置后，该点所在片区供水状态级联置为停供；片区内全部点恢复后片区才恢复。
    5. 停供期间该片区的监测照常进行并标记“停供期间”，超标仍报告，
       新出现的停供型超标指标并入恢复条件。
    6. 恢复条件：触发停供的每个指标在该点连续 RECOVERY_STREAK(=3) 轮实测正常方可恢复。
       取 3 轮的理由：水质存在随机波动，单次正常可能是偶然读数；连续 3 轮正常
       既能覆盖常见波动周期、避免误恢复，又不至于过度延迟恢复供水。
    7. 数据矛盾：同片区两个监测点同轮同指标实测值之差超过该指标阈值的
       CONTRADICTION_RATIO(=50%) 即判定矛盾并报告。理由：同一片区由同一管网
       供水，水质应大致一致，差异超过阈值一半通常意味着仪表故障或采样错误。
    8. 同轮同点同指标重复监测报告错误，保留首条记录。
    9. 输出分为：监测结果（逐轮）、错误清单、最终片区供水状态。
"""

import sys

RECOVERY_STREAK = 3       # 恢复所需连续正常轮数
CONTRADICTION_RATIO = 0.5  # 数据矛盾判定：差值占阈值比例上限

ACTION_STOP = "停供"
ACTION_WARN = "预警"


class WaterQualityMonitor:
    def __init__(self):
        self.points = {}         # 点名 -> 服务片区
        self.metrics = {}        # 指标名 -> (阈值, 处置类型)
        self.errors = []         # 错误清单
        self.lines_out = []      # 监测结果输出
        self.stopped = {}        # 点名 -> 是否停供
        self.stop_triggers = {}  # 点名 -> 触发停供且尚未恢复的指标集合
        self.normal_streak = {}  # (点名, 指标) -> 连续正常轮数
        self.area_status = {}    # 片区 -> "正常" / "停供"
        self.round_no = 1
        self.round_label = "1"
        self.pending = []        # 当前轮缓存的监测记录 (行号, 点, 指标, 值)

    def error(self, msg):
        self.errors.append(msg)

    def out(self, msg):
        self.lines_out.append(msg)

    # ---------------- 输入解析 ----------------
    def feed(self, lineno, raw):
        line = raw.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split()
        cmd = parts[0].upper()
        if cmd == "POINT":
            if len(parts) != 3:
                self.error("行%d: POINT 需要 2 个参数（名称 服务片区），实际 %d 个"
                           % (lineno, len(parts) - 1))
                return
            name, area = parts[1], parts[2]
            if name in self.points:
                self.error("行%d: 监测点 '%s' 重复定义，保留首次定义" % (lineno, name))
                return
            self.points[name] = area
            self.stopped[name] = False
            self.stop_triggers[name] = set()
            self.area_status.setdefault(area, "正常")
        elif cmd == "METRIC":
            if len(parts) != 4:
                self.error("行%d: METRIC 需要 3 个参数（名称 阈值 处置类型），实际 %d 个"
                           % (lineno, len(parts) - 1))
                return
            name, threshold_s, action = parts[1], parts[2], parts[3]
            try:
                threshold = float(threshold_s)
            except ValueError:
                self.error("行%d: 指标 '%s' 阈值 '%s' 不是有效数值" % (lineno, name, threshold_s))
                return
            if action not in (ACTION_STOP, ACTION_WARN):
                self.error("行%d: 指标 '%s' 处置类型 '%s' 无效，应为 停供 或 预警"
                           % (lineno, name, action))
                return
            if name in self.metrics:
                self.error("行%d: 指标 '%s' 重复定义，保留首次定义" % (lineno, name))
                return
            self.metrics[name] = (threshold, action)
        elif cmd == "ROUND":
            self.flush_round()
            self.round_no += 1
            self.round_label = parts[1] if len(parts) > 1 else str(self.round_no)
        elif cmd == "MON":
            if len(parts) != 4:
                self.error("行%d: MON 需要 3 个参数（监测点 指标 实测值），实际 %d 个"
                           % (lineno, len(parts) - 1))
                return
            point, metric, value_s = parts[1], parts[2], parts[3]
            try:
                value = float(value_s)
            except ValueError:
                self.error("行%d: 监测值 '%s' 不是有效数值" % (lineno, value_s))
                return
            self.pending.append((lineno, point, metric, value))
        else:
            self.error("行%d: 无法识别的指令 '%s'" % (lineno, parts[0]))

    # ---------------- 轮次处理 ----------------
    def flush_round(self):
        if not self.pending:
            return
        label = self.round_label
        self.out("---- 第 %s 轮监测结果 ----" % label)

        # 1. 校验引用并去除同轮重复监测
        valid = {}   # (点, 指标) -> (值, 行号)
        order = []
        for lineno, point, metric, value in self.pending:
            if point not in self.points:
                self.error("行%d: 监测引用了不存在的监测点 '%s'" % (lineno, point))
                continue
            if metric not in self.metrics:
                self.error("行%d: 监测引用了不存在的指标 '%s'" % (lineno, metric))
                continue
            key = (point, metric)
            if key in valid:
                self.error("行%d: 第%s轮重复监测 点[%s] 指标[%s]，保留首条记录"
                           % (lineno, label, point, metric))
                continue
            valid[key] = (value, lineno)
            order.append(key)
        self.pending = []

        # 2. 同片区多点数据矛盾检查
        self.check_contradictions(valid, label)

        # 3. 逐条判定超标
        exceeded = {}  # 点 -> [(指标, 值, 阈值, 超限量, 处置类型)]
        for point, metric in order:
            value, _ = valid[(point, metric)]
            threshold, action = self.metrics[metric]
            during = "（停供期间）" if self.stopped[point] else ""
            if value > threshold:
                amount = value - threshold
                self.out("  监测 点[%s] 指标[%s] 实测 %g 阈值 %g -> 超标，超限量 %g%s"
                         % (point, metric, value, threshold, amount, during))
                exceeded.setdefault(point, []).append((metric, value, threshold, amount, action))
            else:
                self.out("  监测 点[%s] 指标[%s] 实测 %g 阈值 %g -> 正常%s"
                         % (point, metric, value, threshold, during))

        # 4. 同点同轮多指标超标：处置合并（停供优先于预警）
        for point in sorted(exceeded):
            items = exceeded[point]
            merged = ACTION_STOP if any(it[4] == ACTION_STOP for it in items) else ACTION_WARN
            detail = "、".join("%s(超%g)" % (it[0], it[3]) for it in items)
            self.out("  处置 点[%s] 合并处置为[%s]，超标指标：%s" % (point, merged, detail))
            if merged == ACTION_STOP:
                self.stopped[point] = True
                for metric, _v, _t, _a, act in items:
                    if act == ACTION_STOP:
                        self.stop_triggers[point].add(metric)

        # 5. 停供恢复判定：触发指标须连续 RECOVERY_STREAK 轮正常
        for point in sorted(self.points):
            if not self.stopped[point]:
                continue
            for metric in self.stop_triggers[point]:
                key = (point, metric)
                if key in valid:
                    value, _ = valid[key]
                    threshold = self.metrics[metric][0]
                    if value <= threshold:
                        self.normal_streak[key] = self.normal_streak.get(key, 0) + 1
                    else:
                        self.normal_streak[key] = 0
            triggers = self.stop_triggers[point]
            if triggers and all(self.normal_streak.get((point, m), 0) >= RECOVERY_STREAK
                                for m in triggers):
                self.stopped[point] = False
                self.stop_triggers[point] = set()
                self.out("  恢复 点[%s] 触发指标连续 %d 轮正常，恢复供水"
                         % (point, RECOVERY_STREAK))

        # 6. 片区供水状态级联更新
        for area in sorted(self.area_status):
            pts = [p for p, a in self.points.items() if a == area]
            new_status = ACTION_STOP if any(self.stopped[p] for p in pts) else "正常"
            old_status = self.area_status[area]
            if new_status != old_status:
                self.area_status[area] = new_status
                if new_status == ACTION_STOP:
                    stopped_pts = "、".join(p for p in pts if self.stopped[p])
                    self.out("  级联 片区[%s] 供水状态：正常 -> 停供（停供点：%s）"
                             % (area, stopped_pts))
                else:
                    self.out("  级联 片区[%s] 供水状态：停供 -> 正常（全部监测点已恢复）" % area)

    def check_contradictions(self, valid, label):
        groups = {}  # (片区, 指标) -> [(点, 值)]
        for (point, metric), (value, _lineno) in valid.items():
            area = self.points[point]
            groups.setdefault((area, metric), []).append((point, value))
        for (area, metric), pts in sorted(groups.items()):
            if len(pts) < 2:
                continue
            threshold = self.metrics[metric][0]
            limit = abs(threshold) * CONTRADICTION_RATIO
            p_hi, v_hi = max(pts, key=lambda pv: pv[1])
            p_lo, v_lo = min(pts, key=lambda pv: pv[1])
            if v_hi - v_lo > limit:
                self.error("第%s轮: 片区[%s] 数据矛盾，指标[%s] 点[%s]=%g 与 点[%s]=%g "
                           "差值 %g 超过限值 %g（阈值 %g 的 %d%%）"
                           % (label, area, metric, p_hi, v_hi, p_lo, v_lo,
                              v_hi - v_lo, limit, threshold,
                              int(CONTRADICTION_RATIO * 100)))

    # ---------------- 报告输出 ----------------
    def report(self):
        print("====== 监测结果 ======")
        for line in self.lines_out:
            print(line)
        if not self.lines_out:
            print("（无监测记录）")
        print()
        print("====== 错误清单 ======")
        if self.errors:
            for i, msg in enumerate(self.errors, 1):
                print("%d. %s" % (i, msg))
        else:
            print("（无错误）")
        print()
        print("====== 最终片区供水状态 ======")
        for area in sorted(self.area_status):
            pts = "、".join(p for p, a in self.points.items() if a == area)
            print("片区[%s] %s（监测点：%s）" % (area, self.area_status[area], pts))


def main(argv):
    if len(argv) > 2:
        print("用法: python3 %s [输入文件]" % argv[0], file=sys.stderr)
        return 2
    if len(argv) == 2:
        with open(argv[1], "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    monitor = WaterQualityMonitor()
    for lineno, raw in enumerate(lines, 1):
        monitor.feed(lineno, raw)
    monitor.flush_round()
    monitor.report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
