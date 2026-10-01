#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newspaper_layout.py —— 报纸版面排版模拟工具（纯 Python 标准库，单文件）

用法：
    python3 newspaper_layout.py 输入文件     # 从文件读取
    python3 newspaper_layout.py              # 从标准输入读取
    python3 newspaper_layout.py --demo       # 运行内置示例

输入格式（# 之后为注释，空行忽略；定义与操作可混排，按行序生效，状态跨操作延续）：
    版面 <名称> <容量>
    稿件 <编号> <篇幅> <优先级:高|中|低> <类型:新闻|广告>
    放 <版面> <稿件>
    撤 <版面> <稿件>

输出：执行过程（挤占/级联/回占事件）、版面状态、稿件状态、错误清单。

自定规则及理由：
 1. 优先级 高>中>低。放稿容量不足时，只允许挤占"严格更低"优先级的稿件，
    同优先级互不挤占 —— 保证同级先来先排的稳定性，避免来回抖动。
 2. 挤占顺序：先挤优先级最低者，同级内先挤篇幅最大者 —— 用尽量少的稿件
    腾出空间，减少级联移动次数。
 3. 挤占是原子的：若挤光所有更低优先级稿件仍放不下，则一个都不挤，
    直接报告（版面、稿件、超出量），版面保持原状 —— 不为必然失败的放稿白折腾。
 4. 备用版面顺序：按版面定义顺序，从当前版面的下一个开始循环（不含当前版）。
    被挤稿件依次尝试各备用版面，在备用版面上同样适用挤占规则（级联挤占）；
    所有备用版面都放不下时报告级联失败，稿件转为"待排"。
 5. 每个稿件记录"归属版面"（最近一次显式放稿成功的版面）。某版面因撤稿
    释放空间后，归属该版且被挤走（在别处或待排）的稿件按"优先级高者优先、
    同级先被挤者优先"依次级联回占；回占只用剩余容量，不再挤占别人；
    回占在源版面释放的空间会继续触发源版面的回占检查（级联释放）。
 6. 撤稿：稿件状态变为"撤稿"，清除归属，占位立即释放并触发回占级联。
 7. 稿件状态机：待排 -> 已排 -> (被挤)已排/待排 -> (撤)撤稿；撤稿后可再放。
"""

import sys

PRIORITIES = {"高": 3, "中": 2, "低": 1}
PRIO_NAMES = {v: k for k, v in PRIORITIES.items()}
TYPES = {"新闻", "广告"}


class Page:
    def __init__(self, name, capacity):
        self.name = name
        self.capacity = capacity
        self.articles = []


class Article:
    def __init__(self, aid, size, prio, ptype):
        self.aid = aid
        self.size = size
        self.prio = prio
        self.ptype = ptype
        self.status = "待排"      # 待排 / 已排 / 撤稿
        self.page = None          # 当前所在版面（已排时）
        self.home = None          # 归属版面：最近一次显式放稿成功的版面
        self.disp_seq = None      # 被挤序号（回占排序用），非被挤状态为 None


class Layout:
    def __init__(self):
        self.pages = {}
        self.page_order = []
        self.articles = {}
        self.errors = []
        self.events = []
        self.context = "定义"
        self.op_no = 0
        self._seq = 0

    def error(self, msg):
        self.errors.append("[%s] %s" % (self.context, msg))

    def event(self, msg):
        self.events.append("[%s] %s" % (self.context, msg))

    def used(self, page):
        return sum(self.articles[a].size for a in page.articles)

    def remaining(self, page):
        return page.capacity - self.used(page)

    def _put(self, art, page):
        page.articles.append(art.aid)
        art.page = page.name
        art.status = "已排"

    def _lift(self, art, page):
        page.articles.remove(art.aid)
        art.page = None

    def try_place(self, art, pname):
        """尝试把 art 放入 pname（必要时原子挤占严格更低优先级稿件）。
        成功返回 True；放不下时不做任何改动，返回 False。"""
        page = self.pages[pname]
        lack = art.size - self.remaining(page)
        if lack <= 0:
            self._put(art, page)
            return True
        victims = [self.articles[a] for a in page.articles
                   if self.articles[a].prio < art.prio]
        victims.sort(key=lambda a: (a.prio, -a.size))
        chosen, freed = [], 0
        for v in victims:
            if freed >= lack:
                break
            chosen.append(v)
            freed += v.size
        if freed < lack:
            return False
        for v in chosen:
            self._lift(v, page)
            self._seq += 1
            v.disp_seq = self._seq
            self.event("稿件%s（优先级%s，篇幅%d）被稿件%s挤出版面%s"
                       % (v.aid, PRIO_NAMES[v.prio], v.size, art.aid, pname))
        self._put(art, page)
        for v in sorted(chosen, key=lambda a: (-a.prio, a.disp_seq)):
            self.relocate(v, pname)
        return True

    def relocate(self, art, from_pname):
        """被挤稿件依次尝试备用版面（级联）；全部失败则转待排并报错。"""
        idx = self.page_order.index(from_pname)
        backups = self.page_order[idx + 1:] + self.page_order[:idx]
        for pname in backups:
            if self.try_place(art, pname):
                self.event("稿件%s级联移动到备用版面%s" % (art.aid, pname))
                return
        art.status = "待排"
        self.error("级联挤占失败：稿件%s（篇幅%d）被挤出版面%s后，"
                   "所有备用版面均放不下，稿件转为待排"
                   % (art.aid, art.size, from_pname))

    def release_cascade(self, pname):
        """pname 释放空间后，归属该版且被挤走的稿件按规则级联回占。"""
        page = self.pages[pname]
        while True:
            cands = [a for a in self.articles.values()
                     if a.home == pname and a.disp_seq is not None
                     and a.status != "撤稿" and a.page != pname]
            cands.sort(key=lambda a: (-a.prio, a.disp_seq))
            back = None
            for a in cands:
                if a.size <= self.remaining(page):
                    back = a
                    break
            if back is None:
                return
            src = back.page
            if src is not None:
                self._lift(back, self.pages[src])
            self._put(back, page)
            back.disp_seq = None
            self.event("稿件%s级联回占版面%s%s"
                       % (back.aid, pname,
                          "（自版面%s移回）" % src if src else "（自待排安置）"))
            if src is not None:
                self.release_cascade(src)

    def op_place(self, pname, aid):
        page = self.pages.get(pname)
        if page is None:
            self.error("放稿失败：版面“%s”不存在" % pname)
            return
        art = self.articles.get(aid)
        if art is None:
            self.error("放稿失败：稿件“%s”不存在" % aid)
            return
        if art.status == "已排":
            self.error("重复放稿：稿件%s已在版面%s，不能重复放稿" % (aid, art.page))
            return
        if self.try_place(art, pname):
            art.home = pname
            art.disp_seq = None
            self.event("放稿成功：稿件%s（篇幅%d）放入版面%s，剩余容量%d"
                       % (aid, art.size, pname, self.remaining(page)))
        else:
            self.error("版面%s剩余容量不足：稿件%s篇幅%d，剩余%d，超出%d"
                       % (pname, aid, art.size, self.remaining(page),
                          art.size - self.remaining(page)))

    def op_remove(self, pname, aid):
        page = self.pages.get(pname)
        if page is None:
            self.error("撤稿失败：版面“%s”不存在" % pname)
            return
        art = self.articles.get(aid)
        if art is None:
            self.error("撤稿失败：稿件“%s”不存在" % aid)
            return
        if art.page != pname:
            where = ("版面%s" % art.page) if art.page \
                else ("未排版（状态：%s）" % art.status)
            self.error("撤稿失败：稿件%s不在版面%s（当前%s）" % (aid, pname, where))
            return
        self._lift(art, page)
        art.status = "撤稿"
        art.home = None
        art.disp_seq = None
        self.event("撤稿成功：稿件%s从版面%s撤下，释放篇幅%d，剩余容量%d"
                   % (aid, pname, art.size, self.remaining(page)))
        self.release_cascade(pname)


def run(text):
    layout = Layout()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        layout.context = "第%d行" % lineno
        if cmd == "版面" and len(args) == 2:
            name, cap = args
            try:
                cap = int(cap)
                if cap <= 0:
                    raise ValueError
            except ValueError:
                layout.error("版面容量必须是正整数：%r" % raw.strip())
                continue
            if name in layout.pages:
                layout.error("版面重复定义：%s" % name)
                continue
            layout.pages[name] = Page(name, cap)
            layout.page_order.append(name)
        elif cmd == "稿件" and len(args) == 4:
            aid, size, prio, ptype = args
            try:
                size = int(size)
                if size <= 0:
                    raise ValueError
            except ValueError:
                layout.error("稿件篇幅必须是正整数：%r" % raw.strip())
                continue
            if prio not in PRIORITIES:
                layout.error("优先级必须是 高/中/低：%r" % raw.strip())
                continue
            if ptype not in TYPES:
                layout.error("类型必须是 新闻/广告：%r" % raw.strip())
                continue
            if aid in layout.articles:
                layout.error("稿件重复定义：%s" % aid)
                continue
            layout.articles[aid] = Article(aid, size, PRIORITIES[prio], ptype)
        elif cmd in ("放", "撤") and len(args) == 2:
            layout.op_no += 1
            layout.context = "操作%d（%s）" % (layout.op_no, line)
            if cmd == "放":
                layout.op_place(args[0], args[1])
            else:
                layout.op_remove(args[0], args[1])
        else:
            layout.error("无法解析的行：%r" % raw.strip())
    return layout


def render(layout):
    out = ["========== 执行过程 =========="]
    out.extend("  " + e for e in layout.events) if layout.events \
        else out.append("  （无）")
    out.append("========== 版面状态 ==========")
    for name in layout.page_order:
        p = layout.pages[name]
        u = layout.used(p)
        out.append("版面 %s：容量 %d，已用 %d，剩余 %d"
                   % (name, p.capacity, u, p.capacity - u))
        if p.articles:
            for aid in p.articles:
                a = layout.articles[aid]
                out.append("    - %s（篇幅%d，优先级%s，类型%s）"
                           % (aid, a.size, PRIO_NAMES[a.prio], a.ptype))
        else:
            out.append("    - （空）")
    out.append("========== 稿件状态 ==========")
    for aid, a in layout.articles.items():
        loc = "，所在版面：%s" % a.page if a.page else ""
        home = "，归属版面：%s" % a.home if a.home and a.home != a.page else ""
        out.append("  %s：%s%s%s" % (aid, a.status, loc, home))
    out.append("========== 错误清单 ==========")
    if layout.errors:
        for i, e in enumerate(layout.errors, 1):
            out.append("  %d. %s" % (i, e))
    else:
        out.append("  （无错误）")
    return "\n".join(out)


SAMPLE = """\
# ---- 版面定义：版面 名称 容量 ----
版面 头版 100
版面 二版 50
版面 三版 30

# ---- 稿件定义：稿件 编号 篇幅 优先级 类型 ----
稿件 N1 40 中 新闻
稿件 N2 35 低 新闻
稿件 N3 20 低 新闻
稿件 B1 25 中 新闻
稿件 A1 50 高 广告
稿件 A2 45 高 广告

# ---- 排版流：放|撤 版面 稿件 ----
放 头版 N1      # 头版剩60
放 头版 N2      # 头版剩25
放 头版 A1      # 高优先级广告挤走低优先级N2，N2级联到二版
放 头版 A1      # 重复放稿 -> 错误
放 二版 N3      # 同级不能挤占 -> 超出量错误
放 三版 B1      # 正常放稿
放 头版 A2      # 挤走N1 -> N1挤走二版N2 -> N2无处可去 -> 级联失败
撤 头版 A1      # 撤广告，释放空间 -> 被挤新闻N1级联回占头版
撤 头版 A2      # 再释放 -> 待排的N2级联回占头版
放 头版 X9      # 稿件不存在 -> 错误
放 四版 N1      # 版面不存在 -> 错误
撤 头版 N3      # N3未排版 -> 错误
撤 二版 N1      # N1在头版不在二版 -> 错误
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        print("---------- 内置示例输入 ----------")
        print(SAMPLE)
        print(render(run(SAMPLE)))
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            print(render(run(f.read())))
    else:
        print(render(run(sys.stdin.read())))


if __name__ == "__main__":
    main(sys.argv)
