#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rescue_dispatch.py — 登山救援派遣校验工具（纯 Python 标准库，单文件）

用法：
    python3 rescue_dispatch.py 输入文件 [更多文件...]   # 从文件读取指令流
    python3 rescue_dispatch.py                          # 从标准输入读取
    python3 rescue_dispatch.py --demo                   # 运行内置示例
    python3 rescue_dispatch.py --json 输入文件          # 以 JSON 输出

指令流格式（# 开头为注释，名单用英文/中文逗号分隔，可用引号包裹含空格的名称）：
    member     队员名 资质1,资质2            # 定义队员（资质如：急救、攀岩、驾驶）
    equipment  装备名 天气1,天气2            # 定义装备及其适用天气
    incident   警情名 地点 资质1,资质2 天气   # 定义警情（同名再次出现视为升级，见下）
    upgrade    警情名 新增资质1,新增资质2     # 警情升级：新增需求并级联重检已派队员
    dispatch   警情名 队员1,队员2 [装备1,装备2]  # 派遣；装备省略时默认全队装备出动
    （member/equipment/incident/upgrade/dispatch 也可用中文：队员/装备/警情/升级/派遣）

规则说明（自定部分的理由）：
 1. 资质：派遣队员必须具备警情要求的【全部】资质。山地救援现场无法分工替补，
    缺任一项即存在安全隐患，故缺资质的队员整人拒派并报告（警情、队员、缺资质）。
 2. 天气/装备：每次派遣默认全队装备随车出动（也可在 dispatch 中显式指定装备清单）。
    任一装备不适用该警情天气，即视为出动条件不满足，报告并【阻止本次派遣】
    （装备不达标不出动，宁可误报不可漏报）。
 3. 人数：每种必需资质至少需 1 名具备该资质的队员在岗（专人专岗），
    故需求人数 = 必需资质种数；在岗不足时报告缺口。
 4. 状态级联：队员一旦被派遣即进入"已派遣"状态，跨警情延续，不可再派往其他警情；
    同警情重复派遣幂等忽略。
 5. 警情升级：新增需求后，对该警情全部已派队员级联重检；不再满足资质者
    立即撤回并释放状态（可再派），同时重新核算人数缺口。
 6. 引用不存在的队员/装备/警情：报告错误；未知警情直接拒绝派遣，
    未知装备视为装备检查失败并阻止本次派遣。

退出码：无错误为 0，存在任何错误为 1（便于脚本化校验）。
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 数据模型

@dataclass
class Member:
    name: str
    quals: set


@dataclass
class Equipment:
    name: str
    weathers: set


@dataclass
class Incident:
    name: str
    location: str
    required_quals: set
    weather: str
    assigned: list = field(default_factory=list)  # 在岗队员名（有序）

    @property
    def headcount_needed(self) -> int:
        return len(self.required_quals)


# ---------------------------------------------------------------- 引擎

class DispatchEngine:
    def __init__(self):
        self.members = {}      # name -> Member
        self.equipment = {}    # name -> Equipment
        self.incidents = {}    # name -> Incident
        self.assignment = {}   # member name -> incident name（跨警情延续）
        self.errors = []

    # -- 错误记录 ------------------------------------------------------
    def error(self, etype, message, **details):
        entry = {"type": etype, "message": message}
        entry.update(details)
        self.errors.append(entry)

    # -- 定义类指令 ----------------------------------------------------
    def define_member(self, name, quals):
        self.members[name] = Member(name, set(quals))

    def define_equipment(self, name, weathers):
        self.equipment[name] = Equipment(name, set(weathers))

    def define_incident(self, name, location, quals, weather):
        if name in self.incidents:
            # 同名警情再次出现 = 升级：更新地点/天气，合并新增需求并级联重检
            inc = self.incidents[name]
            inc.location = location
            inc.weather = weather
            self.upgrade_incident(name, set(quals))
        else:
            self.incidents[name] = Incident(name, location, set(quals), weather)

    # -- 警情升级 ------------------------------------------------------
    def upgrade_incident(self, inc_name, new_quals):
        inc = self.incidents.get(inc_name)
        if inc is None:
            self.error("UNKNOWN_INCIDENT",
                       "升级失败：警情「%s」不存在" % inc_name,
                       incident=inc_name)
            return
        added = set(new_quals) - inc.required_quals
        inc.required_quals |= set(new_quals)
        if added:
            # 级联重检：已派队员不再满足新需求 -> 撤回并释放状态
            for mname in list(inc.assigned):
                mem = self.members[mname]
                missing = inc.required_quals - mem.quals
                if missing:
                    inc.assigned.remove(mname)
                    del self.assignment[mname]
                    self.error(
                        "UPGRADE_RECHECK_FAILED",
                        "警情「%s」升级新增需求（%s），已派队员「%s」缺资质（%s），"
                        "已撤回并释放为可派遣状态" % (
                            inc_name, "、".join(sorted(added)),
                            mname, "、".join(sorted(missing))),
                        incident=inc_name, member=mname,
                        added=sorted(added), missing=sorted(missing))
        self._check_headcount(inc)

    # -- 派遣 ----------------------------------------------------------
    def dispatch(self, inc_name, member_names, equip_names=None):
        inc = self.incidents.get(inc_name)
        if inc is None:
            self.error("UNKNOWN_INCIDENT",
                       "派遣失败：警情「%s」不存在" % inc_name,
                       incident=inc_name)
            return

        # 1) 天气/装备检查（未指定装备 = 全队装备出动）
        targets = list(equip_names) if equip_names else list(self.equipment)
        equip_ok = True
        for ename in targets:
            eq = self.equipment.get(ename)
            if eq is None:
                self.error("UNKNOWN_EQUIPMENT",
                           "警情「%s」派遣失败：装备「%s」不存在" % (inc_name, ename),
                           incident=inc_name, equipment=ename)
                equip_ok = False
                continue
            if inc.weather not in eq.weathers:
                self.error(
                    "WEATHER_NOT_ALLOWED",
                    "装备「%s」不适用天气「%s」（适用：%s），警情「%s」本次派遣被阻止" % (
                        ename, inc.weather, "、".join(sorted(eq.weathers)), inc_name),
                    incident=inc_name, equipment=ename, weather=inc.weather)
                equip_ok = False

        # 2) 队员检查（存在性 / 重复派遣 / 资质）
        accepted = []
        for mname in member_names:
            mem = self.members.get(mname)
            if mem is None:
                self.error("UNKNOWN_MEMBER",
                           "警情「%s」派遣失败：队员「%s」不存在" % (inc_name, mname),
                           incident=inc_name, member=mname)
                continue
            if mname in self.assignment:
                other = self.assignment[mname]
                if other == inc_name:
                    continue  # 同警情重复派遣：幂等忽略
                self.error(
                    "DUPLICATE_DISPATCH",
                    "队员「%s」已派遣至警情「%s」，不可再派往「%s」" % (mname, other, inc_name),
                    member=mname, assigned_to=other, incident=inc_name)
                continue
            missing = inc.required_quals - mem.quals
            if missing:
                self.error(
                    "MISSING_QUALIFICATION",
                    "队员「%s」不具备警情「%s」所需资质：缺 %s" % (
                        mname, inc_name, "、".join(sorted(missing))),
                    incident=inc_name, member=mname, missing=sorted(missing))
                continue
            accepted.append(mname)

        # 3) 状态级联更新：装备检查通过才真正派遣（装备不达标不出动）
        if equip_ok:
            for mname in accepted:
                self.assignment[mname] = inc_name
                inc.assigned.append(mname)

        # 4) 人数缺口核算
        self._check_headcount(inc)

    # -- 人数缺口 ------------------------------------------------------
    def _check_headcount(self, inc):
        need = inc.headcount_needed
        have = len(inc.assigned)
        if have < need:
            self.error(
                "INSUFFICIENT_HEADCOUNT",
                "警情「%s」需求人数 %d，在岗 %d，缺口 %d" % (inc.name, need, have, need - have),
                incident=inc.name, needed=need, available=have, gap=need - have)

    # -- 结果汇总 ------------------------------------------------------
    def report(self):
        incidents = []
        for inc in self.incidents.values():
            need = inc.headcount_needed
            have = len(inc.assigned)
            incidents.append({
                "name": inc.name,
                "location": inc.location,
                "weather": inc.weather,
                "required_quals": sorted(inc.required_quals),
                "headcount_needed": need,
                "assigned": [
                    {"name": m, "quals": sorted(self.members[m].quals)}
                    for m in inc.assigned
                ],
                "status": "齐备" if have >= need else "缺口 %d" % (need - have),
            })
        members = [{
            "name": m.name,
            "quals": sorted(m.quals),
            "status": ("派遣至「%s」" % self.assignment[m.name])
                      if m.name in self.assignment else "空闲",
        } for m in self.members.values()]
        return {"incidents": incidents, "members": members, "errors": self.errors}


# ---------------------------------------------------------------- 指令解析

ALIASES = {
    "member": "member", "队员": "member",
    "equipment": "equipment", "装备": "equipment",
    "incident": "incident", "警情": "incident",
    "upgrade": "upgrade", "升级": "upgrade",
    "dispatch": "dispatch", "派遣": "dispatch",
}


def split_list(text):
    """按英文/中文逗号拆分名单，去空白并丢弃空项。"""
    return [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]


def run_stream(engine, text, source="<stdin>"):
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as exc:
            engine.error("PARSE_ERROR", "%s:%d 解析失败：%s" % (source, lineno, exc))
            continue
        if not tokens:
            continue
        cmd = ALIASES.get(tokens[0])
        args = tokens[1:]
        where = "%s:%d" % (source, lineno)
        if cmd is None:
            engine.error("PARSE_ERROR", "%s 未知指令「%s」" % (where, tokens[0]))
        elif cmd == "member" and len(args) == 2:
            engine.define_member(args[0], split_list(args[1]))
        elif cmd == "equipment" and len(args) == 2:
            engine.define_equipment(args[0], split_list(args[1]))
        elif cmd == "incident" and len(args) == 4:
            engine.define_incident(args[0], args[1], split_list(args[2]), args[3])
        elif cmd == "upgrade" and len(args) == 2:
            engine.upgrade_incident(args[0], split_list(args[1]))
        elif cmd == "dispatch" and len(args) in (2, 3):
            equips = split_list(args[2]) if len(args) == 3 else None
            engine.dispatch(args[0], split_list(args[1]), equips)
        else:
            engine.error("PARSE_ERROR", "%s 指令「%s」参数个数不正确" % (where, tokens[0]))


# ---------------------------------------------------------------- 输出

def render_text(report):
    out = []
    out.append("======== 派遣结果 ========")
    if not report["incidents"]:
        out.append("（无警情）")
    for inc in report["incidents"]:
        out.append("警情「%s」 地点=%s 天气=%s 需求资质=%s 需求人数=%d 在岗=%d 状态=%s" % (
            inc["name"], inc["location"], inc["weather"],
            "、".join(inc["required_quals"]) or "（无）",
            inc["headcount_needed"], len(inc["assigned"]), inc["status"]))
        if inc["assigned"]:
            for m in inc["assigned"]:
                out.append("    - %s（%s）" % (m["name"], "、".join(m["quals"])))
        else:
            out.append("    - （无在岗队员）")
    out.append("")
    out.append("======== 队员状态 ========")
    if not report["members"]:
        out.append("（无队员）")
    for m in report["members"]:
        out.append("%s：%s（资质：%s）" % (m["name"], m["status"], "、".join(m["quals"])))
    out.append("")
    errors = report["errors"]
    out.append("======== 错误清单（%d 条）========" % len(errors))
    if not errors:
        out.append("（无错误）")
    for i, e in enumerate(errors, 1):
        out.append("%2d. [%s] %s" % (i, e["type"], e["message"]))
    return "\n".join(out)


# ---------------------------------------------------------------- 内置示例

DEMO = """\
# ==== 队员定义 ====
member 张三 急救,攀岩
member 李四 驾驶
member 王五 急救,驾驶
member 赵六 急救,攀岩,驾驶

# ==== 装备定义 ====
equipment 担架 晴,阴,雨,雪
equipment 卫星电话 晴,阴,雨,雪
equipment 无人机 晴

# ==== 警情定义 ====
incident 北坡坠崖 北坡 急救,攀岩 晴
incident 雪崩搜救 西沟 急救,驾驶 雪

# 1) 正常派遣：张三资质齐备，上岗北坡坠崖
dispatch 北坡坠崖 张三 担架,卫星电话

# 2) 资质不足：李四缺 急救、攀岩，被拒派
dispatch 北坡坠崖 李四

# 3) 张三已派遣不可再派（重复派遣）；王五上岗雪崩搜救；人数需2在岗1，报缺口
dispatch 雪崩搜救 张三,王五 卫星电话

# 4) 引用不存在：队员孙七 / 警情幽灵警情 / 装备幽灵装备
dispatch 雪崩搜救 孙七
dispatch 幽灵警情 赵六
dispatch 雪崩搜救 赵六 幽灵装备

# 5) 天气不允许：无人机仅适用晴，雪崩搜救为雪天，本次派遣被阻止
dispatch 雪崩搜救 赵六 无人机

# 6) 装备合规后补派成功，雪崩搜救人数齐备
dispatch 雪崩搜救 赵六 卫星电话

# 7) 警情升级：北坡坠崖新增 驾驶 需求 -> 张三缺驾驶，级联重检后撤回并释放
upgrade 北坡坠崖 驾驶

# 8) 张三已释放可再派，但雪崩搜救需 急救+驾驶，张三缺驾驶仍被拒
dispatch 雪崩搜救 张三 卫星电话
"""


# ---------------------------------------------------------------- 入口

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="登山救援派遣校验工具（纯标准库单文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="指令格式详见文件头部 docstring；可用 --demo 直接运行内置示例。")
    parser.add_argument("files", nargs="*", help="指令流输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例并输出结果")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args(argv)

    engine = DispatchEngine()
    if args.demo:
        run_stream(engine, DEMO, source="<demo>")
    elif args.files:
        for path in args.files:
            with open(path, encoding="utf-8") as fh:
                run_stream(engine, fh.read(), source=path)
    else:
        run_stream(engine, sys.stdin.read())

    report = engine.report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
