#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子检验判定工具（单文件，仅依赖 Python 标准库）。

输入（CSV，UTF-8，首行为表头）：
  批次定义 batches.csv:      batch_id, variety, source(可空)
  指标定义 indicators.csv:   name, threshold, direction, tolerance(可空)
      direction: 合格   -> 实测值 > 阈值 判合格（如发芽率，越高越好）
                 不合格 -> 实测值 > 阈值 判不合格（如水分，越低越好）
      tolerance: 初检/复检容许差，缺省用 --tolerance（默认 2.0）
  检验流 inspections.csv:    batch_id, indicator, value, round(初检|复检)

输出：检验结果 + 报告（复检矛盾/批次作废/级联复核等）+ 错误清单。
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROUND_INITIAL = "初检"
ROUND_RECHECK = "复检"
DIR_PASS_ABOVE = "合格"      # 超阈值判合格
DIR_FAIL_ABOVE = "不合格"    # 超阈值判不合格

# 指标/批次状态
S_QUALIFIED = "QUALIFIED"              # 合格
S_FAILED_INITIAL = "FAILED_INITIAL"    # 初检不合格，待复检
S_VOIDED = "VOIDED"                    # 复检仍不合格，作废
S_CONFLICT = "RECHECK_CONFLICT"        # 复检与初检矛盾，待仲裁
S_PENDING = "PENDING_RECHECK"          # 批次级：存在待复检指标
S_NO_DATA = "INSUFFICIENT_DATA"        # 批次级：无任何检验

DEFAULT_SOURCE = "未注明"


@dataclass
class IndicatorDef:
    name: str
    threshold: Decimal
    direction: str
    tolerance: Decimal


@dataclass
class IndicatorState:
    initial_value: Decimal | None = None
    initial_pass: bool | None = None
    recheck_value: Decimal | None = None
    recheck_pass: bool | None = None
    status: str = S_NO_DATA


@dataclass
class BatchState:
    batch_id: str
    variety: str
    source: str
    indicators: dict = field(default_factory=dict)   # name -> IndicatorState
    cascade_review: bool = False                     # 被级联复核标记
    cascade_causes: list = field(default_factory=list)


def judge(value: Decimal, ind: IndicatorDef) -> bool:
    """按指标方向判定。等于阈值不算'超阈值'。"""
    if ind.direction == DIR_PASS_ABOVE:
        return value > ind.threshold
    return value <= ind.threshold  # DIR_FAIL_ABOVE：超过才不合格


def load_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if any((v or "").strip() for v in row.values())]


def parse_decimal(text: str, what: str, errors: list) -> Decimal | None:
    try:
        return Decimal((text or "").strip())
    except InvalidOperation:
        errors.append({"code": "BAD_NUMBER", "message": f"{what} 不是合法数值: {text!r}"})
        return None


class Engine:
    def __init__(self, batches, indicators, default_tolerance):
        self.batches = batches            # id -> BatchState
        self.indicators = indicators      # name -> IndicatorDef
        self.default_tolerance = default_tolerance
        self.reports = []                 # 业务报告：矛盾/作废/级联/分歧
        self.errors = []                  # 数据与规则错误
        self._cascade_done = set()        # (作废批, 被复核批) 去重

    # ---------- 检验流处理（状态跨检验延续） ----------
    def process(self, inspections):
        for line_no, rec in enumerate(inspections, start=2):
            batch_id = (rec.get("batch_id") or "").strip()
            ind_name = (rec.get("indicator") or "").strip()
            rnd = (rec.get("round") or "").strip()
            value = parse_decimal(rec.get("value", ""), f"第{line_no}行实测值", self.errors)
            if value is None:
                continue

            batch = self.batches.get(batch_id)
            if batch is None:
                self.errors.append({"code": "UNKNOWN_BATCH", "line": line_no,
                                    "message": f"检验引用不存在的批次: {batch_id}"})
                continue
            ind = self.indicators.get(ind_name)
            if ind is None:
                self.errors.append({"code": "UNKNOWN_INDICATOR", "line": line_no,
                                    "message": f"检验引用不存在的指标: {ind_name}"})
                continue

            if self.batch_status(batch) == S_VOIDED:
                self.reports.append({"type": "INSPECTION_ON_VOIDED", "batch": batch_id,
                                     "indicator": ind_name,
                                     "message": "批次已作废，后续检验忽略（状态跨检验延续）"})
                continue

            state = batch.indicators.setdefault(ind_name, IndicatorState())

            if rnd == ROUND_INITIAL:
                if state.initial_value is not None:
                    self.errors.append({"code": "DUPLICATE_INSPECTION", "line": line_no,
                                        "message": f"重复初检被忽略: 批次{batch_id} 指标{ind_name}"})
                    continue
                self._apply_initial(batch, ind, state, value)
            elif rnd == ROUND_RECHECK:
                if state.recheck_value is not None:
                    self.errors.append({"code": "DUPLICATE_INSPECTION", "line": line_no,
                                        "message": f"重复复检被忽略: 批次{batch_id} 指标{ind_name}"})
                    continue
                self._apply_recheck(batch, ind, state, value, line_no)
            else:
                self.errors.append({"code": "BAD_ROUND", "line": line_no,
                                    "message": f"未知轮次 {rnd!r}，应为 初检/复检"})

    def _apply_initial(self, batch, ind, state, value):
        passed = judge(value, ind)
        state.initial_value, state.initial_pass = value, passed
        state.status = S_QUALIFIED if passed else S_FAILED_INITIAL

    def _apply_recheck(self, batch, ind, state, value, line_no):
        if state.initial_value is None:
            self.errors.append({"code": "RECHECK_WITHOUT_INITIAL", "line": line_no,
                                "message": f"复检缺少对应初检: 批次{batch.batch_id} 指标{ind.name}"})
            return
        if state.initial_pass:
            # 合格免复检：保留初检合格结论，复检记录为违规
            self.errors.append({"code": "UNNECESSARY_RECHECK", "line": line_no,
                                "message": f"初检已合格，免复检；复检被忽略: 批次{batch.batch_id} 指标{ind.name}"})
            return
        passed = judge(value, ind)
        state.recheck_value, state.recheck_pass = value, passed
        diff = abs(value - state.initial_value)
        flipped = passed != state.initial_pass
        over = diff > ind.tolerance

        if flipped and over:
            # 矛盾：结论翻转且差异超容许差 -> 不自动定论，待仲裁
            state.status = S_CONFLICT
            self.reports.append({
                "type": "RECHECK_CONFLICT", "batch": batch.batch_id, "indicator": ind.name,
                "initial_value": str(state.initial_value), "recheck_value": str(value),
                "diff": str(diff), "tolerance": str(ind.tolerance),
                "message": "复检与初检结论矛盾且差异超容许差，需仲裁"})
        elif flipped:
            # 容许差内的结论翻转属正常复现性波动，以复检为准
            state.status = S_QUALIFIED if passed else S_VOIDED
            if not passed:
                self._void(batch, ind, state, value)
        else:
            state.status = S_QUALIFIED if passed else S_VOIDED
            if over:
                self.reports.append({
                    "type": "VALUE_DIVERGENCE", "batch": batch.batch_id, "indicator": ind.name,
                    "initial_value": str(state.initial_value), "recheck_value": str(value),
                    "diff": str(diff), "tolerance": str(ind.tolerance),
                    "message": "两轮结论一致但数值差异超容许差，建议核查采样/操作"})
            if not passed:
                self._void(batch, ind, state, value)

    def _void(self, batch, ind, state, value):
        state.status = S_VOIDED
        self.reports.append({
            "type": "BATCH_VOIDED", "batch": batch.batch_id, "indicator": ind.name,
            "initial_value": str(state.initial_value), "recheck_value": str(value),
            "message": f"复检仍不合格，批次 {batch.batch_id} 作废"})
        self._cascade(batch)

    def _cascade(self, voided):
        """同品种同来源的关联批次级联复核（不含自身，不递归）。"""
        for other in self.batches.values():
            if other.batch_id == voided.batch_id:
                continue
            if other.variety == voided.variety and other.source == voided.source:
                key = (voided.batch_id, other.batch_id)
                if key in self._cascade_done:
                    continue
                self._cascade_done.add(key)
                other.cascade_review = True
                other.cascade_causes.append(voided.batch_id)
                self.reports.append({
                    "type": "CASCADE_REVIEW", "batch": other.batch_id,
                    "triggered_by": voided.batch_id,
                    "variety": other.variety, "source": other.source,
                    "message": f"关联批次（同品种同来源）{voided.batch_id} 作废，需级联复核"})

    # ---------- 结果汇总 ----------
    def batch_status(self, batch):
        statuses = [s.status for s in batch.indicators.values()]
        if not statuses:
            return S_NO_DATA
        if S_VOIDED in statuses:
            return S_VOIDED
        if S_CONFLICT in statuses:
            return S_CONFLICT
        if S_FAILED_INITIAL in statuses:
            return S_PENDING
        return S_QUALIFIED

    def results(self):
        out = []
        for batch in self.batches.values():
            inds = {name: {
                "status": st.status,
                "initial": None if st.initial_value is None else str(st.initial_value),
                "recheck": None if st.recheck_value is None else str(st.recheck_value),
            } for name, st in batch.indicators.items()}
            out.append({
                "batch_id": batch.batch_id, "variety": batch.variety, "source": batch.source,
                "status": self.batch_status(batch),
                "cascade_review": batch.cascade_review,
                "cascade_causes": batch.cascade_causes,
                "indicators": inds,
            })
        return out


def load_inputs(args, errors):
    batches, indicators = {}, {}
    for row in load_csv(args.batches):
        bid = (row.get("batch_id") or "").strip()
        if not bid:
            errors.append({"code": "BAD_BATCH_ROW", "message": f"批次缺少编号: {row}"})
            continue
        if bid in batches:
            errors.append({"code": "DUPLICATE_BATCH", "message": f"批次编号重复: {bid}"})
            continue
        batches[bid] = BatchState(bid, (row.get("variety") or "").strip(),
                                  (row.get("source") or "").strip() or DEFAULT_SOURCE)
    for row in load_csv(args.indicators):
        name = (row.get("name") or "").strip()
        direction = (row.get("direction") or "").strip()
        threshold = parse_decimal(row.get("threshold", ""), f"指标{name}阈值", errors)
        if not name or threshold is None:
            continue
        if direction not in (DIR_PASS_ABOVE, DIR_FAIL_ABOVE):
            errors.append({"code": "BAD_DIRECTION",
                           "message": f"指标{name}判定须为 合格/不合格，得到 {direction!r}"})
            continue
        tol_text = (row.get("tolerance") or "").strip()
        tol = parse_decimal(tol_text, f"指标{name}容许差", errors) if tol_text else args.tolerance
        if tol is None:
            continue
        if name in indicators:
            errors.append({"code": "DUPLICATE_INDICATOR", "message": f"指标重复定义: {name}"})
            continue
        indicators[name] = IndicatorDef(name, threshold, direction, tol)
    return batches, indicators


def render_text(payload) -> str:
    lines = ["== 检验结果 =="]
    for r in payload["results"]:
        flag = " [级联复核]" if r["cascade_review"] else ""
        lines.append(f"批次 {r['batch_id']}（{r['variety']}/{r['source']}）: {r['status']}{flag}")
        for name, st in r["indicators"].items():
            lines.append(f"  - {name}: {st['status']} 初检={st['initial']} 复检={st['recheck']}")
    lines.append("== 报告 ==")
    lines += [f"[{r['type']}] {r['message']} " +
              " ".join(f"{k}={v}" for k, v in r.items() if k not in ("type", "message"))
              for r in payload["reports"]] or ["（无）"]
    lines.append("== 错误清单 ==")
    lines += [f"[{e['code']}] {e['message']}" for e in payload["errors"]] or ["（无）"]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="种子检验判定工具（纯标准库单文件）")
    ap.add_argument("--batches", required=True, help="批次定义 CSV: batch_id,variety,source")
    ap.add_argument("--indicators", required=True, help="指标定义 CSV: name,threshold,direction[,tolerance]")
    ap.add_argument("--inspections", required=True, help="检验流 CSV: batch_id,indicator,value,round")
    ap.add_argument("--tolerance", type=Decimal, default=Decimal("2.0"),
                    help="默认初/复检容许差（指标可单独覆盖），默认 2.0")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    errors = []
    batches, indicators = load_inputs(args, errors)
    engine = Engine(batches, indicators, args.tolerance)
    engine.errors[:0] = errors
    engine.process(load_csv(args.inspections))

    payload = {"results": engine.results(), "reports": engine.reports, "errors": engine.errors}
    if args.format == "json":
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_text(payload))
    return 1 if engine.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
