#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消防队出动调度工具（纯 Python 标准库，单文件）

用法：
    python3 fire_dispatch.py 输入文件        # 从文件读取
    python3 fire_dispatch.py < 输入文件      # 从标准输入读取
    python3 fire_dispatch.py --demo          # 运行内置示例（覆盖全部错误类型）

输入格式（按行解析，# 之后为注释，空白行忽略；定义须先于引用出现）：
    站   <站名> <车辆编号...>                 定义消防站及其车辆
    车   <编号> <类型> <状态>                 类型: 水罐|云梯|泡沫  状态: 可用|检修
    警情 <名称> <类型> <所需车辆类型...>       类型: 火灾|救援|危化；类型可重复表示数量
    出动 <警情名> <站名> <车辆编号...>         发起一次出动
    升级 <警情名> <新类型> <新所需车辆类型...> 警情升级，级联重检已出动车辆
    归队 <车辆编号...>                        车辆归队，状态恢复为可用

自定规则及理由：
    1. 警情类型 -> 允许出动的车辆类型（MATCH_RULES）：
         火灾 -> 水罐、泡沫      （灭火主力）
         救援 -> 云梯、水罐      （登高救人 + 供水）
         危化 -> 泡沫            （危化品处置只派泡沫车，避免扩大风险）
    2. 车辆争抢：先报先得。出动流按顺序处理，车辆一旦出动即被锁定，
       后续警情再派该车记为"车辆争抢"错误。理由：事件流天然有序，
       先到的警情先占用资源，实现简单且无歧义。
    3. 状态级联：出动成功后车辆状态变为"出动中"，后续事件（含跨出动）
       均看到最新状态；归队后恢复"可用"。
    4. 警情升级：立即用新类型/新需求级联重检该警情全部已出动车辆，
       不再匹配的车辆与新增缺口都会报告。
"""
import sys
from collections import Counter
from dataclasses import dataclass, field

VEHICLE_TYPES = ("水罐", "云梯", "泡沫")
INCIDENT_TYPES = ("火灾", "救援", "危化")

MATCH_RULES = {
    "火灾": {"水罐", "泡沫"},
    "救援": {"云梯", "水罐"},
    "危化": {"泡沫"},
}


@dataclass
class Vehicle:
    vid: str
    vtype: str
    status: str          # 可用 / 检修 / 出动中
    station: str = ""
    incident: str = ""   # 当前出动服务的警情


@dataclass
class Station:
    name: str
    vehicles: list


@dataclass
class Incident:
    name: str
    itype: str
    required: list
    dispatched: list = field(default_factory=list)


class Engine:
    def __init__(self):
        self.stations = {}
        self.vehicles = {}
        self.incidents = {}
        self.errors = []    # (行号, 类别, 消息)
        self.results = []   # (行号, 结果描述)

    def err(self, lineno, category, msg):
        self.errors.append((lineno, category, msg))

    def shortage(self, inc):
        req = Counter(inc.required)
        have = Counter(self.vehicles[v].vtype for v in inc.dispatched)
        return [(t, req[t] - have[t]) for t in req if have[t] < req[t]]

    # ---------- 定义类命令 ----------

    def do_station(self, lineno, args):
        if len(args) < 1:
            raise ValueError("站 命令格式：站 <站名> <车辆编号...>")
        name = args[0]
        self.stations[name] = Station(name, list(args[1:]))
        for vid in args[1:]:
            if vid in self.vehicles:
                self.vehicles[vid].station = name

    def do_vehicle(self, lineno, args):
        if len(args) != 3:
            raise ValueError("车 命令格式：车 <编号> <类型> <状态>")
        vid, vtype, status = args
        if vtype not in VEHICLE_TYPES:
            raise ValueError(f"未知车辆类型「{vtype}」，应为 {'/'.join(VEHICLE_TYPES)}")
        if status not in ("可用", "检修"):
            raise ValueError(f"未知车辆状态「{status}」，应为 可用/检修")
        v = Vehicle(vid, vtype, status)
        for s in self.stations.values():
            if vid in s.vehicles:
                v.station = s.name
        self.vehicles[vid] = v

    def do_incident(self, lineno, args):
        if len(args) < 2:
            raise ValueError("警情 命令格式：警情 <名称> <类型> <所需车辆类型...>")
        name, itype, required = args[0], args[1], args[2:]
        if itype not in INCIDENT_TYPES:
            raise ValueError(f"未知警情类型「{itype}」，应为 {'/'.join(INCIDENT_TYPES)}")
        bad = [t for t in required if t not in VEHICLE_TYPES]
        if bad:
            raise ValueError(f"所需车辆类型非法：{'、'.join(bad)}")
        self.incidents[name] = Incident(name, itype, list(required))

    # ---------- 事件类命令 ----------

    def do_dispatch(self, lineno, args):
        if len(args) < 2:
            raise ValueError("出动 命令格式：出动 <警情名> <站名> <车辆编号...>")
        inc_name, st_name, vids = args[0], args[1], args[2:]
        inc = self.incidents.get(inc_name)
        st = self.stations.get(st_name)
        if inc is None:
            self.err(lineno, "引用错误", f"警情「{inc_name}」不存在")
        if st is None:
            self.err(lineno, "引用错误", f"消防站「{st_name}」不存在")
        if inc is None or st is None:
            self.results.append((lineno, f"出动 {inc_name} @ {st_name}：中止（引用不存在）"))
            return

        allowed = MATCH_RULES[inc.itype]
        accepted, rejected = [], []
        seen = set()
        for vid in vids:
            if vid in seen:
                rejected.append(f"{vid}(重复)")
                self.err(lineno, "重复出动", f"车辆「{vid}」在同一出动单中重复出现")
                continue
            seen.add(vid)
            v = self.vehicles.get(vid)
            if v is None:
                rejected.append(f"{vid}(不存在)")
                self.err(lineno, "引用错误", f"车辆「{vid}」不存在")
                continue
            if vid not in st.vehicles:
                rejected.append(f"{vid}(非本站)")
                self.err(lineno, "归属错误",
                         f"车辆「{vid}」不属于消防站「{st_name}」（属于「{v.station or '未分配'}」）")
                continue
            if v.status == "检修":
                rejected.append(f"{vid}(检修)")
                self.err(lineno, "车辆检修", f"车辆「{vid}」({v.vtype}) 正在检修，不得出动")
                continue
            if v.status == "出动中":
                if v.incident == inc_name:
                    rejected.append(f"{vid}(重复出动)")
                    self.err(lineno, "重复出动",
                             f"车辆「{vid}」已出动至警情「{inc_name}」，不得重复出动")
                else:
                    rejected.append(f"{vid}(被争抢)")
                    self.err(lineno, "车辆争抢",
                             f"车辆「{vid}」已出动至警情「{v.incident}」，警情「{inc_name}」"
                             f"争抢失败（规则：先报先得）")
                continue
            if v.vtype not in allowed:
                rejected.append(f"{vid}(类型不匹配)")
                self.err(lineno, "类型不匹配",
                         f"警情「{inc_name}」({inc.itype}) 不允许使用{v.vtype}车，"
                         f"车辆「{vid}」被拒（允许：{'、'.join(sorted(allowed))}）")
                continue
            accepted.append(vid)

        for vid in accepted:  # 状态级联更新：可用 -> 出动中
            v = self.vehicles[vid]
            v.status = "出动中"
            v.incident = inc_name
            inc.dispatched.append(vid)

        for t, n in self.shortage(inc):
            self.err(lineno, "需求不足", f"警情「{inc_name}」缺 {t}车 × {n}")

        desc = f"出动 {inc_name} @ {st_name}："
        desc += ("派出 " + " ".join(f"{v}({self.vehicles[v].vtype})" for v in accepted)) if accepted else "派出 无"
        if rejected:
            desc += "；被拒 " + " ".join(rejected)
        self.results.append((lineno, desc))

    def do_upgrade(self, lineno, args):
        if len(args) < 2:
            raise ValueError("升级 命令格式：升级 <警情名> <新类型> <新所需车辆类型...>")
        inc_name, new_type, new_required = args[0], args[1], args[2:]
        inc = self.incidents.get(inc_name)
        if inc is None:
            self.err(lineno, "引用错误", f"警情「{inc_name}」不存在，无法升级")
            return
        if new_type not in INCIDENT_TYPES:
            raise ValueError(f"未知警情类型「{new_type}」，应为 {'/'.join(INCIDENT_TYPES)}")
        bad = [t for t in new_required if t not in VEHICLE_TYPES]
        if bad:
            raise ValueError(f"所需车辆类型非法：{'、'.join(bad)}")

        old = f"{inc.itype} 需求[{' '.join(inc.required)}]"
        inc.itype = new_type
        inc.required = list(new_required)
        allowed = MATCH_RULES[new_type]

        for vid in inc.dispatched:  # 级联重检已出动车辆
            v = self.vehicles[vid]
            if v.vtype not in allowed:
                self.err(lineno, "类型不匹配",
                         f"警情「{inc_name}」升级为{new_type}后，已出动的{v.vtype}车「{vid}」"
                         f"不再匹配，需召回或更换")
        for t, n in self.shortage(inc):
            self.err(lineno, "需求不足", f"警情「{inc_name}」升级后缺 {t}车 × {n}")
        self.results.append((lineno, f"升级 {inc_name}：{old} -> {new_type} 需求[{' '.join(new_required)}]，"
                                     f"已出动 {len(inc.dispatched)} 辆完成级联重检"))

    def do_return(self, lineno, args):
        if not args:
            raise ValueError("归队 命令格式：归队 <车辆编号...>")
        back = []
        for vid in args:
            v = self.vehicles.get(vid)
            if v is None:
                self.err(lineno, "引用错误", f"车辆「{vid}」不存在")
                continue
            if v.status != "出动中":
                self.err(lineno, "状态错误", f"车辆「{vid}」当前状态为「{v.status}」，无需归队")
                continue
            inc = self.incidents.get(v.incident)
            if inc and vid in inc.dispatched:
                inc.dispatched.remove(vid)
            v.status = "可用"
            v.incident = ""
            back.append(vid)
            if inc:
                for t, n in self.shortage(inc):
                    self.err(lineno, "需求不足",
                             f"车辆「{vid}」归队后，警情「{inc.name}」缺 {t}车 × {n}")
        if back:
            self.results.append((lineno, f"归队：{' '.join(back)} 状态恢复为可用"))

    # ---------- 解析与输出 ----------

    def run(self, lines):
        handlers = {
            "站": self.do_station, "车": self.do_vehicle, "警情": self.do_incident,
            "出动": self.do_dispatch, "升级": self.do_upgrade, "归队": self.do_return,
        }
        for lineno, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0], parts[1:]
            handler = handlers.get(cmd)
            if handler is None:
                self.err(lineno, "格式错误", f"未知命令「{cmd}」，应为 {'/'.join(handlers)}")
                continue
            try:
                handler(lineno, args)
            except ValueError as e:
                self.err(lineno, "格式错误", str(e))

    def render(self):
        out = ["========== 出动结果 =========="]
        out += [f"[行{ln}] {desc}" for ln, desc in self.results] or ["（无事件）"]
        out.append("")
        out.append("========== 错误报告 ==========")
        if self.errors:
            out += [f"{i:2d}. [行{ln}][{cat}] {msg}"
                    for i, (ln, cat, msg) in enumerate(self.errors, 1)]
            out.append(f"共 {len(self.errors)} 条错误")
        else:
            out.append("（无错误）")
        out.append("")
        out.append("========== 最终状态 ==========")
        out.append("-- 车辆 --")
        for vid in sorted(self.vehicles):
            v = self.vehicles[vid]
            extra = f"（警情「{v.incident}」）" if v.status == "出动中" else ""
            out.append(f"  {vid}  {v.vtype}  {v.status}{extra}  所属站：{v.station or '未分配'}")
        out.append("-- 警情 --")
        for name, inc in self.incidents.items():
            gap = self.shortage(inc)
            gap_s = "、".join(f"{t}×{n}" for t, n in gap) if gap else "无缺口"
            out.append(f"  {name}  {inc.itype}  需求[{' '.join(inc.required)}]  "
                       f"已出动[{' '.join(inc.dispatched) or '无'}]  缺口：{gap_s}")
        return "\n".join(out)


DEMO = """\
# ===== 消防站与车辆定义 =====
站 城东站 车01 车02 车03 车04
站 城西站 车05 车06
车 车01 水罐 可用
车 车02 水罐 可用
车 车03 云梯 可用
车 车04 泡沫 检修
车 车05 泡沫 可用
车 车06 水罐 可用

# ===== 警情流 =====
警情 火警一号 火灾 水罐 水罐 泡沫
警情 救援二号 救援 云梯
警情 危化三号 危化 泡沫

# ===== 出动流 =====
出动 火警一号 城东站 车01 车02 车04   # 车04 检修被拒；缺泡沫×1
出动 火警一号 城东站 车01 车01        # 重复出动（已出动 + 同单重复）
出动 火警一号 城东站 车03             # 云梯不匹配火灾
出动 火警九号 城东站 车01             # 警情不存在
出动 火警一号 东湖站 车01             # 站不存在
出动 火警一号 城东站 车99             # 车辆不存在
出动 火警一号 城东站 车06             # 车06 不属于城东站
出动 危化三号 城西站 车05             # 成功
出动 救援二号 城西站 车05 车06        # 车05 被危化三号占用（争抢）；车06 成功；缺云梯
出动 救援二号 城东站 车03             # 云梯补齐，跨出动状态延续
升级 火警一号 危化 泡沫 泡沫          # 级联重检：车01/车02 水罐不再匹配；缺泡沫×2
归队 车01                             # 状态级联：车01 恢复可用
出动 危化三号 城东站 车01             # 水罐不匹配危化
"""


def main(argv):
    if "--demo" in argv:
        text = DEMO
        print("========== 示例输入 ==========")
        print(text)
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    engine = Engine()
    engine.run(text.splitlines())
    print(engine.render())
    return 1 if engine.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
