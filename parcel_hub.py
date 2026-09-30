#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parcel_hub.py —— 包裹分拣 / 转运 / 故障级联单文件模拟器（仅 Python 标准库）

输入为行式指令流（# 起为注释，空行忽略，字段以空白分隔）：

    限重 <公斤>                                   # 可选，默认 30.0；须出现在包裹流之前
    格口 <名称> <容量> <备用格口|无|-> [关键词1,关键词2,...]
    包裹 <编号> <收件地址> <重量公斤>             # 地址可含空格，重量取最后一个字段
    分拣 <包裹编号> <格口名称>
    转运 <格口名称> <目的站>
    故障 <格口名称>

说明：
  * 格口未给关键词时，默认以格口名作为关键词；关键词 "*" 表示兜底（匹配任意地址）。
  * 各类指令可按任意顺序交错出现，状态跨流延续（单遍顺序模拟）。
  * 格口/备用格口/包裹须先定义后引用，否则记入错误清单。

用法：
    python3 parcel_hub.py 输入文件
    cat 输入文件 | python3 parcel_hub.py
"""

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 包裹状态机：待分拣 -> 已分拣 -> 转运中 -> 已转运
PENDING, SORTED, IN_TRANSIT, DONE = "待分拣", "已分拣", "转运中", "已转运"

# 超重规则：默认单件上限 30 公斤。
# 理由：参照国内快递/快运行业惯例，30kg 是自动分拣线（交叉带、摆轮等）
# 普遍的单件重量上限，超过须走大件人工/专线通道，不应进入自动分拣格口。
DEFAULT_WEIGHT_LIMIT = 30.0

NO_FALLBACK = ("无", "-", "none", "None")


@dataclass
class Chute:
    name: str
    capacity: int
    fallback: Optional[str]
    keywords: List[str]
    faulty: bool = False
    packages: List[str] = field(default_factory=list)  # 当前在口的包裹编号（状态=已分拣）
    def_line: int = 0  # 定义所在行号

    def matches(self, address: str) -> bool:
        """地址关键词匹配：任一关键词为地址子串即命中；"*" 匹配一切。"""
        for kw in self.keywords:
            if kw == "*" or kw in address:
                return True
        return False


@dataclass
class Parcel:
    pid: str
    address: str
    weight: float
    status: str = PENDING
    chute: Optional[str] = None        # 所在格口（已分拣）或来源格口（转运中/已转运）
    destination: Optional[str] = None  # 转运目的站
    overweight: bool = False


class Simulator:
    def __init__(self) -> None:
        self.chutes: Dict[str, Chute] = {}
        self.parcels: Dict[str, Parcel] = {}
        self.errors: List[str] = []
        self.limit = DEFAULT_WEIGHT_LIMIT

    # ---------- 错误记录 ----------
    def err(self, lineno: int, kind: str, msg: str) -> None:
        self.errors.append("第{}行 [{}] {}".format(lineno, kind, msg))

    # ---------- 指令：限重 ----------
    def do_limit(self, lineno: int, args: List[str]) -> None:
        if len(args) != 1:
            self.err(lineno, "格式错误", "限重指令需要 1 个参数")
            return
        try:
            self.limit = float(args[0])
        except ValueError:
            self.err(lineno, "格式错误", "限重值无法解析: {}".format(args[0]))

    # ---------- 指令：格口定义 ----------
    def do_chute(self, lineno: int, args: List[str]) -> None:
        if len(args) < 3:
            self.err(lineno, "格式错误", "格口指令需要至少 3 个参数: 名称 容量 备用格口 [关键词]")
            return
        name, cap_s, fallback = args[0], args[1], args[2]
        if name in self.chutes:
            self.err(lineno, "重复定义", "格口 {} 已定义".format(name))
            return
        try:
            capacity = int(cap_s)
            if capacity < 0:
                raise ValueError
        except ValueError:
            self.err(lineno, "格式错误", "格口 {} 容量非法: {}".format(name, cap_s))
            return
        # 备用格口允许前向引用，全部指令处理完后再统一校验（见 run 末尾）。
        fb: Optional[str] = None if fallback in NO_FALLBACK else fallback
        keywords: List[str] = []
        if len(args) >= 4:
            for piece in args[3].replace("，", ",").split(","):
                piece = piece.strip()
                if piece:
                    keywords.append(piece)
        if not keywords:
            keywords = [name]  # 默认以格口名为关键词
        self.chutes[name] = Chute(name, capacity, fb, keywords, def_line=lineno)

    # ---------- 指令：包裹登记 ----------
    def do_parcel(self, lineno: int, args: List[str]) -> None:
        if len(args) < 3:
            self.err(lineno, "格式错误", "包裹指令需要至少 3 个参数: 编号 地址 重量")
            return
        pid, weight_s = args[0], args[-1]
        address = " ".join(args[1:-1])
        if pid in self.parcels:
            self.err(lineno, "重复定义", "包裹 {} 已登记".format(pid))
            return
        try:
            weight = float(weight_s)
        except ValueError:
            self.err(lineno, "格式错误", "包裹 {} 重量无法解析: {}".format(pid, weight_s))
            return
        parcel = Parcel(pid, address, weight)
        # 超重规则：超过限重的包裹不进入分拣流程，直接报告并留在待分拣。
        if weight > self.limit:
            parcel.overweight = True
            self.err(lineno, "超重拦截",
                     "包裹 {} 重 {}kg，超过限重 {}kg，不进入分拣".format(pid, weight, self.limit))
        self.parcels[pid] = parcel

    # ---------- 指令：分拣 ----------
    def do_sort(self, lineno: int, args: List[str]) -> None:
        if len(args) != 2:
            self.err(lineno, "格式错误", "分拣指令需要 2 个参数: 包裹编号 格口名称")
            return
        pid, cname = args
        parcel = self.parcels.get(pid)
        if parcel is None:
            self.err(lineno, "引用不存在", "分拣引用了不存在的包裹 {}".format(pid))
            return
        chute = self.chutes.get(cname)
        if chute is None:
            self.err(lineno, "引用不存在", "分拣引用了不存在的格口 {}".format(cname))
            return
        if parcel.overweight:
            self.err(lineno, "超重拦截", "超重包裹 {} 不得进入格口 {}".format(pid, cname))
            return
        if chute.faulty:
            self.err(lineno, "故障拒收",
                     "格口 {} 故障期间不收新包裹，包裹 {} 保持待分拣".format(cname, pid))
            return
        if parcel.status != PENDING:
            self.err(lineno, "重复分拣",
                     "包裹 {} 当前状态为「{}」，不能再次分拣".format(pid, parcel.status))
            return
        if not chute.matches(parcel.address):
            # 错投撤回级联：不入格口、不计数，包裹保持待分拣，可再次正确分拣。
            self.err(lineno, "错投撤回",
                     "包裹 {} 地址「{}」与格口 {} 规则{}不匹配，已撤回".format(
                         pid, parcel.address, cname, chute.keywords))
            return
        if len(chute.packages) >= chute.capacity:
            self.err(lineno, "格口已满",
                     "格口 {} 已满({}/{})，包裹 {} 保持待分拣".format(
                         cname, len(chute.packages), chute.capacity, pid))
            return
        chute.packages.append(pid)
        parcel.status = SORTED
        parcel.chute = cname

    # ---------- 指令：转运 ----------
    def do_transfer(self, lineno: int, args: List[str]) -> None:
        if len(args) != 2:
            self.err(lineno, "格式错误", "转运指令需要 2 个参数: 格口名称 目的站")
            return
        cname, dest = args
        chute = self.chutes.get(cname)
        if chute is None:
            self.err(lineno, "引用不存在", "转运引用了不存在的格口 {}".format(cname))
            return
        if chute.faulty:
            self.err(lineno, "故障拒运", "格口 {} 处于故障状态，不能发运".format(cname))
            return
        # 级联 1：同一格口上一次发出的在途批次确认到达，转运中 -> 已转运。
        arrived = [p for p in self.parcels.values()
                   if p.status == IN_TRANSIT and p.chute == cname]
        for p in arrived:
            p.status = DONE
        # 级联 2：当前在口包裹整批发运，已分拣 -> 转运中，格口计数清零。
        batch = list(chute.packages)
        chute.packages.clear()
        for pid in batch:
            p = self.parcels[pid]
            p.status = IN_TRANSIT
            p.destination = dest
        if not batch and not arrived:
            self.err(lineno, "空转运", "格口 {} 无在口包裹可转运至 {}".format(cname, dest))

    # ---------- 指令：故障 ----------
    def do_fault(self, lineno: int, args: List[str]) -> None:
        if len(args) != 1:
            self.err(lineno, "格式错误", "故障指令需要 1 个参数: 格口名称")
            return
        cname = args[0]
        chute = self.chutes.get(cname)
        if chute is None:
            self.err(lineno, "引用不存在", "故障引用了不存在的格口 {}".format(cname))
            return
        if chute.faulty:
            self.err(lineno, "重复故障", "格口 {} 已处于故障状态".format(cname))
            return
        chute.faulty = True
        # 级联：口内未转运包裹沿备用链转移；转运中/已转运的不受影响（已离口）。
        stranded = list(chute.packages)
        chute.packages.clear()
        for pid in stranded:
            p = self.parcels[pid]
            target = self._find_fallback(cname)
            if target is None:
                p.status = PENDING
                p.chute = None
                self.err(lineno, "级联失败",
                         "包裹 {} 无可用备用格口，退回待分拣".format(pid))
            else:
                self.chutes[target].packages.append(pid)
                p.chute = target  # 状态保持「已分拣」，所在格口级联变更

    def _find_fallback(self, cname: str) -> Optional[str]:
        """沿备用格口链寻找第一个未故障且未满的格口；带成环与空引用保护。"""
        seen = {cname}
        cur = self.chutes[cname].fallback
        while cur is not None:
            if cur in seen:
                return None
            seen.add(cur)
            ch = self.chutes.get(cur)
            if ch is None:
                return None
            if not ch.faulty and len(ch.packages) < ch.capacity:
                return cur
            cur = ch.fallback
        return None

    # ---------- 主循环 ----------
    def _validate_fallbacks(self) -> None:
        for c in self.chutes.values():
            if c.fallback is not None and c.fallback not in self.chutes:
                self.err(c.def_line, "引用不存在",
                         "格口 {} 的备用格口 {} 未定义".format(c.name, c.fallback))

    def run(self, lines) -> None:
        handlers = {
            "限重": self.do_limit,
            "格口": self.do_chute,
            "包裹": self.do_parcel,
            "分拣": self.do_sort,
            "转运": self.do_transfer,
            "故障": self.do_fault,
        }
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            handler = handlers.get(parts[0])
            if handler is None:
                self.err(lineno, "未知指令", "无法识别: {}".format(parts[0]))
                continue
            handler(lineno, parts[1:])
        self._validate_fallbacks()

    # ---------- 输出 ----------
    def report(self, out) -> None:
        w = out.write
        w("====== 分拣转运状态 ======\n")
        w("--- 格口 ---\n")
        if not self.chutes:
            w("（无格口定义）\n")
        for c in self.chutes.values():
            w("格口 {} | 容量 {} | 在口 {}{} | 备用 {} | 关键词 {} | 包裹 [{}]\n".format(
                c.name, c.capacity, len(c.packages),
                " | 故障" if c.faulty else "",
                c.fallback or "无", ",".join(c.keywords), ", ".join(c.packages)))
        w("--- 包裹 ---\n")
        if not self.parcels:
            w("（无包裹登记）\n")
        for p in self.parcels.values():
            if p.status == SORTED:
                loc = "格口 {}".format(p.chute)
            elif p.status == IN_TRANSIT:
                loc = "由格口 {} 运往 {}".format(p.chute, p.destination)
            elif p.status == DONE:
                loc = "已送达 {}".format(p.destination)
            else:
                loc = "未入格口"
            w("包裹 {} | 地址 {} | {}kg{} | {} | {}\n".format(
                p.pid, p.address, p.weight,
                " | 超重" if p.overweight else "", p.status, loc))
        w("====== 错误清单（{} 条） ======\n".format(len(self.errors)))
        if not self.errors:
            w("（无错误）\n")
        for i, e in enumerate(self.errors, 1):
            w("{}. {}\n".format(i, e))


def main(argv: List[str]) -> int:
    sim = Simulator()
    if len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as f:
            sim.run(f)
    else:
        sim.run(sys.stdin)
    sim.report(sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
