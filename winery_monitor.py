#!/usr/bin/env python3
"""葡萄酒酿造温控监控工具（纯 Python 标准库，单文件）。

输入（JSON 文件或 stdin）：
{
  "overtemp_threshold": 3,                 # 可选，超温降级阈值，默认 3
  "batches":   [{"id": "B1", "variety": "赤霞珠", "target_quality": "特级"}],
  "processes": [{"name": "发酵", "temp_min": 18, "temp_max": 28,
                 "deps": ["破碎"], "min_quality": "三级"}],
  "events":    [{"batch": "B1", "process": "发酵", "temp": 25.5}]
}

输出（JSON 到 stdout）：{"batches": {...各批次状态...}, "errors": [...错误清单...]}

错误类型：
  unknown_batch / unknown_process   引用不存在的批次或工序
  dependency_not_met                依赖工序未完成
  repeat_process                    工序重复执行
  temp_out_of_range                 实测温度超出温控区间（含超温值）
  quality_insufficient              批次当前品质低于工序要求（降级级联）
  batch_downgraded                  超温累计达阈值，批次降级（通知类记录）
"""

import argparse
import json
import sys
from dataclasses import dataclass, field

# 品质等级由低到高；索引即等级数值，便于比较与降级运算
QUALITY_LEVELS = ["等外", "三级", "二级", "一级", "特级"]

# 默认超温降级阈值：3 次。
# 理由：发酵中短暂小幅超温可经人工干预（换热、搅拌）恢复，单次即降级过于敏感；
# 累计 3 次说明温控系统性失控，不良风味物质积累不可逆，应降级处理。
DEFAULT_OVERTEMP_THRESHOLD = 3


@dataclass
class ProcessDef:
    name: str
    temp_min: float
    temp_max: float
    deps: list = field(default_factory=list)
    min_quality: str = "等外"  # 执行该工序要求批次达到的最低品质


@dataclass
class BatchState:
    batch_id: str
    variety: str
    target_quality: str
    current_quality: str
    completed: list = field(default_factory=list)   # 已完成工序（按完成顺序）
    overtemp_count: int = 0                          # 距上次降级以来的超温次数
    downgrade_count: int = 0


def quality_index(name):
    return QUALITY_LEVELS.index(name)


def load_input(path):
    try:
        raw = open(path, encoding="utf-8").read() if path != "-" else sys.stdin.read()
    except OSError as exc:
        sys.exit(f"无法读取输入文件: {exc}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"输入不是合法 JSON: {exc}")


def build_definitions(data):
    """解析批次与工序定义，定义层面的错误直接拒绝启动。"""
    errors = []
    for q in [b.get("target_quality") for b in data.get("batches", [])]:
        if q not in QUALITY_LEVELS:
            sys.exit(f"批次目标品质非法: {q!r}，合法值: {QUALITY_LEVELS}")

    batches = {}
    for b in data.get("batches", []):
        if b["id"] in batches:
            sys.exit(f"批次定义重复: {b['id']}")
        batches[b["id"]] = BatchState(
            batch_id=b["id"], variety=b.get("variety", ""),
            target_quality=b["target_quality"],
            current_quality=b["target_quality"],
        )

    processes = {}
    for p in data.get("processes", []):
        if p["name"] in processes:
            sys.exit(f"工序定义重复: {p['name']}")
        if p["temp_min"] > p["temp_max"]:
            sys.exit(f"工序 {p['name']} 温控区间非法: {p['temp_min']} > {p['temp_max']}")
        mq = p.get("min_quality", "等外")
        if mq not in QUALITY_LEVELS:
            sys.exit(f"工序 {p['name']} 的 min_quality 非法: {mq!r}")
        processes[p["name"]] = ProcessDef(
            name=p["name"], temp_min=float(p["temp_min"]), temp_max=float(p["temp_max"]),
            deps=list(p.get("deps", [])), min_quality=mq,
        )
    for p in processes.values():
        for d in p.deps:
            if d not in processes:
                sys.exit(f"工序 {p.name} 依赖未定义的工序: {d}")
    return batches, processes, errors


def downgrade(batch, threshold, seq, errors):
    """批次降一级；已在最低级则保持并记录。"""
    idx = quality_index(batch.current_quality)
    if idx == 0:
        errors.append({
            "seq": seq, "type": "batch_downgraded", "batch": batch.batch_id,
            "process": None,
            "message": f"批次 {batch.batch_id} 超温累计达阈值 {threshold}，"
                       f"但已是最低品质等外，无法继续降级",
        })
        return
    old = batch.current_quality
    batch.current_quality = QUALITY_LEVELS[idx - 1]
    batch.downgrade_count += 1
    errors.append({
        "seq": seq, "type": "batch_downgraded", "batch": batch.batch_id,
        "process": None,
        "message": f"批次 {batch.batch_id} 超温累计达阈值 {threshold}，"
                   f"品质由 {old} 降级为 {batch.current_quality}；"
                   f"后续工序将按新品质重新校验要求",
    })


def process_event(seq, event, batches, processes, threshold, errors):
    batch_id = event.get("batch")
    proc_name = event.get("process")
    temp = event.get("temp")

    if batch_id not in batches:
        errors.append({"seq": seq, "type": "unknown_batch", "batch": batch_id,
                       "process": proc_name,
                       "message": f"事件引用了不存在的批次: {batch_id!r}"})
        return
    if proc_name not in processes:
        errors.append({"seq": seq, "type": "unknown_process", "batch": batch_id,
                       "process": proc_name,
                       "message": f"事件引用了不存在的工序: {proc_name!r}"})
        return

    batch = batches[batch_id]
    proc = processes[proc_name]

    if proc_name in batch.completed:
        errors.append({"seq": seq, "type": "repeat_process", "batch": batch_id,
                       "process": proc_name,
                       "message": f"批次 {batch_id} 的工序 {proc_name} 已完成，"
                                  f"不允许重复执行"})
        return

    missing = [d for d in proc.deps if d not in batch.completed]
    if missing:
        errors.append({"seq": seq, "type": "dependency_not_met", "batch": batch_id,
                       "process": proc_name,
                       "message": f"批次 {batch_id} 执行 {proc_name} 前，"
                                  f"依赖工序未完成: {missing}"})
        return

    # 降级级联：工序品质要求按批次“当前”品质校验，而非目标品质
    if quality_index(batch.current_quality) < quality_index(proc.min_quality):
        errors.append({"seq": seq, "type": "quality_insufficient", "batch": batch_id,
                       "process": proc_name,
                       "message": f"批次 {batch_id} 当前品质 {batch.current_quality} "
                                  f"低于工序 {proc_name} 要求的最低品质 "
                                  f"{proc.min_quality}，工序被拒绝"})
        return

    if not isinstance(temp, (int, float)):
        errors.append({"seq": seq, "type": "invalid_temp", "batch": batch_id,
                       "process": proc_name,
                       "message": f"实测温度缺失或不是数值: {temp!r}"})
        return

    # 温度越界：记录超温值并累计；工序本身仍视为已执行（活儿干了，但质量受损）
    deviation = 0.0
    direction = None
    if temp > proc.temp_max:
        deviation = round(temp - proc.temp_max, 2)
        direction = "超上限"
    elif temp < proc.temp_min:
        deviation = round(proc.temp_min - temp, 2)
        direction = "低于下限"
    if direction:
        batch.overtemp_count += 1
        errors.append({
            "seq": seq, "type": "temp_out_of_range", "batch": batch_id,
            "process": proc_name,
            "message": f"批次 {batch_id} 工序 {proc_name} 实测 {temp}℃ "
                       f"{direction} [{proc.temp_min}, {proc.temp_max}]℃，"
                       f"偏差 {deviation}℃（本批次第 {batch.overtemp_count} 次超温）",
            "temp": temp, "deviation": deviation, "direction": direction,
        })
        if batch.overtemp_count >= threshold:
            downgrade(batch, threshold, seq, errors)
            batch.overtemp_count = 0  # 降级后重新累计，允许连续多级降级

    batch.completed.append(proc_name)


def run(data):
    threshold = int(data.get("overtemp_threshold", DEFAULT_OVERTEMP_THRESHOLD))
    if threshold < 1:
        sys.exit("overtemp_threshold 必须 >= 1")
    batches, processes, errors = build_definitions(data)

    for seq, event in enumerate(data.get("events", []), start=1):
        process_event(seq, event, batches, processes, threshold, errors)

    all_procs = set(processes)
    status = {}
    for bid, b in batches.items():
        pending = sorted(all_procs - set(b.completed))
        status[bid] = {
            "variety": b.variety,
            "target_quality": b.target_quality,
            "current_quality": b.current_quality,
            "status": "已完成" if not pending else "进行中",
            "completed": b.completed,
            "pending": pending,
            "overtemp_count": b.overtemp_count,
            "downgrade_count": b.downgrade_count,
        }
    return {"overtemp_threshold": threshold, "batches": status, "errors": errors}


def main():
    ap = argparse.ArgumentParser(description="葡萄酒酿造温控监控工具")
    ap.add_argument("input", nargs="?", default="-",
                    help="输入 JSON 文件路径，缺省或 '-' 表示 stdin")
    args = ap.parse_args()
    report = run(load_input(args.input))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
