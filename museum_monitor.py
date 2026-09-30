#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
museum_monitor.py — 博物馆展柜温湿度监测与保护联动工具（纯 Python 标准库，单文件）

输入（JSON）：
{
  "cases":    {"C1": {"adjacent": ["C2"]}, ...},              # 展柜及相邻关系
  "artifacts":[{"id": "A1", "case": "C1",
                "temp_max": 22.0, "rh_max": 60.0}, ...],      # 藏品定义（编号/展柜/阈值）
  "stream":   [                                               # 监测流（按时间顺序）
    {"type": "monitor", "round": 1, "case": "C1", "temp": 21.5, "humidity": 55.0},
    {"type": "move", "artifact": "A1", "case": "C2"}          # 藏品更换展柜
  ]
}

自定规则（理由随附）：
1. 监测点故障：某展柜连续 3 轮无任何监测数据即判定监测点故障并报告；
   恢复数据后报告恢复。理由：单轮缺测多为网络抖动等瞬时问题，
   连续 3 轮缺测说明采集链路已不可用，需人工介入。
2. 同轮同柜多次监测冲突：同一展柜同一轮多条监测的温度极差 > 1.0°C 或
   湿度极差 > 5.0%RH 判为数值矛盾并报告；冲突时取最保守值（最高温、最高湿）
   参与判定。理由：藏品保护宁可误报不可漏报。
3. 保护措施：某展柜任一藏品超限，即开启该柜保护措施（降温除湿），
   下一轮起生效；该柜全部藏品回到阈值内后关闭并报告。
   保护开启后对监测值的级联修正：本柜 温度-2.0°C、湿度-5.0%RH；
   每个相邻展柜 温度-1.0°C、湿度-3.0%RH（多个邻居分别累加），
   相邻柜按修正后的有效值重新计算超限，保证级联重算正确。
4. 阈值跟随藏品：藏品更换展柜后，其阈值在新展柜生效，原展柜不再检查它。
5. 状态跨轮延续：缺测计数、故障标记、保护开关、藏品位置均跨轮保持。

用法：
  python3 museum_monitor.py input.json     # 从 JSON 文件读取
  python3 museum_monitor.py --demo         # 运行内置示例
"""

import argparse
import json
import sys

FAULT_MISS_ROUNDS = 3      # 连续缺测多少轮判定监测点故障
TEMP_CONFLICT_TOL = 1.0    # 同轮同柜温度极差容忍（°C）
RH_CONFLICT_TOL = 5.0      # 同轮同柜湿度极差容忍（%RH）
PROTECT_SELF_TEMP = 2.0    # 保护措施对本柜的温度修正
PROTECT_SELF_RH = 5.0      # 保护措施对本柜的湿度修正
PROTECT_ADJ_TEMP = 1.0     # 保护措施对每个相邻柜的温度修正
PROTECT_ADJ_RH = 3.0       # 保护措施对每个相邻柜的湿度修正


class Monitor:
    def __init__(self, config):
        self.adjacent = {name: list(spec.get("adjacent", []))
                         for name, spec in config.get("cases", {}).items()}
        self.artifacts = {}
        for art in config.get("artifacts", []):
            self.artifacts[art["id"]] = {
                "case": art["case"],
                "temp_max": float(art["temp_max"]),
                "rh_max": float(art["rh_max"]),
            }
        self.misses = {name: 0 for name in self.adjacent}       # 连续缺测计数
        self.faulted = {name: False for name in self.adjacent}  # 故障标记
        self.protection = {name: False for name in self.adjacent}  # 保护开关
        self.results = []   # 监测结果（按轮）
        self.errors = []    # 错误清单

    def run(self, stream):
        current_round, buffer = None, []
        for event in stream:
            etype = event.get("type")
            if etype == "monitor":
                rnd = event.get("round")
                if current_round is None:
                    current_round = rnd
                elif rnd != current_round:
                    self._flush_round(current_round, buffer)
                    current_round, buffer = rnd, []
                buffer.append(event)
            elif etype == "move":
                if buffer:
                    self._flush_round(current_round, buffer)
                    current_round, buffer = None, []
                self._move(event)
            else:
                self.errors.append("未知事件类型：%r" % (event,))
        if buffer:
            self._flush_round(current_round, buffer)

    def _move(self, event):
        aid, target = event.get("artifact"), event.get("case")
        if aid not in self.artifacts:
            self.errors.append("藏品调动失败：藏品 %s 不存在" % aid)
            return
        if target not in self.adjacent:
            self.errors.append("藏品调动失败：展柜 %s 不存在（藏品 %s）" % (target, aid))
            return
        old = self.artifacts[aid]["case"]
        self.artifacts[aid]["case"] = target
        self.results.append("[调动] 藏品 %s：%s -> %s，阈值随藏品级联更新至新展柜"
                            % (aid, old, target))

    def _flush_round(self, round_no, readings):
        lines = ["== 第 %s 轮 ==" % round_no]
        # 1) 汇总每柜读数；监测引用不存在的展柜 -> 报告并忽略
        per_case = {}
        for r in readings:
            case = r.get("case")
            if case not in self.adjacent:
                self.errors.append("第%s轮：监测引用了不存在的展柜 %s，已忽略该条数据"
                                   % (round_no, case))
                lines.append("  [错误] 展柜 %s 不存在，数据忽略" % case)
                continue
            per_case.setdefault(case, []).append(
                (float(r["temp"]), float(r["humidity"])))
        # 2) 缺测计数与故障判定（跨轮延续）
        for case in self.adjacent:
            if case in per_case:
                self.misses[case] = 0
                if self.faulted[case]:
                    self.faulted[case] = False
                    lines.append("  [恢复] 展柜 %s 监测点恢复数据" % case)
            else:
                self.misses[case] += 1
                if self.misses[case] >= FAULT_MISS_ROUNDS and not self.faulted[case]:
                    self.faulted[case] = True
                    self.errors.append("第%s轮：展柜 %s 监测点故障（连续 %d 轮无数据）"
                                       % (round_no, case, self.misses[case]))
                    lines.append("  [错误] 展柜 %s 监测点故障（连续 %d 轮无数据）"
                                 % (case, self.misses[case]))
        # 3) 同轮同柜多次监测：冲突检测 + 保守合并
        merged = {}
        for case, vals in per_case.items():
            temps = [v[0] for v in vals]
            rhs = [v[1] for v in vals]
            if len(vals) > 1 and (max(temps) - min(temps) > TEMP_CONFLICT_TOL
                                  or max(rhs) - min(rhs) > RH_CONFLICT_TOL):
                self.errors.append(
                    "第%s轮：展柜 %s 同轮 %d 次监测数值矛盾（温度极差 %.1f°C，"
                    "湿度极差 %.1f%%RH），取最保守值"
                    % (round_no, case, len(vals),
                       max(temps) - min(temps), max(rhs) - min(rhs)))
                lines.append("  [错误] 展柜 %s 同轮监测冲突，取最保守值" % case)
            merged[case] = (max(temps), max(rhs))
        # 4) 保护措施级联修正（按本轮开始时的保护状态，相邻柜受影响重算）
        effective = {}
        for case, (t, h) in merged.items():
            et, eh = t, h
            if self.protection.get(case):
                et -= PROTECT_SELF_TEMP
                eh -= PROTECT_SELF_RH
            for nb in self.adjacent.get(case, []):
                if self.protection.get(nb):
                    et -= PROTECT_ADJ_TEMP
                    eh -= PROTECT_ADJ_RH
            effective[case] = (round(et, 2), round(eh, 2))
        # 5) 阈值判定（按有效值，阈值跟随藏品当前所在展柜）
        exceeded = set()
        for case in sorted(effective):
            et, eh = effective[case]
            raw = merged[case]
            tag = "  展柜 %s：实测 %.1f°C / %.1f%%RH" % (case, raw[0], raw[1])
            if (et, eh) != raw:
                tag += "，保护修正后有效值 %.1f°C / %.1f%%RH" % (et, eh)
            lines.append(tag)
            for aid, art in sorted(self.artifacts.items()):
                if art["case"] != case:
                    continue
                if et > art["temp_max"]:
                    exceeded.add(case)
                    lines.append("    [告警] 温度超限：展柜 %s 藏品 %s 超限 %.2f°C"
                                 "（阈值 %.1f）" % (case, aid, et - art["temp_max"],
                                                    art["temp_max"]))
                if eh > art["rh_max"]:
                    exceeded.add(case)
                    lines.append("    [告警] 湿度超限：展柜 %s 藏品 %s 超限 %.2f%%RH"
                                 "（阈值 %.1f）" % (case, aid, eh - art["rh_max"],
                                                    art["rh_max"]))
        # 6) 保护措施开关（下一轮起生效，状态跨轮延续）
        for case in sorted(self.adjacent):
            if case in exceeded and not self.protection[case]:
                self.protection[case] = True
                lines.append("  [保护] 展柜 %s 开启保护措施（降温除湿），"
                             "相邻展柜将级联修正" % case)
            elif self.protection[case] and case in effective and case not in exceeded:
                self.protection[case] = False
                lines.append("  [保护] 展柜 %s 已恢复阈值内，关闭保护措施" % case)
        self.results.extend(lines)

    def report(self):
        out = ["===== 监测结果 ====="] + self.results + ["", "===== 错误清单 ====="]
        out += ["- " + e for e in self.errors] if self.errors else ["（无错误）"]
        return "\n".join(out)


DEMO = {
    "cases": {
        "C1": {"adjacent": ["C2"]},
        "C2": {"adjacent": ["C1", "C3"]},
        "C3": {"adjacent": ["C2"]},
        "C4": {"adjacent": []}
    },
    "artifacts": [
        {"id": "A1", "case": "C1", "temp_max": 22.0, "rh_max": 60.0},
        {"id": "A2", "case": "C2", "temp_max": 20.0, "rh_max": 55.0},
        {"id": "A3", "case": "C3", "temp_max": 24.0, "rh_max": 65.0},
        {"id": "A4", "case": "C4", "temp_max": 22.0, "rh_max": 60.0}
    ],
    "stream": [
        # 第1轮：C1 两次读数矛盾（冲突）；C2 超限；C9 不存在；C4 缺测(1)
        {"type": "monitor", "round": 1, "case": "C1", "temp": 21.0, "humidity": 55.0},
        {"type": "monitor", "round": 1, "case": "C1", "temp": 24.0, "humidity": 62.0},
        {"type": "monitor", "round": 1, "case": "C2", "temp": 23.0, "humidity": 58.0},
        {"type": "monitor", "round": 1, "case": "C3", "temp": 22.0, "humidity": 60.0},
        {"type": "monitor", "round": 1, "case": "C9", "temp": 20.0, "humidity": 50.0},
        # 第2轮：C1/C2 保护生效并级联修正相邻柜；C4 缺测(2)
        {"type": "monitor", "round": 2, "case": "C1", "temp": 23.0, "humidity": 61.0},
        {"type": "monitor", "round": 2, "case": "C2", "temp": 21.0, "humidity": 57.0},
        {"type": "monitor", "round": 2, "case": "C3", "temp": 25.0, "humidity": 66.0},
        # 藏品 A2 更换展柜 C2 -> C3，阈值随之级联更新
        {"type": "move", "artifact": "A2", "case": "C3"},
        # 第3轮：A2 的阈值在 C3 生效并超限；C4 缺测(3) -> 故障
        {"type": "monitor", "round": 3, "case": "C1", "temp": 21.0, "humidity": 55.0},
        {"type": "monitor", "round": 3, "case": "C2", "temp": 19.0, "humidity": 50.0},
        {"type": "monitor", "round": 3, "case": "C3", "temp": 23.0, "humidity": 64.0},
        # 第4轮：C3 保护生效后恢复；C4 恢复数据
        {"type": "monitor", "round": 4, "case": "C3", "temp": 22.0, "humidity": 60.0},
        {"type": "monitor", "round": 4, "case": "C4", "temp": 22.0, "humidity": 58.0}
    ]
}


def main():
    ap = argparse.ArgumentParser(description="博物馆展柜温湿度监测与保护联动工具")
    ap.add_argument("input", nargs="?", help="输入 JSON 文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args()
    if args.demo:
        config = DEMO
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = json.load(sys.stdin)
    mon = Monitor(config)
    mon.run(config.get("stream", []))
    print(mon.report())


if __name__ == "__main__":
    main()
