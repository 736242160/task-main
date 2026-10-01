#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newspaper_layout.py — 报纸版面放稿/撤稿排版工具（纯 Python 标准库，单文件）

用法：
    python3 newspaper_layout.py 输入文件      # 从文件读取
    python3 newspaper_layout.py < 输入文件    # 从标准输入读取
    python3 newspaper_layout.py -h            # 显示本说明

输入格式（UTF-8 文本，每行一条指令，# 之后为注释，空行忽略，按行顺序执行）：

    版面 <版面名> <容量>                        定义版面
    稿件 <编号> <篇幅> <优先级> <类型>           定义稿件（优先级：高/中/低；类型：新闻/广告）
    放 <版面名> <稿件编号>                      放稿
    撤 <版面名> <稿件编号>                      撤稿

挤占规则（自定）：
  1. 位阶 = (优先级, 类型)，优先级 高>中>低，同优先级时 新闻>广告。
     理由：新闻时效性强、截稿压力高，广告可让位；优先级是硬指标，类型只做同级的次序依据。
  2. 放稿空间不足时，按“位阶最低者优先”挤占位阶【严格低于】新稿的稿件，直到放得下。
     同位阶稿件互不挤占 —— 保证同级稿件稳定，避免来回抖动。
  3. 被挤稿件按版面定义顺序寻找备用版面，在备用版面上可继续级联挤占更低位阶稿件。
     因每次挤占位阶严格下降，级联必然终止，不会死循环。
  4. 所有备用版面都放不下时，被挤稿件回到“待排”并产生报告。
  5. 撤稿释放空间后自动级联整理：
     a) 待排稿件按（优先级高→低、新闻→广告、编号）顺序尝试回占（优先回原版面）；
     b) 曾被挤到备用版面的稿件，若原版面已放得下，则级联搬回原版面（不挤占别人）。

稿件状态机：待排 --放成功--> 已排 --撤--> 撤稿；已排 --被挤--> 待排；撤稿/待排可再放。

输出：全部操作执行完后，打印 版面状态 / 稿件状态 / 错误与报告清单。
"""

import sys

PRIORITY_RANK = {'高': 3, '中': 2, '低': 1}
TYPE_RANK = {'新闻': 2, '广告': 1}


class Page:
    def __init__(self, name, capacity):
        self.name = name
        self.capacity = capacity
        self.articles = []  # 稿件编号，按放入顺序


class Article:
    def __init__(self, aid, size, priority, ptype):
        self.id = aid
        self.size = size
        self.priority = priority
        self.ptype = ptype
        self.state = '待排'        # 待排 / 已排 / 撤稿
        self.page = None           # 当前所在版面名（已排时有效）
        self.home = None           # 最近一次“放”操作指定的版面（回占目标）
        self.displaced = False     # 是否曾被挤占而离开 home

    @property
    def rank(self):
        return (PRIORITY_RANK[self.priority], TYPE_RANK[self.ptype])


class Layout:
    def __init__(self):
        self.pages = {}       # 版面名 -> Page
        self.page_order = []  # 版面定义顺序（备用版面查找顺序）
        self.articles = {}    # 编号 -> Article
        self.reports = []     # (行号, 级别, 消息)

    # ---------- 工具 ----------
    def report(self, lineno, level, msg):
        self.reports.append((lineno, level, msg))

    def used(self, page):
        return sum(self.articles[aid].size for aid in page.articles)

    def remaining(self, page):
        return page.capacity - self.used(page)

    # ---------- 放稿核心 ----------
    def _evict_plan(self, art, page):
        """计算挤占方案。返回 (需挤占的稿件列表, 超出量)；放不下时列表为 None。"""
        need = art.size - self.remaining(page)
        if need <= 0:
            return [], 0
        candidates = [self.articles[aid] for aid in page.articles
                      if self.articles[aid].rank < art.rank]
        candidates.sort(key=lambda a: (a.rank, a.id))  # 位阶最低者优先被挤
        freed, chosen = 0, []
        for cand in candidates:
            chosen.append(cand)
            freed += cand.size
            if need <= freed:
                return chosen, 0
        return None, need - freed  # 挤光所有低位阶稿件仍放不下

    def _place_on(self, art, page, lineno):
        """把 art 放到 page（必要时级联挤占）。返回 (是否成功, 超出量)。失败无副作用。"""
        evict, excess = self._evict_plan(art, page)
        if evict is None:
            return False, excess
        for victim in evict:
            page.articles.remove(victim.id)
            victim.state = '待排'
            victim.page = None
            victim.displaced = True
            self.report(lineno, '报告',
                        '稿件 %s 被 %s 挤出版面 %s' % (victim.id, art.id, page.name))
        page.articles.append(art.id)
        art.state = '已排'
        art.page = page.name
        if page.name == art.home:
            art.displaced = False
        for victim in evict:  # 被挤稿件级联寻找备用版面
            self._cascade(victim, exclude=page.name, lineno=lineno)
        return True, 0

    def _cascade(self, art, exclude, lineno):
        """被挤稿件按版面定义顺序寻找备用版面（在备用版面上可继续级联挤占）。"""
        for name in self.page_order:
            if name == exclude:
                continue
            ok, _ = self._place_on(art, self.pages[name], lineno)
            if ok:
                self.report(lineno, '报告',
                            '稿件 %s 级联移至备用版面 %s' % (art.id, name))
                return
        self.report(lineno, '报告',
                    '稿件 %s 所有备用版面均放不下，回到待排' % art.id)

    # ---------- 操作 ----------
    def op_place(self, lineno, page_name, art_id):
        page = self.pages.get(page_name)
        art = self.articles.get(art_id)
        if page is None:
            self.report(lineno, '错误', '版面 %s 不存在，放稿失败' % page_name)
            return
        if art is None:
            self.report(lineno, '错误', '稿件 %s 不存在，放稿失败' % art_id)
            return
        if art.state == '已排':
            self.report(lineno, '错误',
                        '稿件 %s 已排在版面 %s，重复放稿被拒绝' % (art_id, art.page))
            return
        art.home = page_name
        ok, excess = self._place_on(art, page, lineno)
        if not ok:
            art.state = '待排'
            art.displaced = False
            self.report(lineno, '错误',
                        '稿件 %s（篇幅 %d）超出版面 %s 可用容量，超出 %d'
                        % (art_id, art.size, page_name, excess))

    def op_remove(self, lineno, page_name, art_id):
        page = self.pages.get(page_name)
        art = self.articles.get(art_id)
        if page is None:
            self.report(lineno, '错误', '版面 %s 不存在，撤稿失败' % page_name)
            return
        if art is None:
            self.report(lineno, '错误', '稿件 %s 不存在，撤稿失败' % art_id)
            return
        if art.state != '已排':
            self.report(lineno, '错误',
                        '稿件 %s 未在任何版面上（状态：%s），撤稿失败'
                        % (art_id, art.state))
            return
        if art.page != page_name:
            self.report(lineno, '错误',
                        '稿件 %s 在版面 %s，不在 %s，撤稿失败'
                        % (art_id, art.page, page_name))
            return
        page.articles.remove(art_id)
        art.state = '撤稿'
        art.page = None
        art.displaced = False
        self._rebalance(lineno)  # 撤稿释放空间后级联整理

    def _rebalance(self, lineno):
        """撤稿后：1) 待排稿件级联回占；2) 被挤走的稿件搬回原版面。循环至稳定。"""
        changed = True
        while changed:
            changed = False
            pending = [a for a in self.articles.values() if a.state == '待排']
            pending.sort(key=lambda a: (-a.rank[0], -a.rank[1], a.id))
            for art in pending:
                targets = ([art.home] if art.home in self.pages else [])
                targets += [n for n in self.page_order if n not in targets]
                for name in targets:
                    ok, _ = self._place_on(art, self.pages[name], lineno)
                    if ok:
                        self.report(lineno, '报告',
                                    '释放空间后，稿件 %s 级联回占版面 %s'
                                    % (art.id, name))
                        changed = True
                        break
            for art in self.articles.values():
                if (art.state == '已排' and art.displaced
                        and art.home in self.pages and art.page != art.home):
                    home = self.pages[art.home]
                    if self.remaining(home) >= art.size:
                        self.pages[art.page].articles.remove(art.id)
                        home.articles.append(art.id)
                        art.page = art.home
                        art.displaced = False
                        self.report(lineno, '报告',
                                    '稿件 %s 级联回到原版面 %s' % (art.id, art.home))
                        changed = True

    # ---------- 解析 ----------
    def run(self, text):
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            tok = line.split()
            head = tok[0]
            if head == '版面':
                if len(tok) != 3:
                    self.report(lineno, '错误', '版面定义格式错误：%s' % line)
                    continue
                name = tok[1]
                try:
                    cap = int(tok[2])
                    if cap <= 0:
                        raise ValueError
                except ValueError:
                    self.report(lineno, '错误', '版面 %s 容量无效：%s' % (name, tok[2]))
                    continue
                if name in self.pages:
                    self.report(lineno, '错误', '版面 %s 重复定义，忽略' % name)
                    continue
                self.pages[name] = Page(name, cap)
                self.page_order.append(name)
            elif head == '稿件':
                if len(tok) != 5:
                    self.report(lineno, '错误', '稿件定义格式错误：%s' % line)
                    continue
                aid, size_s, prio, ptype = tok[1], tok[2], tok[3], tok[4]
                try:
                    size = int(size_s)
                    if size <= 0:
                        raise ValueError
                except ValueError:
                    self.report(lineno, '错误', '稿件 %s 篇幅无效：%s' % (aid, size_s))
                    continue
                if prio not in PRIORITY_RANK:
                    self.report(lineno, '错误',
                                '稿件 %s 优先级无效：%s（应为 高/中/低）' % (aid, prio))
                    continue
                if ptype not in TYPE_RANK:
                    self.report(lineno, '错误',
                                '稿件 %s 类型无效：%s（应为 新闻/广告）' % (aid, ptype))
                    continue
                if aid in self.articles:
                    self.report(lineno, '错误', '稿件 %s 重复定义，忽略' % aid)
                    continue
                self.articles[aid] = Article(aid, size, prio, ptype)
            elif head == '放':
                if len(tok) != 3:
                    self.report(lineno, '错误', '放稿格式错误：%s' % line)
                    continue
                self.op_place(lineno, tok[1], tok[2])
            elif head == '撤':
                if len(tok) != 3:
                    self.report(lineno, '错误', '撤稿格式错误：%s' % line)
                    continue
                self.op_remove(lineno, tok[1], tok[2])
            else:
                self.report(lineno, '错误', '无法识别的指令：%s' % line)

    # ---------- 输出 ----------
    def render(self):
        out = ['====== 版面状态 ======']
        if not self.page_order:
            out.append('（无版面）')
        for name in self.page_order:
            page = self.pages[name]
            out.append('版面 %s：容量 %d，已用 %d，剩余 %d'
                       % (name, page.capacity, self.used(page), self.remaining(page)))
            if not page.articles:
                out.append('    （空）')
            for aid in page.articles:
                a = self.articles[aid]
                out.append('    - %s（%s/%s/篇幅%d）' % (aid, a.priority, a.ptype, a.size))
        out.append('')
        out.append('====== 稿件状态 ======')
        if not self.articles:
            out.append('（无稿件）')
        for aid, a in self.articles.items():
            loc = '（%s）' % a.page if a.state == '已排' else ''
            out.append('%s：%s%s' % (aid, a.state, loc))
        out.append('')
        out.append('====== 错误与报告清单 ======')
        if not self.reports:
            out.append('（无）')
        for lineno, level, msg in self.reports:
            out.append('[%s] 第%d行：%s' % (level, lineno, msg))
        return '\n'.join(out)


def main(argv):
    if len(argv) > 1:
        if argv[1] in ('-h', '--help'):
            print(__doc__)
            return 0
        with open(argv[1], encoding='utf-8') as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    layout = Layout()
    layout.run(text)
    print(layout.render())
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
