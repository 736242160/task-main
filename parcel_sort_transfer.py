#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
包裹分拣-转运联动模拟工具（纯 Python 标准库，单文件）

用法:
    python3 parcel_sort_transfer.py 输入文件      # 处理指定输入文件
    python3 parcel_sort_transfer.py              # 从标准输入读取
    python3 parcel_sort_transfer.py --demo       # 运行内置演示

输入格式（按行处理，行序即事件序，跨流状态自然延续；# 开头为注释）:
    格口 <名称> <容量> <备用格口|->
    包裹 <编号> <收件地址...> <重量kg>     # 地址可含空格，重量为最后一个词
    分拣 <包裹编号> <格口名称>
    转运 <格口名称> <目的站>
    故障 <格口名称>
    恢复 <格口名称>

关键设计说明:
1. 地址关键词匹配: 格口名称即目的地关键词，收件地址中【包含】格口名才视为
   匹配；不匹配的分拣视为"错投"，执行撤回级联（包裹回到待分拣、格口计数不变）。
2. 超重规则: 重量 > 30kg 判定为超重。理由: 假定本分拣线为轻型标准件自动线，
   30kg 是行业常见的自动分拣设备单件上限，超重件需走人工/重货通道，故不进
   分拣并报告。
3. 包裹状态机: 待分拣 -> 已分拣 -> 转运中 -> 已转运。
   转运事件级联推进: 该格口上一批"转运中"包裹到站变为"已转运"；当前格口内
   "已分拣"包裹发出变为"转运中"，格口计数清零。
4. 容量联动: 格口装满后继续分拣会报告错误，包裹保持待分拣、计数不变。
5. 故障级联: 格口故障后，其内未转运包裹按序转移至备用格口（包裹保持已分拣，
   双方计数联动更新）；备用格口不存在/已故障/容量不足时包裹留在故障格口并报告。
   故障期间该格口不收新包裹（分拣请求报告错误），恢复后可正常收件。
6. 重复分拣（包裹非待分拣状态时再次分拣）与引用不存在的包裹/格口均会报告。
"""

import sys


MAX_WEIGHT_KG = 30.0  # 超重阈值（kg），见模块 docstring 设计说明 2

STATUS_WAITING = "待分拣"
STATUS_SORTED = "已分拣"
STATUS_IN_TRANSIT = "转运中"
STATUS_DELIVERED = "已转运"


class Chute:
    def __init__(self, name, capacity, backup):
        self.name = name
        self.capacity = capacity
        self.backup = backup          # 备用格口名或 None
        self.failed = False
        self.held = []                # 当前格口内已分拣包裹编号
        self.in_transit = []          # 已发出未到站的包裹编号

    @property
    def load(self):
        return len(self.held)


class Parcel:
    def __init__(self, pid, address, weight):
        self.pid = pid
        self.address = address
        self.weight = weight
        self.status = STATUS_WAITING
        self.chute = None             # 当前所在/发往格口
        self.destination = None       # 最近转运目的站


class Simulator:
    def __init__(self):
        self.chutes = {}
        self.parcels = {}
        self.errors = []              # (行号, 级别, 消息)

    # ---------- 报告 ----------
    def error(self, line_no, msg):
        self.errors.append((line_no, "错误", msg))

    def warn(self, line_no, msg):
        self.errors.append((line_no, "警告", msg))

    # ---------- 各流处理 ----------
    def define_chute(self, line_no, name, capacity, backup):
        if name in self.chutes:
            self.error(line_no, "格口「%s」重复定义" % name)
            return
        if capacity <= 0:
            self.error(line_no, "格口「%s」容量必须为正整数，得到 %d" % (name, capacity))
            return
        self.chutes[name] = Chute(name, capacity, backup)

    def define_parcel(self, line_no, pid, address, weight):
        if pid in self.parcels:
            self.error(line_no, "包裹「%s」重复定义" % pid)
            return
        if weight < 0:
            self.error(line_no, "包裹「%s」重量不能为负: %.2f" % (pid, weight))
            return
        self.parcels[pid] = Parcel(pid, address, weight)
        if weight > MAX_WEIGHT_KG:
            self.warn(line_no, "包裹「%s」超重 %.2fkg > %.0fkg，将不能进入分拣"
                      % (pid, weight, MAX_WEIGHT_KG))

    def sort_parcel(self, line_no, pid, chute_name):
        parcel = self.parcels.get(pid)
        chute = self.chutes.get(chute_name)
        if parcel is None:
            self.error(line_no, "分拣引用了不存在的包裹「%s」" % pid)
            return
        if chute is None:
            self.error(line_no, "分拣引用了不存在的格口「%s」" % chute_name)
            return
        if parcel.status != STATUS_WAITING:
            self.error(line_no, "包裹「%s」重复分拣：当前状态为「%s」，仅待分拣可分拣"
                       % (pid, parcel.status))
            return
        if parcel.weight > MAX_WEIGHT_KG:
            self.error(line_no, "包裹「%s」超重 %.2fkg > %.0fkg，不能进入分拣"
                       % (pid, parcel.weight, MAX_WEIGHT_KG))
            return
        if chute.failed:
            self.error(line_no, "格口「%s」故障期间不收新包裹，包裹「%s」分拣被拒绝"
                       % (chute_name, pid))
            return
        if chute_name not in parcel.address:
            # 错投撤回级联：不入格口、不计数，包裹回到（保持）待分拣
            parcel.status = STATUS_WAITING
            parcel.chute = None
            self.error(line_no,
                       "错投：包裹「%s」地址「%s」与格口「%s」规则不匹配，已撤回至待分拣"
                       % (pid, parcel.address, chute_name))
            return
        if chute.load >= chute.capacity:
            self.error(line_no, "格口「%s」已满（%d/%d），包裹「%s」继续分拣被拒绝，保持待分拣"
                       % (chute_name, chute.load, chute.capacity, pid))
            return
        chute.held.append(pid)
        parcel.status = STATUS_SORTED
        parcel.chute = chute_name

    def transfer(self, line_no, chute_name, station):
        chute = self.chutes.get(chute_name)
        if chute is None:
            self.error(line_no, "转运引用了不存在的格口「%s」" % chute_name)
            return
        if chute.failed:
            self.error(line_no, "格口「%s」故障中，无法执行转运至「%s」" % (chute_name, station))
            return
        # 级联 1：上一批转运中的包裹到站 -> 已转运
        for pid in chute.in_transit:
            parcel = self.parcels[pid]
            parcel.status = STATUS_DELIVERED
            parcel.chute = None
        arrived = len(chute.in_transit)
        chute.in_transit = []
        # 级联 2：当前格口内已分拣包裹发出 -> 转运中，格口计数清零
        for pid in chute.held:
            parcel = self.parcels[pid]
            parcel.status = STATUS_IN_TRANSIT
            parcel.destination = station
        departed = len(chute.held)
        chute.in_transit = list(chute.held)
        chute.held = []
        if arrived == 0 and departed == 0:
            self.warn(line_no, "格口「%s」空转运至「%s」：无包裹发出也无包裹到站"
                      % (chute_name, station))

    def fail_chute(self, line_no, chute_name):
        chute = self.chutes.get(chute_name)
        if chute is None:
            self.error(line_no, "故障引用了不存在的格口「%s」" % chute_name)
            return
        if chute.failed:
            self.error(line_no, "格口「%s」已处于故障状态，重复故障" % chute_name)
            return
        chute.failed = True
        # 级联转移：未转运包裹 -> 备用格口
        if not chute.held:
            return
        if not chute.backup:
            self.error(line_no, "格口「%s」故障且有 %d 件未转运包裹，但未配置备用格口，包裹滞留"
                       % (chute_name, len(chute.held)))
            return
        backup = self.chutes.get(chute.backup)
        if backup is None:
            self.error(line_no, "格口「%s」的备用格口「%s」不存在，%d 件包裹滞留"
                       % (chute_name, chute.backup, len(chute.held)))
            return
        if backup.failed:
            self.error(line_no, "备用格口「%s」同样故障，%d 件包裹滞留在「%s」"
                       % (backup.name, len(chute.held), chute_name))
            return
        remaining = []
        for pid in chute.held:
            if backup.load < backup.capacity:
                backup.held.append(pid)
                parcel = self.parcels[pid]
                parcel.chute = backup.name      # 状态保持已分拣，仅级联换格口
            else:
                remaining.append(pid)
        chute.held = remaining
        if remaining:
            self.error(line_no, "备用格口「%s」容量不足，%d 件包裹滞留在故障格口「%s」: %s"
                       % (backup.name, len(remaining), chute_name, "、".join(remaining)))

    def recover_chute(self, line_no, chute_name):
        chute = self.chutes.get(chute_name)
        if chute is None:
            self.error(line_no, "恢复引用了不存在的格口「%s」" % chute_name)
            return
        if not chute.failed:
            self.error(line_no, "格口「%s」并未故障，无需恢复" % chute_name)
            return
        chute.failed = False

    # ---------- 解析 ----------
    def process_line(self, line_no, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return
        tokens = line.split()
        cmd = tokens[0]
        args = tokens[1:]
        try:
            if cmd == "格口":
                if len(args) != 3:
                    raise ValueError("格口 需要 3 个参数: 名称 容量 备用格口")
                backup = None if args[2] in ("-", "无") else args[2]
                self.define_chute(line_no, args[0], int(args[1]), backup)
            elif cmd == "包裹":
                if len(args) < 3:
                    raise ValueError("包裹 需要至少 3 个参数: 编号 地址 重量")
                pid, address, weight = args[0], " ".join(args[1:-1]), float(args[-1])
                self.define_parcel(line_no, pid, address, weight)
            elif cmd == "分拣":
                if len(args) != 2:
                    raise ValueError("分拣 需要 2 个参数: 包裹 格口")
                self.sort_parcel(line_no, args[0], args[1])
            elif cmd == "转运":
                if len(args) != 2:
                    raise ValueError("转运 需要 2 个参数: 格口 目的站")
                self.transfer(line_no, args[0], args[1])
            elif cmd == "故障":
                if len(args) != 1:
                    raise ValueError("故障 需要 1 个参数: 格口")
                self.fail_chute(line_no, args[0])
            elif cmd == "恢复":
                if len(args) != 1:
                    raise ValueError("恢复 需要 1 个参数: 格口")
                self.recover_chute(line_no, args[0])
            else:
                self.error(line_no, "未知指令「%s」" % cmd)
        except ValueError as exc:
            self.error(line_no, "格式错误: %s" % exc)

    def run(self, text):
        for line_no, line in enumerate(text.splitlines(), 1):
            self.process_line(line_no, line)

    # ---------- 输出 ----------
    def report(self, out=sys.stdout):
        w = out.write
        w("=" * 60 + "\n分拣转运状态\n" + "=" * 60 + "\n")
        w("\n[格口状态]\n")
        if not self.chutes:
            w("  （无格口）\n")
        for name in self.chutes:
            c = self.chutes[name]
            flag = " [故障]" if c.failed else ""
            backup = c.backup or "无"
            held = "、".join(c.held) if c.held else "空"
            transit = "、".join(c.in_transit) if c.in_transit else "无"
            w("  %s: 计数 %d/%d 备用=%s%s\n" % (name, c.load, c.capacity, backup, flag))
            w("    格口内包裹: %s\n" % held)
            w("    转运中包裹: %s\n" % transit)
        w("\n[包裹状态]\n")
        if not self.parcels:
            w("  （无包裹）\n")
        for pid in self.parcels:
            p = self.parcels[pid]
            loc = ""
            if p.status == STATUS_SORTED:
                loc = " 所在格口=%s" % p.chute
            elif p.status == STATUS_IN_TRANSIT:
                loc = " 发往=%s（自格口%s）" % (p.destination, p.chute)
            elif p.status == STATUS_DELIVERED:
                loc = " 目的站=%s" % p.destination
            w("  %s: %s 地址=%s 重量=%.2fkg%s\n" % (pid, p.status, p.address, p.weight, loc))
        w("\n" + "=" * 60 + "\n错误与警告清单（共 %d 条）\n" % len(self.errors) + "=" * 60 + "\n")
        if not self.errors:
            w("  无错误。\n")
        for line_no, level, msg in self.errors:
            w("  [行%03d][%s] %s\n" % (line_no, level, msg))


DEMO_INPUT = """\
# 格口定义: 名称 容量 备用格口
格口 北京 2 华北备
格口 上海 2 华东备
格口 华北备 3 -
格口 华东备 1 -

# 包裹定义: 编号 地址 重量
包裹 P001 北京市海淀区中关村大街1号 5.0
包裹 P002 北京市朝阳区建国路88号 12.0
包裹 P003 北京市西城区西单北大街 3.0
包裹 P004 上海市浦东新区世纪大道100号 8.0
包裹 P005 上海市静安区南京西路 35.0
包裹 P006 广州市天河区体育西路 6.0

# 分拣流
分拣 P001 北京
分拣 P002 北京
分拣 P003 北京
分拣 P004 上海
分拣 P005 上海
分拣 P006 北京
分拣 P001 北京
分拣 P999 北京
分拣 P001 不存在口

# 转运流
转运 北京 北京枢纽站
转运 上海 上海枢纽站
转运 北京 北京枢纽站

# 故障流: 故障级联转移 + 故障期间拒收 + 备用格口容量不足
包裹 P008 上海市徐汇区漕溪北路 2.0
包裹 P009 上海市杨浦区五角场 7.0
分拣 P008 上海
分拣 P009 上海
故障 上海
分拣 P008 上海
包裹 P007 上海市虹口区四川北路 4.0
分拣 P007 上海
恢复 上海
分拣 P007 上海
"""


def main(argv):
    if len(argv) > 1 and argv[1] == "--demo":
        text = DEMO_INPUT
        print("【输入】")
        print(text)
    elif len(argv) > 1:
        with open(argv[1], "r", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    sim = Simulator()
    sim.run(text)
    sim.report()
    return 1 if any(level == "错误" for _, level, _ in sim.errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
