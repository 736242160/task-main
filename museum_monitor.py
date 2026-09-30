#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
museum_monitor.py — 博物馆展柜温湿度监测与保护联动工具（纯 Python 标准库，单文件）

用法:
    python3 museum_monitor.py 输入文件     # 从文件读取指令流
    python3 museum_monitor.py              # 从标准输入读取
    python3 museum_monitor.py --demo       # 运行内置示例

输入指令（每行一条，# 之后为注释）:
    藏品 <编号> <展柜> <温度上限> <湿度上限>   定义藏品及其阈值
    相邻 <展柜A> <展柜B>                       声明展柜相邻（用于保护级联）
    更换 <藏品编号> <新展柜>                   藏品更换展柜，阈值随之级联更新
    监测 <展柜> <温度> <湿度>                  一条监测数据
    轮次                                       结束当前轮，进入下一轮

自定规则（含理由）:
  1. 故障判定: 一轮内某“有藏品”的展柜无任何监测数据记缺失一次，
     连续 3 轮缺失判定监测点故障。理由: 传感器每轮必报，1~2 轮
     缺失多为网络抖动，连续 3 轮大概率是硬件故障。
  2. 读数冲突: 同一展柜同一轮内多条监测，与该轮首条相比温度差
     > 1.0℃ 或湿度差 > 5%RH 判为数值矛盾。理由: 同一展柜微环境
     在一个短轮次内不可能剧烈变化，超差即传感器或上报通道异常。
  3. 保护措施: 展柜出现任一超限即开启保护（恒温除湿机组），自下一
     读数起该柜温度 -2.0℃、湿度 -8%RH；每个“在保”相邻展柜额外
     带来温度 -0.5℃、湿度 -2%RH（多邻柜叠加，湿度钳制 0~100）。
     连续 2 条读数恢复正常后关闭保护。保护开关变化时全部展柜
     修正值级联重算。理由: 展柜间空气流通，邻柜受保护设备外溢影响。
  4. 阈值级联: 阈值挂在藏品上，藏品更换展柜后新展柜立即按该藏品
     阈值判定，原展柜不再为其判定。
  5. 状态延续: 保护开关、连续正常计数、连续缺失计数均跨轮保留。
"""
import sys
from dataclasses import dataclass, field

TEMP_CONFLICT_TOL = 1.0   # 同轮温度矛盾容差(℃)
HUM_CONFLICT_TOL = 5.0    # 同轮湿度矛盾容差(%RH)
FAULT_MISS_ROUNDS = 3     # 连续缺失多少轮判故障
PROTECT_TEMP_DELTA = -2.0 # 本柜保护温度修正
PROTECT_HUM_DELTA = -8.0  # 本柜保护湿度修正
NEIGHBOR_TEMP_DELTA = -0.5  # 每个在保邻柜温度修正
NEIGHBOR_HUM_DELTA = -2.0   # 每个在保邻柜湿度修正
PROTECT_OFF_STREAK = 2    # 连续正常多少条关闭保护


@dataclass
class Artifact:
    aid: str
    cabinet: str
    temp_limit: float
    hum_limit: float


@dataclass
class Cabinet:
    name: str
    artifacts: list = field(default_factory=list)
    protected: bool = False
    miss_rounds: int = 0
    fault_reported: bool = False
    normal_streak: int = 0


class Monitor:
    def __init__(self):
        self.artifacts = {}
        self.cabinets = {}
        self.adj = {}
        self.adj_declared = False
        self.round_no = 1
        self.round_readings = {}
        self.events = []
        self.errors = []

    def ensure_cabinet(self, name):
        if name not in self.cabinets:
            self.cabinets[name] = Cabinet(name)
            self.adj.setdefault(name, set())
        return self.cabinets[name]

    def log(self, msg):
        self.events.append(msg)

    def error(self, kind, msg):
        self.errors.append((kind, msg))
        self.log(f"[错误/{kind}] {msg}")

    def neighbors(self, name):
        if self.adj_declared:
            return sorted(n for n in self.adj.get(name, ()) if n in self.cabinets)
        try:
            num = int(name)
        except ValueError:
            return []
        return [str(num + d) for d in (-1, 1) if str(num + d) in self.cabinets]

    def adjustment(self, name):
        """该展柜当前 (温度修正, 湿度修正)，含本柜保护与邻柜级联。"""
        cab = self.cabinets[name]
        dt = PROTECT_TEMP_DELTA if cab.protected else 0.0
        dh = PROTECT_HUM_DELTA if cab.protected else 0.0
        for nb in self.neighbors(name):
            if self.cabinets[nb].protected:
                dt += NEIGHBOR_TEMP_DELTA
                dh += NEIGHBOR_HUM_DELTA
        return dt, dh

    # ---------------- 指令 ----------------
    def cmd_artifact(self, aid, cab_name, tlim, hlim):
        if aid in self.artifacts:
            self.error("定义重复", f"藏品 {aid} 重复定义，已忽略")
            return
        art = Artifact(aid, cab_name, tlim, hlim)
        self.artifacts[aid] = art
        self.ensure_cabinet(cab_name).artifacts.append(aid)
        self.log(f"[定义] 藏品 {aid} 入藏展柜 {cab_name}，阈值 温度≤{tlim}℃ 湿度≤{hlim}%RH")

    def cmd_adjacent(self, a, b):
        self.adj_declared = True
        self.ensure_cabinet(a)
        self.ensure_cabinet(b)
        self.adj[a].add(b)
        self.adj[b].add(a)
        self.log(f"[拓扑] 展柜 {a} 与 {b} 相邻")

    def cmd_move(self, aid, new_cab):
        art = self.artifacts.get(aid)
        if art is None:
            self.error("未知藏品", f"更换展柜失败：藏品 {aid} 不存在")
            return
        if new_cab not in self.cabinets:
            self.error("未知展柜", f"更换展柜失败：目标展柜 {new_cab} 不存在")
            return
        old = art.cabinet
        if old == new_cab:
            self.log(f"[迁移] 藏品 {aid} 本就在展柜 {new_cab}，无需更换")
            return
        self.cabinets[old].artifacts.remove(aid)
        self.cabinets[new_cab].artifacts.append(aid)
        art.cabinet = new_cab
        self.log(f"[迁移] 藏品 {aid} 由展柜 {old} 迁至 {new_cab}，阈值级联更新："
                 f"新展柜按 温度≤{art.temp_limit}℃ 湿度≤{art.hum_limit}%RH 判定")

    def cmd_reading(self, cab_name, temp, hum):
        cab = self.cabinets.get(cab_name)
        if cab is None:
            self.error("未知展柜", f"第{self.round_no}轮 监测引用不存在的展柜 {cab_name}"
                                   f"（{temp}℃/{hum}%RH），已丢弃")
            return
        prev = self.round_readings.setdefault(cab_name, [])
        if prev:
            t0, h0 = prev[0]
            if abs(temp - t0) > TEMP_CONFLICT_TOL or abs(hum - h0) > HUM_CONFLICT_TOL:
                self.error("读数冲突", f"第{self.round_no}轮 展柜 {cab_name} 同轮读数矛盾："
                                       f"首条 {t0}℃/{h0}%RH vs 本条 {temp}℃/{hum}%RH"
                                       f"（容差 {TEMP_CONFLICT_TOL}℃/{HUM_CONFLICT_TOL}%RH），"
                                       f"本条丢弃，不参与超限与保护判定")
                return
        prev.append((temp, hum))
        dt, dh = self.adjustment(cab_name)
        at = temp + dt
        ah = min(100.0, max(0.0, hum + dh))
        note = f"（保护修正 {dt:+}℃/{dh:+}%RH 后 {at:.1f}℃/{ah:.1f}%RH）" if (dt or dh) else ""
        exceeded = False
        for aid in cab.artifacts:
            art = self.artifacts[aid]
            et = at - art.temp_limit
            eh = ah - art.hum_limit
            if et > 0:
                exceeded = True
                self.error("温度超限", f"第{self.round_no}轮 展柜 {cab_name} 藏品 {aid} "
                                       f"温度超限 {et:.1f}℃（{at:.1f}℃ > 阈值 {art.temp_limit}℃）")
            if eh > 0:
                exceeded = True
                self.error("湿度超限", f"第{self.round_no}轮 展柜 {cab_name} 藏品 {aid} "
                                       f"湿度超限 {eh:.1f}%RH（{ah:.1f}%RH > 阈值 {art.hum_limit}%RH）")
        self.log(f"[监测] 第{self.round_no}轮 展柜 {cab_name} {temp}℃/{hum}%RH{note} "
                 f"→ {'超限' if exceeded else '正常'}")
        if exceeded:
            cab.normal_streak = 0
            if not cab.protected:
                cab.protected = True
                self.log(f"[保护] 展柜 {cab_name} 开启保护措施"
                         f"（修正 {PROTECT_TEMP_DELTA}℃/{PROTECT_HUM_DELTA}%RH），"
                         f"相邻展柜 {self.neighbors(cab_name) or '无'} 级联重算修正值")
        elif cab.protected:
            cab.normal_streak += 1
            if cab.normal_streak >= PROTECT_OFF_STREAK:
                cab.protected = False
                cab.normal_streak = 0
                self.log(f"[保护] 展柜 {cab_name} 连续 {PROTECT_OFF_STREAK} 条读数正常，"
                         f"关闭保护措施，相邻展柜级联重算")

    def cmd_round(self):
        self._finalize_round()
        self.round_no += 1

    def _finalize_round(self):
        for name, cab in sorted(self.cabinets.items()):
            if not cab.artifacts:
                continue
            if name not in self.round_readings:
                cab.miss_rounds += 1
                if cab.miss_rounds >= FAULT_MISS_ROUNDS and not cab.fault_reported:
                    cab.fault_reported = True
                    self.error("展柜故障", f"展柜 {name} 连续 {cab.miss_rounds} 轮无监测数据，"
                                           f"判定监测点故障（阈值 {FAULT_MISS_ROUNDS} 轮）")
            else:
                if cab.fault_reported:
                    self.log(f"[恢复] 展柜 {name} 监测点恢复数据上报")
                cab.miss_rounds = 0
                cab.fault_reported = False
        self.round_readings.clear()

    def finish(self):
        self._finalize_round()

    # ---------------- 输出 ----------------
    def report(self):
        out = ["=" * 64, "监测过程", "=" * 64]
        out.extend(self.events)
        out += ["", "=" * 64, f"错误清单（共 {len(self.errors)} 条）", "=" * 64]
        if not self.errors:
            out.append("（无错误）")
        for i, (kind, msg) in enumerate(self.errors, 1):
            out.append(f"{i:>3}. [{kind}] {msg}")
        return "\n".join(out)


def run(lines):
    mon = Monitor()
    for lineno, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "藏品" and len(args) == 4:
                mon.cmd_artifact(args[0], args[1], float(args[2]), float(args[3]))
            elif cmd == "相邻" and len(args) == 2:
                mon.cmd_adjacent(args[0], args[1])
            elif cmd == "更换" and len(args) == 2:
                mon.cmd_move(args[0], args[1])
            elif cmd == "监测" and len(args) == 3:
                mon.cmd_reading(args[0], float(args[1]), float(args[2]))
            elif cmd == "轮次" and not args:
                mon.cmd_round()
            else:
                mon.error("格式错误", f"第{lineno}行无法解析: {raw.strip()}")
        except ValueError:
            mon.error("格式错误", f"第{lineno}行数值非法: {raw.strip()}")
    mon.finish()
    return mon


DEMO_INPUT = """\
# ---- 藏品定义：编号 展柜 温度上限 湿度上限 ----
藏品 青铜鼎 A1 22 55
藏品 书画   A2 20 50
藏品 陶俑   A3 24 60
相邻 A1 A2
相邻 A2 A3

# ---- 第1轮：A2 温度超3℃、湿度超2% → 开启保护，A1/A3 级联 ----
监测 A1 21 50
监测 A2 23 52
监测 A3 22 55
轮次

# ---- 第2轮：A2 在保(-2/-8)，A1 受邻柜级联(-0.5/-2) ----
监测 A1 22.4 56     # 修正后 21.9/54 → 正常（无级联则会超限）
监测 A2 21.5 49     # 修正后 19.5/41 → 正常(1)
监测 A2 21.6 49.2   # 正常(2) → 关闭保护
监测 A3 23 58
轮次

# ---- 第3轮：同轮冲突 + 未知展柜 ----
监测 A1 21 50
监测 A1 23.5 50     # 与首条温差 2.5℃ > 1.0℃ → 冲突
监测 A9 20 50       # 展柜不存在
监测 A3 22 55
轮次

# ---- 藏品迁移：书画阈值(20/50)带到 A1 ----
更换 书画 A1

# ---- 第4轮：A1 按书画阈值判超限 → A1 开保护；A3 开始缺数据 ----
监测 A1 21.5 52
轮次

# ---- 第5轮：A1 在保，读数正常(1)；A3 连续缺失2轮 ----
监测 A1 20 50
轮次

# ---- 第6轮：A1 正常(2)→关保护；A3 连续缺失3轮 → 故障 ----
监测 A1 19 48
"""


def main(argv):
    args = [a for a in argv[1:] if a != "--demo"]
    if "--demo" in argv:
        lines = DEMO_INPUT.splitlines()
    elif args:
        with open(args[0], encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()
    print(run(lines).report())


if __name__ == "__main__":
    main(sys.argv)
