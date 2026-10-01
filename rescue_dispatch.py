#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登山救援派遣工具（纯标准库，单文件）。

规则模型（自定规则与理由）：
1. 警情需求 = 准入资质集合 + 所需人数。
   - 准入资质：每位出勤队员都必须具备警情列出的全部资质（出勤即要能在
     现场互相替补，资质是准入门槛而非分工），缺一项即报 QUALIFICATION_MISSING。
   - 所需人数：物理到场人数（含资质不合规者，人已在现场）少于 count 时，
     报 SHORTAGE 并给出缺口数；可用队员不足时缺口按实际可派人数计算。
2. 天气规则：每次派遣可随队携带装备，每件装备都必须适应该警情当前天气，
   任一不适应即报 EQUIPMENT_WEATHER（理由：山岳救援中一件关键装备失效即
   危及全队，故采用最严格的全通过规则）。装备适用天气含 "任意" 视为全天候。
   天气升级时对已携带装备级联重检。
3. 状态机：队员一旦计入某警情即进入"占用"状态，跨警情延续、不可再派
   （重复派往其他警情报 MEMBER_BUSY）。同一警情重复派遣同一人为幂等操作，
   静默去重；同一次派遣名单内重复报 DUPLICATE_ENTRY。
4. 警情升级：同一警情名再次出现即升级，资质/人数/天气按新值整体替换，
   随后对已派队员与已携带装备做级联重检（新增资质缺失、天气不再允许均会
   被重新报告）。已派队员留在现场，不因重检失败而撤回。
5. 引用校验：引用不存在的警情/队员/装备分别报 UNKNOWN_ALERT /
   UNKNOWN_MEMBER / UNKNOWN_EQUIPMENT。

输入（JSON，见 load_payload）：
{
  "members":   [{"name": "...", "quals": ["急救", ...]}, ...],
  "equipment": [{"name": "...", "weathers": ["晴", ...]}, ...],
  "events":    [                          # 警情流与派遣流按时间合并
    {"type": "alert",   "name": "...", "location": "...", "weather": "...",
     "quals": ["急救", ...], "count": 2},
    {"type": "upgrade", "name": "...", "quals": [...], "count": 3,
     "weather": "..."},                   # 均可选，缺省沿用旧值
    {"type": "dispatch", "alert": "...", "members": ["...", ...],
     "equipment": ["...", ...]}           # equipment 可省略
  ]
}
也兼容分开给出 "alerts" / "dispatches" 两个列表（先全部警情后全部派遣）。

用法：
  python3 rescue_dispatch.py            # 运行内置示例（覆盖全部规则）
  python3 rescue_dispatch.py case.json  # 运行自定义 JSON 用例
  python3 rescue_dispatch.py --json     # 以 JSON 输出结构化结果
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field

ANY_WEATHER = "任意"

ERROR_LABELS = {
    "UNKNOWN_ALERT": "引用不存在的警情",
    "UNKNOWN_MEMBER": "引用不存在的队员",
    "UNKNOWN_EQUIPMENT": "引用不存在的装备",
    "MEMBER_BUSY": "队员已被派往其他警情",
    "DUPLICATE_ENTRY": "同一派遣名单内队员重复",
    "QUALIFICATION_MISSING": "队员资质不满足警情要求",
    "EQUIPMENT_WEATHER": "装备不适用于当前天气",
    "SHORTAGE": "警情所需人数不足",
}


@dataclass
class Alert:
    name: str
    location: str = ""
    weather: str = ""
    quals: list = field(default_factory=list)   # 准入资质（全员须具备）
    count: int = 0                              # 所需人数
    assigned: list = field(default_factory=list)  # 已派队员（有序去重）
    equipment: list = field(default_factory=list)  # 已携带装备（有序去重）


class RescueEngine:
    """按事件流顺序处理警情与派遣，维护跨警情延续的队员状态。"""

    def __init__(self, members, equipment):
        self.members = {m["name"]: set(m.get("quals", [])) for m in members}
        self.equipment = {e["name"]: set(e.get("weathers", [])) for e in equipment}
        self.alerts = {}          # name -> Alert
        self.member_status = {}   # 队员 -> 所在警情名（占用中）
        self.errors = []          # 错误清单
        self.results = []         # 派遣结果（每次派遣一条）

    # ------------------------------------------------------------------ 工具
    def _err(self, code, **kw):
        self.errors.append({"code": code, "label": ERROR_LABELS[code], **kw})

    def _check_member_quals(self, alert, member):
        """级联资质重检：队员缺少警情当前任一准入资质即报告。"""
        missing = sorted(set(alert.quals) - self.members[member])
        if missing:
            self._err("QUALIFICATION_MISSING", alert=alert.name,
                      member=member, missing=missing)

    def _check_equipment_weather(self, alert, equip):
        """天气规则：装备须适应警情当前天气（'任意' 全天候）。"""
        ok = self.equipment[equip]
        if alert.weather and alert.weather not in ok and ANY_WEATHER not in ok:
            self._err("EQUIPMENT_WEATHER", alert=alert.name, equipment=equip,
                      weather=alert.weather, suitable=sorted(ok))

    def _check_shortage(self, alert):
        """人数校验：物理到场人数不足即报告缺口。"""
        gap = alert.count - len(alert.assigned)
        if alert.count and gap > 0:
            self._err("SHORTAGE", alert=alert.name, required=alert.count,
                      assigned=len(alert.assigned), gap=gap)

    # ---------------------------------------------------------------- 事件处理
    def upsert_alert(self, name, location=None, weather=None, quals=None,
                     count=None, upgrade=False):
        """登记新警情，或对既有警情做升级并级联重检已派队员/装备。"""
        alert = self.alerts.get(name)
        if alert is None:
            alert = Alert(name=name)
            self.alerts[name] = alert
        if location is not None:
            alert.location = location
        if weather is not None:
            alert.weather = weather
        if quals is not None:
            alert.quals = list(quals)
        if count is not None:
            alert.count = int(count)
        if upgrade:
            for member in alert.assigned:      # 级联重检：新增资质
                self._check_member_quals(alert, member)
            for equip in alert.equipment:      # 级联重检：天气变化
                self._check_equipment_weather(alert, equip)
            self._check_shortage(alert)        # 级联重检：人数上调
        return alert

    def dispatch(self, alert_name, member_names, equip_names=None):
        """处理一次派遣：校验引用/状态/资质/天气，更新队员状态并记录结果。"""
        alert = self.alerts.get(alert_name)
        if alert is None:
            self._err("UNKNOWN_ALERT", alert=alert_name, members=list(member_names))
            return

        seen = set()
        for m in member_names:                 # 名单内部重复
            if m in seen:
                self._err("DUPLICATE_ENTRY", alert=alert_name, member=m)
            seen.add(m)

        assigned_now, skipped = [], []
        for m in dict.fromkeys(member_names):  # 保序去重
            if m not in self.members:
                self._err("UNKNOWN_MEMBER", alert=alert_name, member=m)
                skipped.append(m)
                continue
            if m in alert.assigned:            # 同一警情重复派遣：幂等
                continue
            busy_at = self.member_status.get(m)
            if busy_at is not None:            # 跨警情占用：状态延续，不可再派
                self._err("MEMBER_BUSY", member=m, busy_alert=busy_at,
                          alert=alert_name)
                skipped.append(m)
                continue
            alert.assigned.append(m)           # 状态级联更新：立即占用
            self.member_status[m] = alert_name
            assigned_now.append(m)
            self._check_member_quals(alert, m)

        for e in dict.fromkeys(equip_names or []):
            if e not in self.equipment:
                self._err("UNKNOWN_EQUIPMENT", alert=alert_name, equipment=e)
                continue
            if e not in alert.equipment:
                alert.equipment.append(e)
            self._check_equipment_weather(alert, e)

        self._check_shortage(alert)
        self.results.append({
            "alert": alert_name,
            "assigned": assigned_now,
            "skipped": skipped,
            "on_scene": list(alert.assigned),
            "required": alert.count,
            "gap": max(0, alert.count - len(alert.assigned)),
        })

    # ---------------------------------------------------------------- 驱动
    def run(self, events):
        for ev in events:
            kind = ev.get("type")
            if kind == "alert":
                self.upsert_alert(ev["name"], ev.get("location"),
                                  ev.get("weather"), ev.get("quals"),
                                  ev.get("count"))
            elif kind == "upgrade":
                if ev["name"] not in self.alerts:
                    self._err("UNKNOWN_ALERT", alert=ev["name"])
                    continue
                self.upsert_alert(ev["name"], ev.get("location"),
                                  ev.get("weather"), ev.get("quals"),
                                  ev.get("count"), upgrade=True)
            elif kind == "dispatch":
                self.dispatch(ev["alert"], ev.get("members", []),
                              ev.get("equipment"))
            else:
                raise ValueError("未知事件类型: %r" % (kind,))
        return self.report()

    def report(self):
        return {
            "results": self.results,
            "errors": self.errors,
            "alerts": {n: {"location": a.location, "weather": a.weather,
                           "quals": a.quals, "count": a.count,
                           "assigned": a.assigned, "equipment": a.equipment}
                       for n, a in self.alerts.items()},
            "member_status": dict(self.member_status),
        }


# --------------------------------------------------------------------- 输出
def format_report(rep):
    lines = ["===== 派遣结果 ====="]
    if not rep["results"]:
        lines.append("（无派遣）")
    for i, r in enumerate(rep["results"], 1):
        lines.append("[派遣%d] 警情「%s」" % (i, r["alert"]))
        lines.append("  本次派出: %s" % ("、".join(r["assigned"]) or "无"))
        if r["skipped"]:
            lines.append("  未派出  : %s" % "、".join(r["skipped"]))
        lines.append("  现场队员: %s（需 %d 人，缺口 %d）"
                     % ("、".join(r["on_scene"]) or "无",
                        r["required"], r["gap"]))
    lines.append("")
    lines.append("===== 错误清单（%d 条）=====" % len(rep["errors"]))
    if not rep["errors"]:
        lines.append("（无错误）")
    for i, e in enumerate(rep["errors"], 1):
        detail = "，".join("%s=%s" % (k, v) for k, v in e.items()
                           if k not in ("code", "label"))
        lines.append("%2d. [%s] %s（%s）" % (i, e["code"], e["label"], detail))
    lines.append("")
    lines.append("===== 队员状态 =====")
    busy = {m: a for m, a in rep["member_status"].items()}
    lines.append("占用中: %s" % ("、" .join("%s→%s" % kv for kv in busy.items())
                                or "无"))
    return "\n".join(lines)


# --------------------------------------------------------------------- 输入
def load_payload(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    events = list(data.get("events", []))
    for a in data.get("alerts", []):
        events.append({"type": "alert", **a})
    for d in data.get("dispatches", []):
        events.append({"type": "dispatch", **d})
    return data.get("members", []), data.get("equipment", []), events


# --------------------------------------------------------------------- 示例
def demo_payload():
    members = [
        {"name": "张三", "quals": ["急救", "攀岩"]},
        {"name": "李四", "quals": ["急救", "驾驶"]},
        {"name": "王五", "quals": ["攀岩"]},
        {"name": "赵六", "quals": ["急救", "攀岩", "驾驶"]},
    ]
    equipment = [
        {"name": "绳索包", "weathers": ["晴", "阴", "雪"]},
        {"name": "雪地锚", "weathers": ["雪"]},
        {"name": "无人机", "weathers": ["晴"]},
        {"name": "急救箱", "weathers": ["任意"]},
    ]
    events = [
        {"type": "alert", "name": "A1-坠崖", "location": "北坡",
         "weather": "晴", "quals": ["急救", "攀岩"], "count": 2},
        # 正常派遣：张三、李四 出勤（李四缺攀岩 -> QUALIFICATION_MISSING）
        {"type": "dispatch", "alert": "A1-坠崖",
         "members": ["张三", "李四"], "equipment": ["绳索包", "急救箱"]},
        # 王五缺急救；雪地锚不适晴天 -> EQUIPMENT_WEATHER（无人机晴天可用）
        {"type": "dispatch", "alert": "A1-坠崖",
         "members": ["王五"], "equipment": ["无人机", "雪地锚"]},
        # 张三已在 A1（同警情重复派遣，幂等忽略）；钱七不存在 -> UNKNOWN_MEMBER
        {"type": "dispatch", "alert": "A1-坠崖",
         "members": ["张三", "钱七"]},
        # 名单内重复 -> DUPLICATE_ENTRY；赵六补位后人数达标
        {"type": "dispatch", "alert": "A1-坠崖", "members": ["赵六", "赵六"]},
        # 升级：新增“驾驶”资质、人数 2->5、天气转雪
        #   -> 已派张三/王五缺驾驶（级联重检 QUALIFICATION_MISSING）
        #   -> 无人机不适雪天（级联重检 EQUIPMENT_WEATHER），雪地锚转可用
        #   -> 现场 4 人 < 5 人（级联重检 SHORTAGE，缺口 1）
        {"type": "upgrade", "name": "A1-坠崖", "weather": "雪",
         "quals": ["急救", "攀岩", "驾驶"], "count": 5},
        {"type": "alert", "name": "B2-雪崩", "location": "西沟",
         "weather": "雪", "quals": ["急救"], "count": 1},
        # 全员已被 A1 占用 -> MEMBER_BUSY；人数缺口 1 -> SHORTAGE
        {"type": "dispatch", "alert": "B2-雪崩",
         "members": ["赵六"], "equipment": ["雪地锚"]},
        # 引用不存在的警情 / 装备
        {"type": "dispatch", "alert": "C9-不存在", "members": ["张三"]},
        {"type": "dispatch", "alert": "B2-雪崩", "members": [],
         "equipment": ["热成像仪"]},
    ]
    return members, equipment, events


def main(argv):
    as_json = "--json" in argv
    paths = [a for a in argv[1:] if a != "--json"]
    if paths:
        members, equipment, events = load_payload(paths[0])
    else:
        members, equipment, events = demo_payload()
    rep = RescueEngine(members, equipment).run(events)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_report(rep))
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
