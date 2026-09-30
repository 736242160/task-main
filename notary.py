#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notary.py — 公证申请材料复用与状态联动工具（纯 Python 标准库，单文件）

输入：JSON 文件，含三个流（均可选，缺省为空），按 事项 -> 申请 -> 补正 顺序处理，
状态跨流延续：

  {
    "matters":      [{"name": "继承公证", "materials": ["身份证", ...]}, ...],
    "applications": [{"id": "A1", "matter": "继承公证", "applicant": "张三",
                      "materials": ["身份证"], "date": "2026-03-01"}, ...],
    "corrections":  [{"application": "A1", "materials": ["死亡证明"],
                      "date": "2026-03-10"}, ...]
  }

date 字段可省略（缺省视为同一基准日，不会触发超期）。

补正期限：自申请提交之日起 15 个自然日。
  理由：参照《公证程序规则》中当事人应在公证机构要求的合理期限内补充
  材料的规定，行政实践通常取 15 日；期限自申请日起算而非自每次补正
  起算，可避免反复补正无限续期。

材料共享：同一申请人的所有申请共享一个材料池，任一申请（或补正）提交
的材料即时入池；每次入池后级联重检该申请人全部未终结申请的缺项，
材料齐全且未终止即出证。

用法：
  python3 notary.py 输入.json   # 处理输入文件
  python3 notary.py             # 运行内置演示（覆盖全部规则）
  python3 notary.py --demo      # 打印演示输入 JSON，可重定向为模板
"""

import json
import sys
from datetime import date

DEADLINE_DAYS = 15                 # 补正期限（自然日），理由见模块 docstring
BASE_DATE = date(2026, 1, 1)       # 缺省日期基准

PENDING = "待审查"
CORRECTING = "补正中"
ISSUED = "已出证"
TERMINATED = "终止"


class NotaryEngine:
    def __init__(self, deadline_days=DEADLINE_DAYS):
        self.deadline_days = deadline_days
        self.matters = {}    # 事项名 -> 所需材料列表（保序去重）
        self.apps = {}       # 申请编号 -> 申请记录（保插入序）
        self.pools = {}      # 申请人 -> 共享材料池 set
        self.reports = []    # (类别, 内容) 错误与事件报告

    def report(self, kind, msg):
        self.reports.append((kind, msg))

    @staticmethod
    def _parse_date(value):
        if not value:
            return BASE_DATE
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    # ---- 事项定义流 ----
    def load_matters(self, matters):
        for item in matters or []:
            name = item.get("name")
            if not name:
                self.report("定义错误", "事项缺少名称，已忽略")
                continue
            if name in self.matters:
                self.report("定义错误", "事项[%s]重复定义，以新定义覆盖" % name)
            self.matters[name] = list(dict.fromkeys(item.get("materials") or []))

    # ---- 材料池 ----
    def _pool(self, applicant):
        return self.pools.setdefault(applicant, set())

    def _add_materials(self, app_id, applicant, materials):
        """材料并入申请人共享池，报告重复提交（池内重复及同批重复）。"""
        pool = self._pool(applicant)
        batch_seen = set()
        for mat in materials or []:
            if mat in batch_seen or mat in pool:
                self.report("材料重复提交", "申请[%s] 材料[%s]已提交过" % (app_id, mat))
                continue
            batch_seen.add(mat)
            pool.add(mat)

    # ---- 状态联动 ----
    def _evaluate(self, app):
        """重检单个申请缺项并联动状态；材料齐全且未终止则出证。"""
        if app["status"] in (ISSUED, TERMINATED):
            return
        required = self.matters[app["matter"]]
        missing = [m for m in required if m not in self._pool(app["applicant"])]
        app["missing"] = missing
        if not missing:
            app["status"] = ISSUED
            self.report("出证", "申请[%s](%s) 材料齐全，予以出证" % (app["id"], app["matter"]))
        elif app["status"] != CORRECTING:
            app["status"] = CORRECTING
            self.report("材料缺失", "申请[%s](事项:%s) 缺材料:%s"
                        % (app["id"], app["matter"], "、".join(missing)))

    def _recheck_applicant(self, applicant):
        """材料池变化后，级联重检该申请人全部未终结申请（含共享联动）。"""
        for app in self.apps.values():
            if app["applicant"] == applicant:
                self._evaluate(app)

    # ---- 申请流 ----
    def submit_application(self, item):
        app_id = item.get("id")
        matter = item.get("matter")
        applicant = item.get("applicant")
        if not app_id:
            self.report("定义错误", "申请缺少编号，已忽略")
            return
        if app_id in self.apps:
            self.report("定义错误", "申请[%s]编号重复，已忽略" % app_id)
            return
        if matter not in self.matters:
            self.report("引用不存在的事项", "申请[%s]引用事项[%s]，已忽略" % (app_id, matter))
            return
        day = self._parse_date(item.get("date"))
        if day is None:
            self.report("日期错误", "申请[%s]日期无法解析，按基准日处理" % app_id)
            day = BASE_DATE
        self.apps[app_id] = {
            "id": app_id, "matter": matter, "applicant": applicant,
            "date": day, "status": PENDING, "missing": [],
        }
        self._add_materials(app_id, applicant, item.get("materials"))
        self._recheck_applicant(applicant)  # 待审查 -> 补正中/已出证

    # ---- 补正流 ----
    def submit_correction(self, item):
        app_id = item.get("application")
        app = self.apps.get(app_id)
        if app is None:
            self.report("引用不存在的申请", "补正引用申请[%s]，已忽略" % app_id)
            return
        if app["status"] == TERMINATED:
            self.report("终止后补正", "申请[%s]已终止，补正被拒绝" % app_id)
            return
        if app["status"] == ISSUED:
            self.report("重复补正", "申请[%s]已出证，无需补正，已忽略" % app_id)
            return
        day = self._parse_date(item.get("date"))
        if day is None:
            self.report("日期错误", "申请[%s]的补正日期无法解析，按基准日处理" % app_id)
            day = BASE_DATE
        if (day - app["date"]).days > self.deadline_days:
            app["status"] = TERMINATED
            app["missing"] = []
            self.report("补正超期", "申请[%s]补正超出%d日期限（申请日%s，补正日%s），申请终止"
                        % (app_id, self.deadline_days, app["date"], day))
            return
        self._add_materials(app_id, app["applicant"], item.get("materials"))
        self._recheck_applicant(app["applicant"])


def run(data):
    engine = NotaryEngine()
    engine.load_matters(data.get("matters"))
    for item in data.get("applications") or []:
        engine.submit_application(item)
    for item in data.get("corrections") or []:
        engine.submit_correction(item)
    return engine


def print_result(engine):
    print("=" * 64)
    print("申请状态")
    print("=" * 64)
    if not engine.apps:
        print("（无申请）")
    for app in engine.apps.values():
        missing = "、".join(app["missing"]) if app["missing"] else "无"
        print("申请[%s] 事项:%s 申请人:%s 状态:%s 缺项:%s"
              % (app["id"], app["matter"], app["applicant"], app["status"], missing))
    print()
    print("=" * 64)
    print("错误与事件报告")
    print("=" * 64)
    if not engine.reports:
        print("（无）")
    for i, (kind, msg) in enumerate(engine.reports, 1):
        print("%2d. [%s] %s" % (i, kind, msg))


DEMO = {
    "matters": [
        {"name": "继承公证", "materials": ["身份证", "户口本", "死亡证明"]},
        {"name": "委托公证", "materials": ["身份证", "委托书"]},
    ],
    "applications": [
        # 缺 死亡证明 -> 补正中
        {"id": "A1", "matter": "继承公证", "applicant": "张三",
         "materials": ["身份证", "户口本"], "date": "2026-03-01"},
        # 身份证 与 A1 重复提交；缺 委托书 -> 补正中
        {"id": "A2", "matter": "委托公证", "applicant": "张三",
         "materials": ["身份证"], "date": "2026-03-02"},
        # 材料齐全 -> 直接出证
        {"id": "A3", "matter": "继承公证", "applicant": "李四",
         "materials": ["身份证", "户口本", "死亡证明"], "date": "2026-03-03"},
        # 引用不存在的事项
        {"id": "A4", "matter": "学历公证", "applicant": "王五",
         "materials": ["身份证"], "date": "2026-03-03"},
        # 将补正超期 -> 终止
        {"id": "A5", "matter": "委托公证", "applicant": "赵六",
         "materials": ["身份证"], "date": "2026-03-01"},
        # 共享张三材料池，暂缺 死亡证明 -> 补正中；A1 补正后级联出证
        {"id": "A6", "matter": "继承公证", "applicant": "张三",
         "materials": [], "date": "2026-03-05"},
    ],
    "corrections": [
        # A1 补齐 -> 出证；级联重检使共享材料池的 A6 一并出证
        {"application": "A1", "materials": ["死亡证明"], "date": "2026-03-10"},
        # 身份证 重复提交要报告；补 委托书 后 A2 出证
        {"application": "A2", "materials": ["委托书", "身份证"], "date": "2026-03-05"},
        # 距申请日 19 天 > 15 天 -> 补正超期，A5 终止
        {"application": "A5", "materials": ["委托书"], "date": "2026-03-20"},
        # 终止申请不得再次补正
        {"application": "A5", "materials": ["委托书"], "date": "2026-03-21"},
        # 引用不存在的申请
        {"application": "A9", "materials": ["身份证"], "date": "2026-03-10"},
    ],
}


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        print(json.dumps(DEMO, ensure_ascii=False, indent=2))
        return 0
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        print("（未提供输入文件，运行内置演示；--demo 可导出演示输入）\n")
        data = DEMO
    print_result(run(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
