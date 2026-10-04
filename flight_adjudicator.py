#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flight_adjudicator.py — 无人机飞行裁决工具（纯 Python 标准库，单文件）

输入模型
--------
- 航段库 segments:  {航段名: ((x1,y1),(x2,y2))}      # 二维平面几何，用于交叉判定
- 空域   airspaces: [{name, type: 禁飞|限飞, segments: [航段名...]}]
- 无人机 drones:    [{id, type: 巡检|物流}]
- 飞行流 requests:  [{id, drone, route: [航段名...], altitude}]
- 事件   events:    [{request: 申请编号, new_altitude: 新高度}]   # 高度变更事件

自定规则（理由）
----------------
1. 禁飞区：所有机型禁入，命中即拒绝并报告（申请、空域、航段）。
2. 限飞区：物流机禁入、巡检机允许。
   理由：限飞区多因地面设施/人群密集而设，物流机载货飞越风险高；
   而巡检机轻载且往往正是为这些设施做巡检，故放行。
3. 高度冲突阈值 30 米：两机航线交叉且 |高度差| < 30 视为冲突。
4. 优先级：巡检 > 物流（巡检多为应急/基础设施保障），同类按申请编号小者优先。
   冲突时低优先级申请被拒绝。
5. 同机重复申请：保留编号最小者，其余报告并拒绝。
6. 级联重检：按优先级从高到低逐个裁决，每个申请只与"已通过"集合比对，
   被拒绝的航线自动退出空域/冲突检查，不会级联误伤后续申请；
   高度变更事件应用后，整体按同一流程重跑裁决并输出差异。

用法
----
    python3 flight_adjudicator.py            # 运行内置示例（覆盖全部报告类型）
    python3 flight_adjudicator.py input.json # 从 JSON 文件加载自定义输入
"""

import json
import sys

ALT_THRESHOLD = 30.0          # 高度相近阈值（米）
DRONE_PRIORITY = {"巡检": 0, "物流": 1}   # 数值越小优先级越高


# ---------------------------------------------------------------- 几何

def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segments_intersection(p1, p2, p3, p4):
    """返回两线段交点 (x,y)；共线重叠返回重叠段中点；不相交返回 None。"""
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        # 严格相交，求交点
        t = d1 / (d1 - d2)
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))

    def on_seg(a, b, p):
        return min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9 and \
               min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9

    # 端点接触 / 共线
    for p, a, b in ((p1, p3, p4), (p2, p3, p4), (p3, p1, p2), (p4, p1, p2)):
        if abs(_cross(a, b, p)) < 1e-9 and on_seg(a, b, p):
            return p
    return None


def route_crossings(route_a, route_b, segments):
    """返回两条航线的所有交叉点 [(航段A, 航段B, (x,y))]，按物理点去重。"""
    hits = []
    seen = set()
    for sa in route_a:
        for sb in route_b:
            if sa not in segments or sb not in segments:
                continue
            pt = segments_intersection(*segments[sa], *segments[sb])
            if pt is not None:
                key = (round(pt[0], 6), round(pt[1], 6))
                if key not in seen:
                    seen.add(key)
                    hits.append((sa, sb, pt))
    return hits


# ---------------------------------------------------------------- 裁决

def adjudicate(segments, airspaces, drones, requests):
    """对飞行流做一轮完整裁决。返回 (results, errors)。

    results: {申请编号: {"status": "通过"|"拒绝", "reasons": [...]}}
    errors:  错误/冲突报告字符串列表（按发现顺序）
    """
    errors = []
    results = {}

    # ---- 1. 引用合法性检查（不存在的无人机 / 航段）----
    valid = []
    for req in requests:
        rid = req["id"]
        problems = []
        if req["drone"] not in drones:
            problems.append("引用了不存在的无人机 %s" % req["drone"])
            errors.append("[引用错误] 申请 %s：无人机 %s 不存在" % (rid, req["drone"]))
        missing = [s for s in req["route"] if s not in segments]
        for s in missing:
            errors.append("[引用错误] 申请 %s：航段 %s 不存在" % (rid, s))
        if missing:
            problems.append("引用了不存在的航段 %s" % ",".join(missing))
        # 空域引用的航段也校验一次（只报告一次）
        if problems:
            results[rid] = {"status": "拒绝", "reasons": problems}
        else:
            valid.append(req)

    for asp in airspaces:
        for s in asp["segments"]:
            if s not in segments:
                errors.append("[引用错误] 空域 %s：覆盖的航段 %s 不存在" % (asp["name"], s))

    # ---- 2. 同机重复申请：保留编号最小者 ----
    by_drone = {}
    for req in valid:
        by_drone.setdefault(req["drone"], []).append(req)
    deduped = []
    for drone_id, reqs in by_drone.items():
        reqs.sort(key=lambda r: r["id"])
        deduped.append(reqs[0])
        for dup in reqs[1:]:
            errors.append("[重复申请] 无人机 %s 重复申请：保留 %s，拒绝 %s"
                          % (drone_id, reqs[0]["id"], dup["id"]))
            results[dup["id"]] = {"status": "拒绝",
                                  "reasons": ["与申请 %s 为同机重复申请" % reqs[0]["id"]]}

    # ---- 3. 按优先级排序，级联裁决 ----
    def prio(req):
        return (DRONE_PRIORITY.get(drones[req["drone"]], 99), req["id"])

    approved = []   # 已通过集合：后续申请只与它比对，实现级联
    for req in sorted(deduped, key=prio):
        rid = req["id"]
        dtype = drones[req["drone"]]
        reasons = []

        # 3a. 空域检查
        for asp in airspaces:
            hit = [s for s in req["route"] if s in asp["segments"]]
            for s in hit:
                if asp["type"] == "禁飞":
                    errors.append("[禁飞区] 申请 %s 命中禁飞区 %s（航段 %s）"
                                  % (rid, asp["name"], s))
                    reasons.append("航段 %s 命中禁飞区 %s" % (s, asp["name"]))
                elif asp["type"] == "限飞" and dtype == "物流":
                    errors.append("[限飞区] 申请 %s（物流）进入限飞区 %s（航段 %s），"
                                  "按规则物流机禁入" % (rid, asp["name"], s))
                    reasons.append("物流机禁入限飞区 %s（航段 %s）" % (asp["name"], s))

        # 3b. 与已通过航线交叉 + 高度相近检查（已被空域规则拒绝的不再重复检查）
        for other in approved if not reasons else ():
            if abs(req["altitude"] - other["altitude"]) >= ALT_THRESHOLD:
                continue
            for sa, sb, pt in route_crossings(req["route"], other["route"], segments):
                errors.append("[航线冲突] 申请 %s（无人机 %s）与申请 %s（无人机 %s）"
                              "在航段 %s/%s 交叉，交叉点 (%.1f, %.1f)，"
                              "高度 %.0f/%.0f 间隔不足 %.0f 米；按优先级拒绝 %s"
                              % (rid, req["drone"], other["id"], other["drone"],
                                 sa, sb, pt[0], pt[1],
                                 req["altitude"], other["altitude"],
                                 ALT_THRESHOLD, rid))
                reasons.append("与已通过申请 %s 在 (%.1f, %.1f) 冲突且高度相近"
                               % (other["id"], pt[0], pt[1]))

        if reasons:
            results[rid] = {"status": "拒绝", "reasons": reasons}
        else:
            results[rid] = {"status": "通过", "reasons": []}
            approved.append(req)

    return results, errors


def print_report(title, results, errors, requests):
    print("=" * 64)
    print(title)
    print("=" * 64)
    print("【飞行裁决】")
    for req in requests:
        rid = req["id"]
        r = results.get(rid, {"status": "拒绝", "reasons": ["未参与裁决"]})
        line = "  申请 %-4s 无人机 %-4s 高度 %-5.0f -> %s" % (
            rid, req["drone"], req["altitude"], r["status"])
        if r["reasons"]:
            line += "（" + "；".join(r["reasons"]) + "）"
        print(line)
    print("【错误/冲突清单】共 %d 条" % len(errors))
    for i, e in enumerate(errors, 1):
        print("  %2d. %s" % (i, e))
    print()


def run(data):
    segments = {k: (tuple(v[0]), tuple(v[1])) for k, v in data["segments"].items()}
    airspaces = data["airspaces"]
    drones = {d["id"]: d["type"] for d in data["drones"]}
    requests = data["requests"]
    events = data.get("events", [])

    # 第一轮裁决
    results, errors = adjudicate(segments, airspaces, drones, requests)
    print_report("第一轮裁决结果", results, errors, requests)

    # 高度变更事件 -> 级联重检
    for ev in events:
        for req in requests:
            if req["id"] == ev["request"]:
                print(">> 高度变更事件：申请 %s 高度 %.0f -> %.0f，触发级联重检"
                      % (ev["request"], req["altitude"], ev["new_altitude"]))
                req["altitude"] = ev["new_altitude"]
        before = {k: v["status"] for k, v in results.items()}
        results, errors = adjudicate(segments, airspaces, drones, requests)
        print_report("事件后重检裁决结果", results, errors, requests)
        diff = [rid for rid in results if before.get(rid) != results[rid]["status"]]
        if diff:
            print("【级联影响】状态变化的申请：%s" % ", ".join(
                "%s(%s->%s)" % (rid, before.get(rid, "-"), results[rid]["status"])
                for rid in diff))
        else:
            print("【级联影响】无申请状态变化")
        print()


# ---------------------------------------------------------------- 示例数据

DEMO = {
    "segments": {
        "S1": [[0, 0], [10, 0]],
        "S2": [[10, 0], [10, 10]],
        "S3": [[10, 10], [0, 10]],
        "S4": [[0, 10], [0, 0]],
        "S5": [[0, 0], [10, 10]],
        "S6": [[0, 10], [10, 0]],
        "S7": [[20, 0], [30, 0]],
        "S8": [[10, 0], [20, 0]],
    },
    "airspaces": [
        {"name": "NF1", "type": "禁飞", "segments": ["S3"]},
        {"name": "RF1", "type": "限飞", "segments": ["S2"]},
    ],
    "drones": [
        {"id": "D1", "type": "巡检"},
        {"id": "D2", "type": "物流"},
        {"id": "D3", "type": "巡检"},
        {"id": "D4", "type": "物流"},
        {"id": "D5", "type": "物流"},
    ],
    "requests": [
        # R1: 巡检机过限飞区 RF1 -> 按规则放行（演示限飞区按机型区别对待）
        {"id": "R1", "drone": "D1", "route": ["S1", "S2"], "altitude": 200},
        # R2: 物流机进限飞区 RF1 -> 拒绝（演示限飞区报告）
        {"id": "R2", "drone": "D2", "route": ["S8", "S2"], "altitude": 100},
        # R3(物流) 与 R4(巡检) 在 (5,5) 交叉、高度差 10 < 30 -> 巡检优先，R3 被拒
        {"id": "R3", "drone": "D4", "route": ["S5"], "altitude": 120},
        {"id": "R4", "drone": "D3", "route": ["S6"], "altitude": 110},
        # R5: 与 R1 同机(D1)重复申请 -> 报告并拒绝
        {"id": "R5", "drone": "D1", "route": ["S7"], "altitude": 150},
        # R6: 引用不存在的无人机 -> 报告
        {"id": "R6", "drone": "D9", "route": ["S1"], "altitude": 100},
        # R7: 引用不存在的航段 -> 报告
        {"id": "R7", "drone": "D5", "route": ["S99"], "altitude": 100},
        # R8: 命中禁飞区 NF1 -> 报告并拒绝
        {"id": "R8", "drone": "D5", "route": ["S3"], "altitude": 90},
    ],
    "events": [
        # R4 升到 150 后与 R3 高度差 40 >= 30 -> 重检后 R3 级联恢复为通过
        {"request": "R4", "new_altitude": 150},
    ],
}


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = DEMO
    run(data)


if __name__ == "__main__":
    main()
