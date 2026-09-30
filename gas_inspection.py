#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""燃气入户安检管理工具（纯 Python 标准库，单文件）。

用法:
    python3 gas_inspection.py 输入文件          # 从文件读取
    cat 输入文件 | python3 gas_inspection.py    # 从标准输入读取
    python3 gas_inspection.py --demo            # 运行内置示例

输入格式（行式，# 开头为注释，空行忽略）:
    户号     <户号> <名称> <地址> <上次安检日期YYYY-MM-DD>
    隐患类型 <名称> <等级:严重|一般>
    安检     <户号> <日期> <隐患1,隐患2,...>     # 无隐患写 -
    整改     <户号> <隐患> <结果:完成|延期> [日期]  # 日期可省，默认当前日期

自定业务规则（理由）:
  1. 整改/复查期限: 严重隐患 7 天、一般隐患 30 天。
     理由: 严重隐患(如燃气泄漏)直接威胁人身安全，应在一周内整改复查；
     一般隐患(如软管老化)风险可控，给一个月整改周期符合行业惯例。
  2. 整改延期: 期限顺延一个同级周期(严重+7天/一般+30天)，只允许延期一次，
     再次延期报错。理由: 防止无限拖延，严重隐患尤其不能反复挂起。
  3. 停供级联: 严重隐患到期未销号 -> 该户立即停供；停供后该户后续安检
     仍记录隐患，但状态保持"已停供"，直到全部严重隐患整改完成销号后才
     恢复(恢复后若仍有一般隐患未销号则为"待整改")。理由: 停供是安全
     强制措施，不能因为又做了一次安检就自动解除，必须以实际整改为准。
  4. 重复安检: 同一户同一隐患在未销号前再次出现在安检流中 -> 报错并忽略，
     保留首次发现日期与期限。理由: 隐患整改期限应从首次发现起算。
"""

import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

SEVERE = "严重"
GENERAL = "一般"

SEVERE_DEADLINE_DAYS = 7    # 严重隐患整改/复查期限
GENERAL_DEADLINE_DAYS = 30  # 一般隐患整改/复查期限

STATUS_OK = "正常"
STATUS_PENDING = "待整改"
STATUS_STOPPED = "已停供"


def parse_date(text):
    return date.fromisoformat(text.replace("/", "-"))


@dataclass
class OpenHazard:
    name: str
    level: str
    found_date: date
    deadline: date
    deferred: bool = False   # 是否已延期(到期复查安排)
    overdue: bool = False    # 是否已逾期未整改


@dataclass
class Household:
    hid: str
    name: str
    address: str
    last_check: date
    stopped: bool = False
    open_hazards: dict = field(default_factory=dict)   # 隐患名 -> OpenHazard
    closed_hazards: list = field(default_factory=list) # (隐患名, 销号日期)


class GasInspectionSystem:
    def __init__(self):
        self.households = {}       # 户号 -> Household
        self.hazard_types = {}     # 隐患名 -> 等级
        self.errors = []           # 错误清单
        self.events = []           # 事件报告(停供/恢复/复查安排)
        self.current_date = None   # 当前处理到的日期(跨流状态延续的时间轴)

    # ---------- 定义 ----------
    def add_household(self, hid, name, address, last_check):
        if hid in self.households:
            self.errors.append("重复定义户号 %s，已忽略后者" % hid)
            return
        self.households[hid] = Household(hid, name, address, last_check)

    def add_hazard_type(self, name, level):
        if level not in (SEVERE, GENERAL):
            self.errors.append("隐患类型 %s 等级非法: %s(应为 严重/一般)" % (name, level))
            return
        if name in self.hazard_types:
            self.errors.append("重复定义隐患类型 %s，已忽略后者" % name)
            return
        self.hazard_types[name] = level

    # ---------- 内部工具 ----------
    def _deadline_for(self, level, base):
        days = SEVERE_DEADLINE_DAYS if level == SEVERE else GENERAL_DEADLINE_DAYS
        return base + timedelta(days=days)

    def _advance_time(self, new_date):
        """推进时间轴，并结算到期未整改的严重隐患(停供级联)。"""
        if self.current_date is not None and new_date < self.current_date:
            self.errors.append(
                "日期 %s 早于当前处理日期 %s，事件乱序" % (new_date, self.current_date))
        self.current_date = new_date
        for h in self.households.values():
            for oh in h.open_hazards.values():
                if not oh.overdue and oh.deadline < new_date:
                    oh.overdue = True
                    if oh.level == SEVERE and not h.stopped:
                        h.stopped = True
                        self.events.append(
                            "%s 户 %s(%s) 严重隐患「%s」到期(%s)未整改，执行停供"
                            % (new_date, h.hid, h.name, oh.name, oh.deadline))

    def _status_of(self, h):
        if h.stopped:
            return STATUS_STOPPED
        return STATUS_PENDING if h.open_hazards else STATUS_OK

    # ---------- 事件: 安检 ----------
    def inspect(self, hid, check_date, hazard_names):
        if hid not in self.households:
            self.errors.append("安检引用了不存在的户号 %s(日期 %s)，已忽略"
                               % (hid, check_date))
            return
        self._advance_time(check_date)
        h = self.households[hid]
        h.last_check = check_date
        if h.stopped:
            self.events.append(
                "%s 户 %s(%s) 处于停供状态，本次安检结果已记录但不解除停供"
                % (check_date, h.hid, h.name))
        for hz in hazard_names:
            if hz not in self.hazard_types:
                self.errors.append("安检户 %s 引用了未定义的隐患类型「%s」，已忽略"
                                   % (hid, hz))
                continue
            if hz in h.open_hazards:
                self.errors.append(
                    "重复安检: 户 %s 的隐患「%s」尚未销号(首次发现 %s)，重复记录已忽略"
                    % (hid, hz, h.open_hazards[hz].found_date))
                continue
            level = self.hazard_types[hz]
            h.open_hazards[hz] = OpenHazard(
                name=hz, level=level, found_date=check_date,
                deadline=self._deadline_for(level, check_date))

    # ---------- 事件: 整改 ----------
    def rectify(self, hid, hazard, result, rect_date=None):
        if hid not in self.households:
            self.errors.append("整改引用了不存在的户号 %s(隐患「%s」)，已忽略"
                               % (hid, hazard))
            return
        h = self.households[hid]
        if rect_date is None:
            rect_date = self.current_date or h.last_check
        self._advance_time(rect_date)
        if hazard not in h.open_hazards:
            self.errors.append("整改户 %s 的隐患「%s」不存在或未在安检中记录，已忽略"
                               % (hid, hazard))
            return
        oh = h.open_hazards[hazard]
        if result == "完成":
            del h.open_hazards[hazard]
            h.closed_hazards.append((hazard, rect_date))
            if h.stopped and not any(o.level == SEVERE for o in h.open_hazards.values()):
                h.stopped = False
                self.events.append(
                    "%s 户 %s(%s) 严重隐患全部销号，恢复供气(状态: %s)"
                    % (rect_date, h.hid, h.name, self._status_of(h)))
        elif result == "延期":
            if oh.deferred:
                self.errors.append(
                    "户 %s 隐患「%s」已延期过一次，不允许再次延期，已忽略" % (hid, hazard))
                return
            oh.deferred = True
            oh.overdue = False
            oh.deadline = self._deadline_for(oh.level, oh.deadline)
            self.events.append(
                "%s 户 %s(%s) 隐患「%s」(%s)整改延期，到期复查期限调整为 %s"
                % (rect_date, h.hid, h.name, hazard, oh.level, oh.deadline))
        else:
            self.errors.append("整改结果非法: %s(应为 完成/延期)" % result)

    # ---------- 收尾 ----------
    def finalize(self):
        """以最后事件日期为基准做最终到期结算，保证跨流状态延续到输出。"""
        if self.current_date is not None:
            self._advance_time(self.current_date)

    # ---------- 输出 ----------
    def report(self):
        self.finalize()
        out = []
        out.append("=" * 60)
        out.append("一、安检状态(基准日期: %s)" % (self.current_date or "无事件"))
        out.append("=" * 60)
        for hid in sorted(self.households):
            h = self.households[hid]
            out.append("户号 %s | %s | %s" % (h.hid, h.name, h.address))
            out.append("  上次安检: %s   状态: %s" % (h.last_check, self._status_of(h)))
            if h.open_hazards:
                out.append("  未销号隐患:")
                for oh in h.open_hazards.values():
                    flags = []
                    if oh.deferred:
                        flags.append("已延期")
                    if oh.overdue:
                        flags.append("已逾期")
                    flag = ("[%s]" % ",".join(flags)) if flags else ""
                    out.append("    - %s(%s) 发现:%s 整改/复查期限:%s %s"
                               % (oh.name, oh.level, oh.found_date, oh.deadline, flag))
            if h.closed_hazards:
                out.append("  已销号隐患:")
                for name, d in h.closed_hazards:
                    out.append("    - %s 销号日期:%s" % (name, d))
            out.append("")
        out.append("=" * 60)
        out.append("二、事件报告(停供级联/恢复/到期复查安排)")
        out.append("=" * 60)
        out.extend(self.events if self.events else ["  (无)"])
        out.append("")
        out.append("=" * 60)
        out.append("三、错误清单")
        out.append("=" * 60)
        out.extend(self.errors if self.errors else ["  (无)"])
        return "\n".join(out)


def load(text):
    """解析输入文本，返回处理完毕的系统对象。"""
    system = GasInspectionSystem()
    events = []  # 先收集定义，再按文件顺序执行事件流
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind = parts[0]
        try:
            if kind == "户号":
                _, hid, name, addr, d = parts
                system.add_household(hid, name, addr, parse_date(d))
            elif kind == "隐患类型":
                _, name, level = parts
                system.add_hazard_type(name, level)
            elif kind == "安检":
                _, hid, d, hz = parts
                hazards = [] if hz == "-" else [x for x in hz.split(",") if x]
                events.append(("安检", hid, parse_date(d), hazards))
            elif kind == "整改":
                if len(parts) == 4:
                    _, hid, hz, result = parts
                    d = None
                elif len(parts) == 5:
                    _, hid, hz, result, d = parts
                    d = parse_date(d)
                else:
                    raise ValueError("整改行字段数应为 4 或 5")
                events.append(("整改", hid, hz, result, d))
            else:
                system.errors.append("第 %d 行: 未知指令「%s」" % (lineno, kind))
        except (ValueError, IndexError) as exc:
            system.errors.append("第 %d 行解析失败: %s (%s)" % (lineno, raw.strip(), exc))
    # 事件按日期排序执行(跨流状态延续): 同日安检先于整改;
    # 未指定日期的整改排在所有带日期事件之后，按文件顺序执行。
    far_future = date.max
    def sort_key(ev):
        if ev[0] == "安检":
            return (ev[2], 0)
        return (ev[4] or far_future, 1)
    for ev in sorted(enumerate(events), key=lambda p: (sort_key(p[1]), p[0])):
        ev = ev[1]
        if ev[0] == "安检":
            _, hid, d, hazards = ev
            system.inspect(hid, d, hazards)
        else:
            _, hid, hz, result, d = ev
            system.rectify(hid, hz, result, d)
    return system


DEMO_INPUT = """\
# ===== 户号定义: 户号 名称 地址 上次安检日期 =====
户号 H001 张三 幸福路1号 2025-01-10
户号 H002 李四 幸福路2号 2025-01-10
户号 H003 王五 幸福路3号 2025-01-10
户号 H004 赵六 幸福路4号 2025-01-10

# ===== 隐患类型: 名称 等级 =====
隐患类型 燃气泄漏 严重
隐患类型 软管老化 一般
隐患类型 无熄火保护 一般

# ===== 安检流 =====
安检 H001 2025-02-01 燃气泄漏,软管老化
安检 H002 2025-02-01 软管老化
安检 H001 2025-02-03 燃气泄漏          # 重复安检同户同隐患 -> 报错
安检 H999 2025-02-03 软管老化          # 不存在的户号 -> 报错
安检 H003 2025-02-20 -                 # 无隐患
安检 H004 2025-02-01 燃气泄漏          # 严重隐患一直不整改 -> 停供

# ===== 整改流 =====
整改 H001 燃气泄漏 延期 2025-02-05     # 严重隐患延期 -> 复查期限 2025-02-15
整改 H002 软管老化 完成 2025-02-10     # 一般隐患按期销号
整改 H001 燃气泄漏 完成 2025-02-18     # 超过复查期限 -> 期间触发停供级联
整改 H999 软管老化 完成 2025-02-12     # 不存在的户号 -> 报错
整改 H002 燃气泄漏 完成 2025-02-12     # 隐患不存在 -> 报错

# ===== 停供后的后续安检(级联验证: 停供不因新安检自动解除) =====
安检 H001 2025-02-15 无熄火保护
安检 H004 2025-02-20 软管老化          # 停供中安检: 记录隐患但保持停供
"""


def main(argv):
    if "--demo" in argv:
        text = DEMO_INPUT
        print("【输入】")
        print(text)
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    system = load(text)
    print("【输出】")
    print(system.report())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
