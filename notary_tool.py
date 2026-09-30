#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notary_tool.py — 公证申请材料状态联动工具（纯 Python 标准库，单文件）

功能：
  读入公证事项定义、申请流、补正流（JSON），按事件顺序处理并级联更新，
  输出各申请状态（含状态轨迹）、申请人材料池状态与错误清单。

输入格式（JSON 文件或标准输入）：
{
  "matters": [
    {"name": "继承公证", "materials": ["身份证", "户口簿", "死亡证明"]}
  ],
  "applications": [
    {"id": "A1", "matter": "继承公证", "applicant": "张三",
     "date": "2026-01-05", "materials": ["身份证", "户口簿"]}
  ],
  "corrections": [
    {"application": "A1", "date": "2026-01-20", "materials": ["死亡证明"]}
  ]
}
说明：
  - date 为 YYYY-MM-DD，可省略（默认当天）。
  - 三个流按 matters -> applications -> corrections 顺序处理，状态跨流延续。

补正期限（自定）：自申请提交之日起 30 日。
  理由：参考《公证程序规则》关于补正材料的通行做法，30 日既给申请人
  充分的材料准备时间，又避免案件长期悬置、占用公证机构办案资源。
  如需调整，改 CORRECTION_DEADLINE_DAYS 即可。

申请状态机：待审查 -> 补正中 -> 已出证 / 终止
  - 出证条件：材料齐全（按申请人材料池判定，同人多申请共享）且未终止。
  - 出证后该申请人材料池整体置为“已出证”，并级联重检其名下其他申请。

用法：
  python3 notary_tool.py 输入.json     # 从文件读
  python3 notary_tool.py < 输入.json   # 从标准输入读
  python3 notary_tool.py --demo        # 运行内置示例
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

CORRECTION_DEADLINE_DAYS = 30  # 补正期限（天），理由见模块 docstring

ST_PENDING = "待审查"
ST_CORRECTING = "补正中"
ST_ISSUED = "已出证"
ST_TERMINATED = "终止"


@dataclass
class Application:
    app_id: str
    matter: str
    applicant: str
    submit_date: date
    state: str = ST_PENDING
    history: list = field(default_factory=lambda: [ST_PENDING])
    own_materials: set = field(default_factory=set)  # 本申请名下提交过的材料


class NotaryOffice:
    def __init__(self, deadline_days=CORRECTION_DEADLINE_DAYS):
        self.deadline_days = deadline_days
        self.matters = {}          # 事项名 -> [材料名, ...]（保序去重）
        self.apps = {}             # 申请编号 -> Application
        self.pools = {}            # 申请人 -> {材料名: "已提交"/"已出证"}
        self.errors = []           # {"type": ..., "message": ...}

    # ---------- 基础工具 ----------

    def error(self, category, message):
        self.errors.append({"type": category, "message": message})

    def pool_of(self, applicant):
        return self.pools.setdefault(applicant, {})

    def set_state(self, app, new_state):
        if app.state != new_state:
            app.state = new_state
            app.history.append(new_state)

    def missing_of(self, app):
        """缺项 = 事项要求材料 - 申请人共享材料池。"""
        pool = self.pool_of(app.applicant)
        return [m for m in self.matters[app.matter] if m not in pool]

    def report_missing(self, app):
        missing = self.missing_of(app)
        if missing:
            self.error(
                "缺材料",
                "申请%s（事项：%s，申请人：%s）缺材料：%s"
                % (app.app_id, app.matter, app.applicant, "、".join(missing)),
            )
        return missing

    # ---------- 事项定义流 ----------

    def define_matter(self, name, materials):
        if not name:
            self.error("事项定义错误", "存在未命名的事项定义，已忽略")
            return
        if name in self.matters:
            self.error("事项重复定义", "事项「%s」重复定义，以新定义覆盖" % name)
        self.matters[name] = list(dict.fromkeys(materials or []))

    # ---------- 材料接收与级联 ----------

    def receive_materials(self, app, materials, source):
        pool = self.pool_of(app.applicant)
        for m in materials or []:
            if m in pool:
                self.error(
                    "重复提交",
                    "%s：申请人%s的材料「%s」此前已提交，本次忽略（申请%s）"
                    % (source, app.applicant, m, app.app_id),
                )
                continue
            pool[m] = "已提交"
            app.own_materials.add(m)

    def issue(self, app):
        """出证：材料齐全且未终止。出证后材料池升级并级联重检同人其他申请。"""
        self.set_state(app, ST_ISSUED)
        pool = self.pool_of(app.applicant)
        for m in pool:
            pool[m] = "已出证"

    def evaluate(self, app):
        """重检单个申请：缺项->补正中并报告；齐全->出证。"""
        if app.state in (ST_ISSUED, ST_TERMINATED):
            return
        if self.missing_of(app):
            self.set_state(app, ST_CORRECTING)
            self.report_missing(app)
        else:
            self.issue(app)

    def cascade(self, applicant):
        """出证/收材料后，级联重检该申请人全部未结申请，直到无变化。"""
        changed = True
        while changed:
            changed = False
            for app in self.apps.values():
                if app.applicant != applicant or app.state in (ST_ISSUED, ST_TERMINATED):
                    continue
                if not self.missing_of(app):
                    self.issue(app)
                    changed = True

    # ---------- 申请流 ----------

    def add_application(self, app_id, matter, applicant, materials, day):
        if not app_id:
            self.error("申请错误", "存在无编号的申请，已忽略")
            return
        if app_id in self.apps:
            self.error("重复申请编号", "申请编号%s已存在，本次申请被忽略" % app_id)
            return
        if matter not in self.matters:
            self.error(
                "引用不存在的事项",
                "申请%s引用了不存在的事项「%s」，申请未受理" % (app_id, matter),
            )
            return
        app = Application(app_id=app_id, matter=matter, applicant=applicant, submit_date=day)
        self.apps[app_id] = app
        self.receive_materials(app, materials, "申请%s提交" % app_id)
        self.evaluate(app)          # 待审查 -> 补正中 / 已出证
        self.cascade(applicant)     # 同人共享材料，可能带动其他申请出证

    # ---------- 补正流 ----------

    def deadline_of(self, app):
        return app.submit_date + timedelta(days=self.deadline_days)

    def add_correction(self, app_id, materials, day):
        app = self.apps.get(app_id)
        if app is None:
            self.error("引用不存在的申请", "补正引用了不存在的申请%s，已忽略" % app_id)
            return
        if app.state == ST_TERMINATED:
            self.error("终止后补正", "申请%s已终止，不得再次补正，本次补正被拒绝" % app_id)
            return
        if app.state == ST_ISSUED:
            self.error("出证后补正", "申请%s已出证，无需补正，本次补正被忽略" % app_id)
            return
        deadline = self.deadline_of(app)
        if day > deadline:
            self.set_state(app, ST_TERMINATED)
            self.error(
                "补正超期",
                "申请%s补正期限为%s，补正日期%s已超期，申请终止"
                % (app_id, deadline.isoformat(), day.isoformat()),
            )
            return
        self.receive_materials(app, materials, "申请%s补正" % app_id)
        self.evaluate(app)          # 仍缺 -> 报告更新后的缺项；齐全 -> 出证
        self.cascade(app.applicant)

    # ---------- 结案扫描 ----------

    def final_sweep(self, today):
        """以最后事件日期为基准，终止逾期未补齐的在办申请。"""
        for app in self.apps.values():
            if app.state in (ST_PENDING, ST_CORRECTING):
                deadline = self.deadline_of(app)
                if today > deadline:
                    self.set_state(app, ST_TERMINATED)
                    self.error(
                        "补正超期",
                        "申请%s至%s仍未补齐材料，超过补正期限%s，申请终止"
                        % (app.app_id, today.isoformat(), deadline.isoformat()),
                    )


# ---------- 输入解析 ----------

def parse_day(value, office, context):
    if not value:
        return date.today()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        office.error("日期格式错误", "%s：日期「%s」无效（应为 YYYY-MM-DD），按当天处理" % (context, value))
        return date.today()


def run(data):
    office = NotaryOffice()
    if not isinstance(data, dict):
        office.error("输入错误", "顶层 JSON 必须是对象")
        return office, date.today()

    event_dates = []

    for item in data.get("matters") or []:
        office.define_matter(item.get("name"), item.get("materials"))

    for item in data.get("applications") or []:
        day = parse_day(item.get("date"), office, "申请%s" % item.get("id"))
        event_dates.append(day)
        office.add_application(
            item.get("id"), item.get("matter"), item.get("applicant") or "（未署名）",
            item.get("materials"), day,
        )

    for item in data.get("corrections") or []:
        day = parse_day(item.get("date"), office, "补正（申请%s）" % item.get("application"))
        event_dates.append(day)
        office.add_correction(item.get("application"), item.get("materials"), day)

    today = max(event_dates) if event_dates else date.today()
    office.final_sweep(today)
    return office, today


# ---------- 输出 ----------

def render(office, today):
    lines = []
    out = lines.append

    out("=" * 60)
    out("申请状态（基准日期：%s，补正期限：申请日起 %d 天）" % (today.isoformat(), office.deadline_days))
    out("=" * 60)
    if not office.apps:
        out("（无有效申请）")
    for app_id in sorted(office.apps):
        app = office.apps[app_id]
        missing = office.missing_of(app)
        missing_txt = "、".join(missing) if missing and app.state not in (ST_ISSUED, ST_TERMINATED) else "—"
        out("%s | 申请人：%s | 事项：%s | 状态：%s | 当前缺项：%s"
            % (app.app_id, app.applicant, app.matter, app.state, missing_txt))
        out("    状态轨迹：%s" % " -> ".join(app.history))

    out("")
    out("=" * 60)
    out("申请人材料池状态")
    out("=" * 60)
    if not office.pools:
        out("（空）")
    for applicant in sorted(office.pools):
        pool = office.pools[applicant]
        items = "、".join("%s(%s)" % (m, s) for m, s in pool.items()) or "（无）"
        out("%s：%s" % (applicant, items))

    out("")
    out("=" * 60)
    out("错误报告（共 %d 条）" % len(office.errors))
    out("=" * 60)
    if not office.errors:
        out("（无错误）")
    for i, e in enumerate(office.errors, 1):
        out("%d. [%s] %s" % (i, e["type"], e["message"]))

    return "\n".join(lines)


# ---------- 内置示例 ----------

DEMO = {
    "matters": [
        {"name": "继承公证", "materials": ["身份证", "户口簿", "死亡证明"]},
        {"name": "委托公证", "materials": ["身份证", "委托书"]},
        {"name": "学历公证", "materials": ["身份证", "毕业证"]},
    ],
    "applications": [
        {"id": "A1", "matter": "继承公证", "applicant": "张三", "date": "2026-01-05",
         "materials": ["身份证", "户口簿"]},
        {"id": "A2", "matter": "委托公证", "applicant": "张三", "date": "2026-01-06",
         "materials": ["委托书"]},
        {"id": "A3", "matter": "学历公证", "applicant": "李四", "date": "2026-01-06",
         "materials": ["身份证", "毕业证"]},
        {"id": "A4", "matter": "继承公证", "applicant": "王五", "date": "2026-01-10",
         "materials": ["身份证"]},
        {"id": "A5", "matter": "房产公证", "applicant": "赵六", "date": "2026-01-11",
         "materials": ["身份证"]},
        {"id": "A6", "matter": "学历公证", "applicant": "张三", "date": "2026-02-01",
         "materials": ["身份证"]},
        {"id": "A7", "matter": "学历公证", "applicant": "钱七", "date": "2026-01-02",
         "materials": ["身份证"]},
    ],
    "corrections": [
        {"application": "A1", "date": "2026-01-20", "materials": ["死亡证明"]},
        {"application": "A4", "date": "2026-03-01", "materials": ["户口簿"]},
        {"application": "A4", "date": "2026-03-02", "materials": ["死亡证明"]},
        {"application": "A9", "date": "2026-02-01", "materials": ["身份证"]},
        {"application": "A6", "date": "2026-02-10", "materials": ["毕业证"]},
        {"application": "A2", "date": "2026-02-11", "materials": ["身份证"]},
    ],
}


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        data = DEMO
    elif len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    office, today = run(data)
    print(render(office, today))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
