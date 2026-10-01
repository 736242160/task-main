#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winetemp.py — 葡萄酒酿造「工序 × 温度」联动监控工具（纯标准库，单文件）

输入（JSON，文件或标准输入）：
{
  "threshold": 2,                        # 可选，超温降级阈值，默认 2
  "batches":   [{"id": "B1", "variety": "赤霞珠", "target": "特级"}, ...],
  "processes": [{"name": "酒精发酵",
                 "temp_min": 25.0, "temp_max": 30.0,
                 "deps": ["低温浸渍"],          # 可选，默认 []
                 "min_quality": "一级"}, ...],  # 可选，默认 "等外"（无要求）
  "events":    [{"batch": "B1", "process": "酒精发酵", "temp": 26.5}, ...]
}

用法：
  python3 winetemp.py input.json          # 从文件读取
  cat input.json | python3 winetemp.py    # 从标准输入读取
  python3 winetemp.py --demo              # 运行内置演示（覆盖全部错误类型）
  python3 winetemp.py --threshold 3 ...   # 命令行覆盖降级阈值
"""

import argparse
import json
import sys
from dataclasses import dataclass, field

# 品质等级阶梯（低到高）。降级即沿阶梯下降一档，到 "等外" 为止。
QUALITY_LADDER = ["等外", "二级", "一级", "特级"]
QUALITY_RANK = {name: rank for rank, name in enumerate(QUALITY_LADDER)}

# 默认降级阈值：累计 2 次超温降一档。
# 理由：单次超温多为瞬时扰动（开盖操作、探头误差、环境突变），
# 直接降级过于严苛；连续/累计 2 次说明温控已系统性失效，
# 品质受损可信，此时降一档既灵敏又不误伤。
DEFAULT_THRESHOLD = 2


@dataclass
class ProcessDef:
    name: str
    temp_min: float
    temp_max: float
    deps: list = field(default_factory=list)
    min_quality: str = "等外"


@dataclass
class BatchState:
    batch_id: str
    variety: str
    target: str
    current: str                      # 当前品质（随降级变化）
    over_temp_count: int = 0
    completed: list = field(default_factory=list)   # 已完成工序（有序）
    downgraded: bool = False


@dataclass
class Error:
    seq: int          # 事件序号（1 起）；定义阶段错误为 0
    kind: str
    message: str


class Engine:
    """顺序应用酿造事件，跨事件维护批次状态。"""

    def __init__(self, batches, processes, threshold=DEFAULT_THRESHOLD):
        self.threshold = max(1, int(threshold))
        self.batches = {}      # id -> BatchState
        self.processes = {}    # name -> ProcessDef
        self.errors = []

        for b in batches:
            state = BatchState(b["id"], b.get("variety", "?"),
                               b.get("target", "等外"), b.get("target", "等外"))
            if state.current not in QUALITY_RANK:
                self.errors.append(Error(0, "UNKNOWN_QUALITY",
                                         f"批次 {b['id']}: 未知目标品质 '{state.current}'，按等外处理"))
                state.current = state.target = "等外"
            if state.batch_id in self.batches:
                self.errors.append(Error(0, "DUPLICATE_BATCH",
                                         f"批次 {b['id']} 重复定义，后者覆盖前者"))
            self.batches[state.batch_id] = state

        for p in processes:
            deps = list(p.get("deps", []))
            pd = ProcessDef(p["name"], float(p["temp_min"]), float(p["temp_max"]),
                            deps, p.get("min_quality", "等外"))
            if pd.temp_min > pd.temp_max:
                self.errors.append(Error(0, "BAD_TEMP_RANGE",
                                         f"工序 '{pd.name}': 温控区间 [{pd.temp_min}, {pd.temp_max}] 无效"))
            if pd.min_quality not in QUALITY_RANK:
                self.errors.append(Error(0, "UNKNOWN_QUALITY",
                                         f"工序 '{pd.name}': 未知品质要求 '{pd.min_quality}'，按等外处理"))
                pd.min_quality = "等外"
            if pd.name in self.processes:
                self.errors.append(Error(0, "DUPLICATE_PROCESS",
                                         f"工序 '{pd.name}' 重复定义，后者覆盖前者"))
            self.processes[pd.name] = pd

        # 依赖引用校验（定义期）
        for pd in self.processes.values():
            for d in pd.deps:
                if d not in self.processes:
                    self.errors.append(Error(0, "UNKNOWN_DEP",
                                             f"工序 '{pd.name}' 依赖未定义的工序 '{d}'"))

    # ---- 事件处理 ----

    def apply(self, event, seq):
        batch_id = event.get("batch")
        proc_name = event.get("process")
        temp = event.get("temp")

        # 1) 引用校验：不存在的批次 / 工序
        batch = self.batches.get(batch_id)
        if batch is None:
            self.errors.append(Error(seq, "UNKNOWN_BATCH",
                                     f"事件引用了不存在的批次 '{batch_id}'"))
            return
        proc = self.processes.get(proc_name)
        if proc is None:
            self.errors.append(Error(seq, "UNKNOWN_PROCESS",
                                     f"批次 {batch_id}: 引用了不存在的工序 '{proc_name}'"))
            return
        if not isinstance(temp, (int, float)):
            self.errors.append(Error(seq, "BAD_TEMP",
                                     f"批次 {batch_id} 工序 '{proc_name}': 实测温度缺失或非数值"))
            return

        # 2) 顺序约束：依赖工序未完成 → 事件被拒绝（不计完成、不计超温）
        missing = [d for d in proc.deps if d not in batch.completed]
        if missing:
            self.errors.append(Error(seq, "DEPENDENCY_NOT_MET",
                                     f"批次 {batch_id} 工序 '{proc_name}': "
                                     f"依赖工序 {missing} 未完成，本次执行无效"))
            return

        # 3) 重复执行：报告，但仍检查温度（热量确实作用于批次）
        if proc_name in batch.completed:
            self.errors.append(Error(seq, "REPEATED_PROCESS",
                                     f"批次 {batch_id} 工序 '{proc_name}' 重复执行"))
        else:
            batch.completed.append(proc_name)

        # 4) 温度检查：超出温控区间 → 报告超温值并累计
        deviation, direction = 0.0, None
        if temp > proc.temp_max:
            deviation, direction = temp - proc.temp_max, "高于上限"
        elif temp < proc.temp_min:
            deviation, direction = proc.temp_min - temp, "低于下限"
        if direction:
            deviation = round(deviation, 2)
            batch.over_temp_count += 1
            self.errors.append(Error(
                seq, "TEMP_OUT_OF_RANGE",
                f"批次 {batch_id} 工序 '{proc_name}': 实测 {temp}°C "
                f"{direction} [{proc.temp_min}, {proc.temp_max}]，"
                f"超温值 {deviation}°C（本批第 {batch.over_temp_count} 次超温）"))
            self._maybe_downgrade(batch, seq)

        # 5) 品质要求（降级级联的后果）：批次当前品质低于工序要求
        if QUALITY_RANK[batch.current] < QUALITY_RANK[proc.min_quality]:
            self.errors.append(Error(
                seq, "QUALITY_INSUFFICIENT",
                f"批次 {batch_id} 工序 '{proc_name}': 批次当前品质 "
                f"'{batch.current}' 低于该工序要求 '{proc.min_quality}'，"
                f"本工序无法达成原定品质目标"))

    def _maybe_downgrade(self, batch, seq):
        """超温次数每累计达阈值一档，品质降一级；随后级联重算受影响工序。"""
        if batch.over_temp_count % self.threshold != 0:
            return
        rank = QUALITY_RANK[batch.current]
        if rank == 0:
            return  # 已是等外，无法再降
        old, batch.current = batch.current, QUALITY_LADDER[rank - 1]
        batch.downgraded = True
        self.errors.append(Error(
            seq, "BATCH_DOWNGRADED",
            f"批次 {batch.batch_id}: 累计超温 {batch.over_temp_count} 次 "
            f"达阈值 {self.threshold}，品质由 '{old}' 降级为 '{batch.current}'"))
        # 级联重算：尚未完成、且品质要求高于批次当前品质的工序
        affected = [name for name, p in self.processes.items()
                    if name not in batch.completed
                    and QUALITY_RANK[p.min_quality] > QUALITY_RANK[batch.current]]
        if affected:
            self.errors.append(Error(
                seq, "QUALITY_CASCADE",
                f"批次 {batch.batch_id}: 降级级联重算——后续工序 {affected} "
                f"的品质要求已超出批次当前品质 '{batch.current}'"))

    # ---- 输出 ----

    def report(self):
        lines = ["=== 错误报告 ==="]
        if not self.errors:
            lines.append("（无错误）")
        for e in self.errors:
            tag = f"事件#{e.seq}" if e.seq else "定义"
            lines.append(f"[{e.kind}] ({tag}) {e.message}")

        lines.append("")
        lines.append("=== 酿造状态 ===")
        for b in self.batches.values():
            pending = [n for n in self.processes if n not in b.completed]
            lines.append(
                f"批次 {b.batch_id}（{b.variety}）: "
                f"目标品质={b.target} 当前品质={b.current}"
                f"{'（已降级）' if b.downgraded else ''} | "
                f"超温次数={b.over_temp_count} | "
                f"已完成={b.completed or '无'} | 未完成={pending or '无'}")
        return "\n".join(lines)


def run(config, threshold_override=None):
    threshold = threshold_override or config.get("threshold", DEFAULT_THRESHOLD)
    engine = Engine(config.get("batches", []), config.get("processes", []), threshold)
    for seq, event in enumerate(config.get("events", []), start=1):
        engine.apply(event, seq)
    return engine.report()


def demo_config():
    return {
        "threshold": 2,
        "batches": [
            {"id": "B1", "variety": "赤霞珠", "target": "特级"},
            {"id": "B2", "variety": "美乐", "target": "一级"},
        ],
        "processes": [
            {"name": "除梗破碎", "temp_min": 18, "temp_max": 24, "deps": []},
            {"name": "低温浸渍", "temp_min": 8, "temp_max": 12, "deps": ["除梗破碎"]},
            {"name": "酒精发酵", "temp_min": 25, "temp_max": 30,
             "deps": ["低温浸渍"], "min_quality": "一级"},
            # 下面两道工序互相无依赖（都仅依赖酒精发酵）→ 可任意先后（并行）
            {"name": "陈酿", "temp_min": 12, "temp_max": 16,
             "deps": ["酒精发酵"], "min_quality": "二级"},
            {"name": "苹果酸乳酸发酵", "temp_min": 18, "temp_max": 22,
             "deps": ["酒精发酵"], "min_quality": "特级"},
        ],
        "events": [
            {"batch": "B1", "process": "除梗破碎", "temp": 20},        # 正常
            {"batch": "B1", "process": "低温浸渍", "temp": 10},        # 正常
            {"batch": "B1", "process": "酒精发酵", "temp": 31.5},      # 超温 #1
            {"batch": "B1", "process": "酒精发酵", "temp": 26},        # 重复执行
            {"batch": "B1", "process": "陈酿", "temp": 17.5},          # 超温 #2 → 降级特级→一级，级联
            {"batch": "B1", "process": "苹果酸乳酸发酵", "temp": 20},  # 品质不足（需特级）
            {"batch": "B2", "process": "除梗破碎", "temp": 21},        # 正常
            {"batch": "B2", "process": "低温浸渍", "temp": 7},         # 低于下限，超温 #1
            {"batch": "B2", "process": "陈酿", "temp": 14},            # 依赖未完成（酒精发酵）
            {"batch": "B9", "process": "除梗破碎", "temp": 20},        # 批次不存在
            {"batch": "B2", "process": "蒸馏", "temp": 30},            # 工序不存在
            {"batch": "B2", "process": "酒精发酵", "temp": 27},        # 正常（补齐依赖）
            {"batch": "B2", "process": "陈酿", "temp": 14},            # 正常：依赖已补齐，重试成功
        ],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="葡萄酒酿造工序-温度联动监控工具")
    ap.add_argument("source", nargs="?", help="输入 JSON 文件（缺省读标准输入）")
    ap.add_argument("--demo", action="store_true", help="运行内置演示数据")
    ap.add_argument("--threshold", type=int, default=None,
                    help=f"超温降级阈值（默认 {DEFAULT_THRESHOLD}，覆盖输入中的 threshold）")
    args = ap.parse_args(argv)

    if args.demo:
        config = demo_config()
    else:
        try:
            text = open(args.source, encoding="utf-8").read() if args.source \
                else sys.stdin.read()
            config = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"输入读取/解析失败: {exc}", file=sys.stderr)
            return 2

    print(run(config, args.threshold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
