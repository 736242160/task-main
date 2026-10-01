#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收容中心动物领养联动工具（仅依赖 Python 标准库，单文件）。

用法：
    python3 shelter.py 输入文件      # 从文件读取指令流
    python3 shelter.py < 输入文件    # 从标准输入读取
    python3 shelter.py --demo        # 运行内置示例（可直接验证全部规则）

指令（每行一条，# 之后为注释，字段以空白分隔）：
    收容区 <名称> <容量>
    动物   <编号> <品种> <良好|待治疗|隔离> <已接种|未接种> <收容区名称>
    治疗   <动物编号> <治愈|无效>
    申请   <动物编号> <申请人> <高|中|低>
    撤销   <申请人> <动物编号>

领养条件（自定规则及理由）：
    1. 动物健康状况必须为 良好 —— 待治疗/隔离动物出院前进入家庭易导致病情反复或疾病传播。
    2. 动物必须 已接种 疫苗 —— 保障领养家庭与其他宠物的安全。
    3. 申请人意向等级须为 中 或 高 —— 低意向申请人弃养风险高，暂不受理。

裁决与级联策略：
    - 申请到达即审：合格立即批准（先到先得）；不合格则挂起并报告缺失条件。
    - 同一动物有多条挂起申请时，按意向等级（高>中>低）裁决，同级按申请先后。
    - 治疗“治愈”把 待治疗 级联更新为 良好，并自动重审该动物全部挂起申请。
    - 领养成功后动物标记 已领养、收容区释放一个床位，其余挂起申请关闭。
    - 全部指令按顺序执行，状态在指令间延续；结束时输出最终状态快照。
"""

import argparse
import sys
from dataclasses import dataclass, field

HEALTH_STATUS = ("良好", "待治疗", "隔离")
VACCINE_STATUS = ("已接种", "未接种")
INTENT_RANK = {"高": 3, "中": 2, "低": 1}
MIN_INTENT_RANK = INTENT_RANK["中"]


@dataclass
class Animal:
    aid: str
    breed: str
    health: str
    vaccine: str
    shelter: str
    adopted: bool = False


@dataclass
class Shelter:
    name: str
    capacity: int
    residents: list = field(default_factory=list)

    @property
    def free(self):
        return self.capacity - len(self.residents)


@dataclass
class Application:
    seq: int
    aid: str
    applicant: str
    intent: str
    status: str = "待审"  # 待审 / 成功 / 落选 / 撤销
    last_reported: tuple = ()


class Center:
    def __init__(self):
        self.animals = {}
        self.shelters = {}
        self.apps = []
        self.results = []   # 领养结果
        self.reports = []   # 错误与报告清单 (行号, 内容)
        self._seq = 0

    def report(self, line, msg):
        self.reports.append((line, msg))

    # ---------- 指令处理 ----------
    def cmd_shelter(self, line, name, capacity):
        if name in self.shelters:
            self.report(line, f"收容区 {name} 重复定义，忽略。")
            return
        try:
            cap = int(capacity)
        except ValueError:
            self.report(line, f"收容区 {name} 容量 {capacity!r} 不是整数，忽略。")
            return
        if cap <= 0:
            self.report(line, f"收容区 {name} 容量必须为正整数，忽略。")
            return
        self.shelters[name] = Shelter(name, cap)

    def cmd_animal(self, line, aid, breed, health, vaccine, shelter_name):
        if aid in self.animals:
            self.report(line, f"动物 {aid} 重复登记，忽略。")
            return
        if health not in HEALTH_STATUS:
            self.report(line, f"动物 {aid} 健康状况 {health!r} 非法（应为 {'/'.join(HEALTH_STATUS)}），忽略。")
            return
        if vaccine not in VACCINE_STATUS:
            self.report(line, f"动物 {aid} 疫苗情况 {vaccine!r} 非法（应为 {'/'.join(VACCINE_STATUS)}），忽略。")
            return
        shelter = self.shelters.get(shelter_name)
        if shelter is None:
            self.report(line, f"动物 {aid} 引用了不存在的收容区 {shelter_name}，拒绝登记。")
            return
        if shelter.free <= 0:
            self.report(line, f"收容区 {shelter_name} 已满（容量 {shelter.capacity}），动物 {aid} 拒收。")
            return
        shelter.residents.append(aid)
        self.animals[aid] = Animal(aid, breed, health, vaccine, shelter_name)

    def cmd_treat(self, line, aid, outcome):
        animal = self.animals.get(aid)
        if animal is None:
            self.report(line, f"治疗记录引用了不存在的动物 {aid}，忽略。")
            return
        if outcome == "治愈":
            if animal.health != "待治疗":
                self.report(line, f"动物 {aid} 当前状态为 {animal.health}，不接受“治愈”结果，忽略。")
                return
            animal.health = "良好"
            self.report(line, f"动物 {aid} 治愈，健康状况级联更新为 良好，触发挂起申请重审。")
            self.adjudicate(aid, line)
        elif outcome == "无效":
            if animal.adopted:
                self.report(line, f"动物 {aid} 已被领养，不再接受治疗记录，忽略。")
            elif animal.health != "待治疗":
                self.report(line, f"动物 {aid} 当前状态为 {animal.health}，治疗“无效”结果不适用，忽略。")
            else:
                self.report(line, f"动物 {aid} 治疗无效，状态保持 待治疗，不可领养；相关申请继续挂起等待。")
        else:
            self.report(line, f"治疗结果 {outcome!r} 非法（应为 治愈/无效），忽略。")

    def cmd_apply(self, line, aid, applicant, intent):
        animal = self.animals.get(aid)
        if animal is None:
            self.report(line, f"申请引用了不存在的动物 {aid}，拒绝受理（申请人 {applicant}）。")
            return
        if intent not in INTENT_RANK:
            self.report(line, f"申请意向等级 {intent!r} 非法（应为 高/中/低），拒绝受理。")
            return
        if animal.adopted:
            self.report(line, f"动物 {aid} 已被领养，申请人 {applicant} 的申请不予受理。")
            return
        for app in self.apps:
            if app.aid == aid and app.applicant == applicant and app.status != "撤销":
                self.report(line, f"申请人 {applicant} 重复申请同一动物 {aid}，拒绝受理。")
                return
        self._seq += 1
        self.apps.append(Application(self._seq, aid, applicant, intent))
        self.adjudicate(aid, line)

    def cmd_cancel(self, line, applicant, aid):
        for app in self.apps:
            if app.aid == aid and app.applicant == applicant and app.status == "待审":
                app.status = "撤销"
                self.report(line, f"申请人 {applicant} 撤销了对动物 {aid} 的申请#{app.seq}。")
                return
        self.report(line, f"申请人 {applicant} 对动物 {aid} 没有待审申请，引用了不存在的申请，忽略。")

    # ---------- 裁决 ----------
    def missing_conditions(self, animal, app):
        conds = []
        if animal.health == "隔离":
            conds.append("动物处于隔离状态，不可领养")
        elif animal.health == "待治疗":
            conds.append("动物尚待治疗，健康状况未达良好")
        if animal.vaccine != "已接种":
            conds.append("动物未完成疫苗接种")
        if INTENT_RANK[app.intent] < MIN_INTENT_RANK:
            conds.append("申请人意向等级不足（需 中 及以上）")
        return conds

    def adjudicate(self, aid, line):
        animal = self.animals[aid]
        if animal.adopted:
            return
        pending = [a for a in self.apps if a.aid == aid and a.status == "待审"]
        if not pending:
            return
        eligible = []
        for app in pending:
            conds = self.missing_conditions(animal, app)
            if conds:
                key = tuple(conds)
                if key != app.last_reported:  # 条件变化才重复报告，避免刷屏
                    app.last_reported = key
                    self.report(line, f"申请#{app.seq}（{app.applicant} -> {aid}）暂不满足领养条件：{'；'.join(conds)}。")
            else:
                eligible.append(app)
        if not eligible:
            return
        winner = max(eligible, key=lambda a: (INTENT_RANK[a.intent], -a.seq))
        winner.status = "成功"
        animal.adopted = True
        shelter = self.shelters[animal.shelter]
        shelter.residents.remove(aid)
        self.results.append(
            f"申请#{winner.seq} {winner.applicant} 成功领养动物 {aid}（{animal.breed}）；"
            f"收容区 {shelter.name} 释放 1 个床位，剩余空位 {shelter.free}。"
        )
        for app in pending:
            if app is winner or app.status != "待审":
                continue
            app.status = "落选"
            if app in eligible:
                why = f"仲裁落选：申请#{winner.seq}（{winner.applicant}，意向 {winner.intent}）优先级更高"
            else:
                why = "动物已被领养，申请关闭"
            self.report(line, f"申请#{app.seq}（{app.applicant} -> {aid}）{why}。")


HANDLERS = {
    "收容区": (Center.cmd_shelter, 2),
    "动物": (Center.cmd_animal, 5),
    "治疗": (Center.cmd_treat, 2),
    "申请": (Center.cmd_apply, 3),
    "撤销": (Center.cmd_cancel, 2),
}


def run(lines):
    center = Center()
    for lineno, raw in enumerate(lines, 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        parts = text.split()
        cmd, args = parts[0], parts[1:]
        entry = HANDLERS.get(cmd)
        if entry is None:
            center.report(lineno, f"未知指令 {cmd!r}，忽略。")
            continue
        handler, arity = entry
        if len(args) != arity:
            center.report(lineno, f"指令 {cmd} 需要 {arity} 个参数，实际 {len(args)} 个，忽略。")
            continue
        handler(center, lineno, *args)
    return center


def print_report(center):
    print("==== 领养结果 ====")
    if center.results:
        for i, r in enumerate(center.results, 1):
            print(f"{i}. {r}")
    else:
        print("（无）")
    print()
    print("==== 错误与报告清单 ====")
    if center.reports:
        for i, (line, msg) in enumerate(center.reports, 1):
            print(f"{i}. [行{line}] {msg}")
    else:
        print("（无）")
    print()
    print("==== 最终状态（状态延续快照）====")
    print("动物：")
    if center.animals:
        for a in center.animals.values():
            status = "已领养" if a.adopted else a.health
            loc = "已离区" if a.adopted else a.shelter
            print(f"  {a.aid:<6} {a.breed:<6} 状态:{status:<4} 疫苗:{a.vaccine} 收容区:{loc}")
    else:
        print("  （无）")
    print("收容区：")
    if center.shelters:
        for s in center.shelters.values():
            print(f"  {s.name:<6} 容量:{s.capacity} 在住:{len(s.residents)} 空位:{s.free} 在住动物:{s.residents}")
    else:
        print("  （无）")
    pending = [a for a in center.apps if a.status == "待审"]
    print("待审申请：")
    if pending:
        for a in pending:
            print(f"  申请#{a.seq} {a.applicant} -> {a.aid} 意向:{a.intent}")
    else:
        print("  （无）")


DEMO_SCRIPT = """\
# ---- 内置示例：覆盖全部规则 ----
收容区 东区 3
收容区 西区 1
动物 A001 金毛 良好 已接种 东区
动物 A002 英短 待治疗 已接种 东区
动物 A005 哈士奇 良好 未接种 东区
动物 A003 田园犬 隔离 未接种 西区
动物 A004 布偶 良好 已接种 西区        # 西区已满 -> 拒收
动物 A001 金毛 良好 已接种 东区        # 重复登记 -> 报告
动物 A006 柯基 良好 已接种 南区        # 不存在的收容区 -> 报告
申请 A001 张三 中                      # 合格 -> 立即成功，东区释放床位
申请 A001 李四 高                      # A001 已领养 -> 报告
申请 A002 王五 中                      # 待治疗 -> 挂起并报告缺条件
申请 A002 赵六 高                      # 同上，挂起
申请 A002 王五 高                      # 重复申请同一动物 -> 报告
申请 A003 孙七 高                      # 隔离+未接种 -> 报告两条缺条件
申请 A009 张三 高                      # 不存在的动物 -> 报告
申请 A005 周八 低                      # 未接种+意向不足 -> 报告两条缺条件
动物 A007 博美 待治疗 已接种 东区       # 张三领养 A001 后东区腾出床位 -> 成功入住
申请 A007 钱十 高                      # 待治疗 -> 挂起
治疗 A007 无效                         # 治疗无效 -> 保持待治疗，不可领养
治疗 A002 治愈                         # 级联：A002 变良好，重审 -> 赵六(高) 胜，王五落选
治疗 A002 无效                         # A002 已被领养 -> 不再接受治疗记录
治疗 A009 治愈                         # 不存在的动物 -> 报告
治疗 A003 治愈                         # 隔离状态不接受治愈 -> 报告
撤销 周八 A005                         # 撤销成功
撤销 周八 A005                         # 再次撤销 -> 引用不存在的申请
申请 A005 吴九 高                      # 未接种 -> 挂起（仅一条缺条件）
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="收容中心动物领养联动工具（标准库单文件）",
        epilog="指令格式与规则见文件头部文档字符串。",
    )
    parser.add_argument("input", nargs="?", help="指令输入文件（缺省读标准输入）")
    parser.add_argument("--demo", action="store_true", help="运行内置示例并输出结果")
    args = parser.parse_args(argv)

    if args.demo:
        print("---- 示例输入 ----")
        print(DEMO_SCRIPT)
        print("---- 运行输出 ----")
        center = run(DEMO_SCRIPT.splitlines())
    elif args.input:
        with open(args.input, encoding="utf-8") as f:
            center = run(f.read().splitlines())
    else:
        center = run(sys.stdin.read().splitlines())
    print_report(center)
    return 0


if __name__ == "__main__":
    sys.exit(main())
