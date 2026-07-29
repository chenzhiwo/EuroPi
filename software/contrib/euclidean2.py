# Copyright 2024 Allen Synthesis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Euclidean2 - 6-channel dual Euclidean sequencer (Digitakt 2 style dual machine)
================================================================================

六路独立 CV/GATE 输出，每路绑定一组双欧几里得发生器 (Gen1 + Gen2)，两路通过合并
逻辑 (OR / AND / XOR / GEN1 / GEN2) 实时得到该通道最终门信号。"通道 CV 输出电平"
即门的触发高电压 (0~10V，默认 5V)。全局时钟统一驱动全部 6 路。

控制：
  B1            短按：上一页    长按(>500ms)：保存状态（唯一触发 save 的途径）
  B2            短按：下一页    长按(>500ms)：回到全局时钟页 (P0)
  同时按住 B1+B2 0.5s：返回菜单（系统行为，本脚本不拦截）
  K1            参数选择（跳转策略，带迟滞）
  K2            参数数值调节（拾取策略：旋钮位置匹配当前值后才生效）

时钟：
  全局页可切换 内部时钟(INT, 可调 BPM 与倍率 mul) / 外部时钟(EXT, DIN 上升沿驱动)
  实际时钟速率 = BPM x mul（如 BPM=120, mul=4 → 480 BPM 实际节拍）

页面：
  P0 全局时钟(K1 切换: 时钟源 / BPM / 倍率mul / 输出电压)  ->  P1..P6 各通道编辑页  ->  循环
  每通道 9 项参数（K1 遍历）：
    ROT1 Gen1旋转  ROT2 Gen2旋转  PLS1 Gen1脉冲  PLS2 Gen2脉冲
    STP1 Gen1步数  STP2 Gen2步数  PRB1 Gen1概率  PRB2 Gen2概率
    MERG 合并模式
  （输出电压为全局设置项，于 P0 调节，默认 5V，作用全部 6 路）

UI（128x32）：
  行0 状态栏  P{page} {缩写}:{值}（全局页 P0 仅显示此行）
  行1 Gen1 序列（实心=触发，点=空步），当前步固定在最左(col0)
  行2 Gen2 序列，同左锚定前瞻滚动窗（最多 16 步，超出按播放头左滚）

--------------------------------------------------------------------------------
重构后的分层架构（依据 EuroPi_Sequencer_Refactoring_Plan.md 的 6 个步骤）
--------------------------------------------------------------------------------
本文件刻意保持为**单个脚本**，但严格按重构计划分层。各层只通过「语义事件 /
命令 / 模型读取 / 输出事件」交互，不直接互相改状态：

    EuroPi 硬件
        │
        ▼
    Hardware          ← 唯一允许调用 EuroPi API 的模块（硬件抽象 + 输入采样源）
        │                · 采样层：仅按键去抖（轮询态式）产出稳定值
        │                · 持有 DIN ISR 与时钟事件队列（_clock_q）
        │                · 输出落点：将输出事件 apply 到物理 CV/Gate
        ▼
    InputManager      ← 稳定采样 → 语义输入事件（边沿/长按时序）
                          [KnobTurn, ButtonEvent, ClockEvent]
        │
        ▼
    Controller         ← Euclidean2：事件分发 + 命令派发（_exec 统一出口）+ 主循环
        ├─→ UI Pages   ← 把交互译为命令（产出 Command，不直改模型）
        ├─→ AppState   ← 仅 UI 状态（当前页 / 选中项 / K2 拾取）
        ├─→ Sequencer  ← 拥有演奏：接收 ClockTick → 分发给各 TrackPlayer
        │      │          → 产出「输出事件」(硬件无关)
        │      ├─ EuclidPattern  ← Pattern 接口实现：单个发生器的音乐内容 (what)
        │      ├─ TrackSettings  ← 播放配置 (how)：merge（+ 预留 length/div/dir…）
        │      └─ TrackPlayer    ← 运行时播放头/相位 (where)
        └─→ Transport   ← 拥有全局时间：BPM / 时钟源 / 运行状态 (when)
        │
        ▼
    Renderer           ← 无状态渲染：经 ViewModel 读 AppState + Sequencer/Transport，
                          再经 Hardware 绘制（不修改、不持有持久 UI 数据）

关键约束（来自重构计划）：
  * Sequencer / Pattern / Track / Transport 绝不出现 oled/k1/cv1 等 EuroPi 调用；
    它们只产生语义输出事件（GateOutputEvent / CVOutputEvent / ClockOutputEvent），
    由 Controller 经 Hardware 适配器 apply 到物理输出。
  * Renderer 是无状态的：只读模型、经 ViewModel 取值、不修改状态、不持有持久数据。
  * 单一事实来源：Pattern 是音乐数据唯一来源，Transport 是时间唯一来源，
    AppState 是 UI 状态唯一来源；不跨层复制信息。
  * 换平台只需重写 Hardware 适配器，其余层保持不变。
"""

try:
    # Local development
    from software.firmware.europi import (
        OLED_WIDTH,
        OLED_HEIGHT,
        CHAR_HEIGHT,
        din,
        k1,
        k2,
        oled,
        b1,
        b2,
        cv1,
        cv2,
        cv3,
        cv4,
        cv5,
        cv6,
        cvs,
        HIGH,
        turn_off_all_cvs,
    )
    from software.firmware.europi_script import EuroPiScript
except ImportError:
    # Device import path
    from europi import *
    from europi_script import EuroPiScript

try:
    from experimental.euclid import generate_euclidean_pattern
except ImportError:
    from firmware.experimental.euclid import generate_euclidean_pattern

import random
import time
import micropython
from utime import ticks_diff, ticks_ms, ticks_us, ticks_add


# 状态持久化总开关：True 时允许保存。保存仅在用户长按 K1 时显式触发
# （见 dispatch 的 B1 long 分支），不在主循环里自动写盘，避免阻塞式写
# flash 的 25~60ms 卡顿拖慢节拍。
SAVE_STATES = True


# ---------------------------------------------------------------------------
# 性能分析（全局开关 + 统计器）
# ---------------------------------------------------------------------------
PROFILE = False  # 性能分析总开关：置 False 即完全禁用（不计时、不打印、零开销）

class _NullProfiler:
    """PROFILE=False 时的空实现：相同接口但全部空操作，运行期开销可忽略。"""
    class _NullSection:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def section(self, name): return self._NullSection()
    def record(self, name, dt_us): pass
    def maybe_print(self, now_us): pass
    def print_stats(self): pass


class _ProfSection:
    """`with PROFILER.section("name"):` 测量该代码块耗时（us），退出时记录。"""
    __slots__ = ("_p", "_name", "_t")
    def __init__(self, p, name):
        self._p = p
        self._name = name
        self._t = ticks_us()
    def __enter__(self): return self
    def __exit__(self, *exc):
        self._p.record(self._name, ticks_diff(ticks_us(), self._t))
        return False


class Profiler:
    """关键路径耗时统计器（MicroPython 友好，基于 utime.ticks_us）。

    按 name 区分累计每类代码块：调用次数、总耗时、当前此次耗时、历史最大耗时；
    每 1 秒经串口打印：当前此次 / 平均 / 历史最大。生命周期由全局 PROFILE 开关控制。
    """
    def __init__(self):
        self._stats = {}        # name -> [count, total_us, last_us, max_us]
        self._last_print_us = 0
        self._start_us = ticks_us()
        self._warmed = False    # 启动后前 1s 为预热期，样本不计入统计

    def section(self, name):
        return _ProfSection(self, name)

    def record(self, name, dt_us):
        s = self._stats.get(name)
        if s is None:
            # [last_us, max_us(历史), win_count, win_total]
            self._stats[name] = [dt_us, dt_us, 1, dt_us]
        else:
            s[0] = dt_us          # 当前此次耗时
            if dt_us > s[1]:
                s[1] = dt_us      # 历史最大耗时（跨 1s 窗口保留）
            s[2] += 1             # 过去 1s 窗口内次数
            s[3] += dt_us         # 过去 1s 窗口内总耗时

    def maybe_print(self, now_us):
        # 启动后前 1s 为预热期：丢弃其样本，避免初始误差污染统计
        if not self._warmed:
            if now_us - self._start_us >= 1_000_000:
                self._warmed = True
                self._stats = {}          # 丢弃预热期累计
                self._last_print_us = now_us
            return
        # 预热结束后每 ~1s 输出一次（用 ticks 差值，自动处理 71 分钟回绕）
        if now_us - self._last_print_us >= 1_000_000:
            self._last_print_us = now_us
            self.print_stats()
            # 重置 1s 窗口累计（max 保留），下一窗口重新统计平均值
            for s in self._stats.values():
                s[2] = 0
                s[3] = 0

    def print_stats(self):
        if not self._stats:
            return
        parts = []
        for name, s in self._stats.items():
            last, mx, n, tot = s
            avg = (tot / n) / 1000 if n else 0  # us -> ms，过去 1s 窗口均值
            parts.append("%s cur=%.2f avg=%.2f max=%.2f ms (n=%d)"
                         % (name, last / 1000, avg, mx / 1000, n))
        print("PROF:", " | ".join(parts))


PROFILER = Profiler() if PROFILE else _NullProfiler()


# ---------------------------------------------------------------------------
# 配置与纯函数（与硬件无关）
# ---------------------------------------------------------------------------

# 合并模式（对齐 Digitakt 2 双机器）
MERGE_MODES = ["OR", "AND", "XOR", "G1", "G2"]
MERGE_OR, MERGE_AND, MERGE_XOR, MERGE_G1, MERGE_G2 = range(5)


def combine_outputs(on1, on2, mode):
    """合并两路触发，得到该通道本步 ON/OFF（纯函数，无副作用）。"""
    if mode == MERGE_OR:
        return on1 or on2
    if mode == MERGE_AND:
        return on1 and on2
    if mode == MERGE_XOR:
        return on1 ^ on2
    if mode == MERGE_G1:
        return on1
    return on2


# 通道参数顺序（K1 遍历），(缩写, 内部kind)，缩写统一 4 字符便于顶栏阅读
# —— 全局常量（取值范围，供 CH_PARAMS / GLOBAL_PARAMS 等表驱动引用）——
MIN_BPM = 20
MAX_BPM = 240
MIN_MUL = 1
MAX_MUL = 8  # 时钟倍率（实际时钟 = BPM x mul）；上限 8 以保证每拍间隔 (>31ms) 留出刷新窗口
MAX_STEPS = 64  # 单发生器序列最大步数
MIN_STEPS = 1   # 单发生器序列最小步数
GATE_MS = 5  # 时钟事件后统一拉低输出的延迟（ms）

CH_PARAMS = [
    ("ROT1", "rot1"),
    ("ROT2", "rot2"),
    ("PLS1", "pulses1"),
    ("PLS2", "pulses2"),
    ("STP1", "steps1"),
    ("STP2", "steps2"),
    ("PRB1", "prob1"),
    ("PRB2", "prob2"),
    ("MERG", "merge"),
]

# 全局页参数表（与 CH_PARAMS 对称驱动）：
#   (缩写, pmin, pmax, discrete?, get_cur(transport)->当前值,
#    make_cmd(transport, v)->Command, fmt(transport)->顶栏显示值)
# 时钟源作为离散 0/1 参数纳入同一拾取/编辑流程，不特判；所有参数任何时候都可编辑。
GLOBAL_PARAMS = [
    ("CLK", 0, 1, True,
     lambda t: 0 if t.source == "INT" else 1,
     lambda t, v: SetTransportSource(t, "EXT" if v else "INT"),
     lambda t: t.source),
    ("BPM", MIN_BPM, MAX_BPM, False,
     lambda t: t.bpm,
     lambda t, v: SetBpm(t, v),
     lambda t: t.bpm),
    ("MUL", MIN_MUL, MAX_MUL, False,
     lambda t: t.mul,
     lambda t, v: SetMul(t, v),
     lambda t: t.mul),
    ("LVL", 0, 10, False,
     lambda t: t.level,
     lambda t, v: SetLevel(t, v),
     lambda t: f"{t.level}V"),
]

NUM_PAGES = 7  # 0 全局 + 1..6 通道

T_SHOW_US = 20_000  # 屏幕刷新预留窗口（微秒）；距离下次时钟事件不足该值时跳过刷新
DEBOUNCE_MS = 30       # 按键去抖窗口（ms）：raw 变化后须连续稳定达此宽才认定状态变更
KNOB_DEADBAND = 0.01  # 旋钮事件阈值（1%）：InputManager 据此判定旋钮变化是否足以产生 KnobTurn 事件（去除 Hardware 内增量保持后，由事件层承担抑抖/降事件率）


# ---------------------------------------------------------------------------
# 语义输入事件（InputManager.poll() → Controller.dispatch 的通信载体）
# ---------------------------------------------------------------------------

class KnobTurn:
    """K1/K2 当前位置事件；复用单例对象避免每轮分配。"""
    __slots__ = ("knob", "value")

    def __init__(self, knob, value):
        self.knob = knob
        self.value = value


class ButtonEvent:
    """按钮语义事件：button ∈ {"B1","B2"}，kind ∈ {"press","long"}。"""
    __slots__ = ("button", "kind")

    def __init__(self, button, kind):
        self.button = button
        self.kind = kind


class ClockEvent:
    """外部时钟 tick 事件（DIN 上升沿）。
    由 Hardware 的时钟队列流出，经 dispatch 驱动 Transport.ext_tick（仅 EXT 源生效）。"""
    __slots__ = ()


# ---------------------------------------------------------------------------
# 语义输出事件（Sequencer Core → Controller → Hardware 的通信载体，硬件无关）
# ---------------------------------------------------------------------------

class OutputEvent:
    """输出事件基类：Sequencer Core 在每次推进时产出的语义输出。
    不含任何硬件调用；由 Controller 经 Hardware 适配器 apply 到物理输出。"""
    pass


class CVOutputEvent(OutputEvent):
    """某 CV 通道置为指定电压（硬件无关；电平由 Sequencer 计算后携带）。"""
    __slots__ = ("channel", "voltage")

    def __init__(self, channel, voltage):
        self.channel = channel
        self.voltage = voltage


class GateOutputEvent(OutputEvent):
    """某 Gate/CV 通道的触发状态（True=触发高电平，False=拉低）。
    本脚本中每通道的 CV 即门，故主要使用此事件；高电平电压由 Hardware 适配器的
    gate_level 决定（与全局输出电压同步）。"""
    __slots__ = ("channel", "state")

    def __init__(self, channel, state):
        self.channel = channel
        self.state = state


class ClockOutputEvent(OutputEvent):
    """时钟输出脉冲事件（框架词汇；当前脚本无独立时钟输出引脚，故不主动产生）。"""
    __slots__ = ()


# ---------------------------------------------------------------------------
# 命令（Controller → Model 的通信载体，与输入侧事件对称）
# 所有模型变更经 Controller._exec(cmd) 统一执行：执行即改模型 + 置 dirty。
# 命令只携带「意图」（目标 + 值），不直接耦合 Controller；便于单测/录制。
# ---------------------------------------------------------------------------

class Command:
    """命令基类：所有命令实现 execute() 完成对模型的变更。"""
    def execute(self):
        raise NotImplementedError


class SetTransportSource(Command):
    def __init__(self, transport, src):
        self._t, self._v = transport, src

    def execute(self):
        self._t.set_source(self._v)


class SetBpm(Command):
    def __init__(self, transport, bpm):
        self._t, self._v = transport, bpm

    def execute(self):
        self._t.set_bpm(self._v)


class SetMul(Command):
    def __init__(self, transport, mul):
        self._t, self._v = transport, mul

    def execute(self):
        self._t.set_mul(self._v)


class SetLevel(Command):
    def __init__(self, transport, level):
        self._t, self._v = transport, level

    def execute(self):
        self._t.set_level(self._v)


class SetTrackParam(Command):
    """通道发生器参数（steps/pulses/rot/prob）变更。"""
    def __init__(self, track, which, attr, val):
        self._t, self._w, self._a, self._v = track, which, attr, val

    def execute(self):
        self._t.set_gen(self._w, self._a, self._v)


class SetMerge(Command):
    """通道合并模式变更。"""
    def __init__(self, track, val):
        self._t, self._v = track, val

    def execute(self):
        self._t.set_merge(self._v)


# ---------------------------------------------------------------------------
# Hardware Adapter —— 唯一允许访问 EuroPi 硬件 API 的模块（硬件抽象 + 输入事件源）
# ---------------------------------------------------------------------------

class Hardware:
    """Hardware Adapter：集中所有 oled/k1/k2/b1/b2/cv*/din 调用。
    其余模块只与本适配器交互，永不直接调用 EuroPi。换平台只改这里。"""

    def __init__(self):
        self.oled = oled
        self.k1 = k1
        self.k2 = k2
        self.b1 = b1
        self.b2 = b2
        self.cv = [cv1, cv2, cv3, cv4, cv5, cv6]
        self.din = din

        # 门触发高电平（与全局输出电压同步；Setter 在每次节拍/改电平时刷新）
        self._gate_level = 5

        # —— 输入去抖状态（采样层：只在 Hardware 内，事件层只看到稳定值）——
        # 按键：经典弹跳滤波——raw 变化后须连续 DEBOUNCE_MS 稳定才更新 state
        self._btn = {
            "b1": {"state": False, "pending": False, "t": 0},
            "b2": {"state": False, "pending": False, "t": 0},
        }

        # 外部时钟事件队列（ISR 经 micropython.schedule 推入，由 InputManager 取走）
        self._clock_q = []

        # 注册 DIN 上升沿 → 往时钟队列推 ClockEvent（外部时钟源时由 dispatch 驱动）
        self.on_clock_rise(self._push_clock)

    # --- 输入采样（去抖层：产出稳定值）---
    def _debounced(self, key, raw):
        d = self._btn[key]
        now = ticks_ms()
        if raw != d["state"]:
            if raw != d["pending"]:
                d["pending"] = raw
                d["t"] = now
            elif ticks_diff(now, d["t"]) >= DEBOUNCE_MS:
                d["state"] = raw
        else:
            d["pending"] = raw
        return d["state"]

    def knob1(self):
        # 旋钮消抖已移至 InputManager 事件层（按 KNOB_DEADBAND 阈值决定是否发事件）；
        # 这里只透传 europi 的稳定采样（percent 自带过采样 + 量程死区），不做额外滤波。
        return self.k1.percent()

    def knob2(self):
        return self.k2.percent()

    def button1(self):
        return self._debounced("b1", self.b1.value() == HIGH)

    def button2(self):
        return self._debounced("b2", self.b2.value() == HIGH)

    def _push_clock(self, _=None):
        # 由 DIN ISR 经 micropython.schedule 调用（已脱离中断上下文），安全入队
        self._clock_q.append(ClockEvent())

    def take_clock_events(self):
        """取出并清空 ISR 推入的 ClockEvent 列表（由 InputManager 调用，并入事件流）。"""
        if not self._clock_q:
            return []
        q = self._clock_q
        self._clock_q = []
        return q

    def on_clock_rise(self, cb):
        # 直接注册到 DIN 原始引脚，绕开 europi 的 _bounce_wrapper：
        #   · 设备上 _bounce_wrapper 对 handler 的调用签名与本项目不符，会直接抛
        #     TypeError（见运行期崩溃），导致外部时钟根本无法生效；
        #   · 同时避开 europi 自带的 debounce（din 默认 0，但固件版本差异可能吞掉
        #     高速时钟脉冲）。
        # 触发边沿用 IRQ_FALLING：europi 的 value() 已反相，物理下降沿对应“逻辑高”
        # 的起始，与原 din.handler(上升沿回调) 的相位一致。
        pin = self.din.pin

        def _isr(*_):
            try:
                micropython.schedule(cb, None)
            except (ValueError, RuntimeError):
                pass  # 调度队列满（正常时钟速率下不会发生），丢弃本次 tick

        pin.irq(trigger=pin.IRQ_FALLING, handler=_isr)

    # --- CV / Gate 输出（输出事件的落点）---
    def set_gate_level(self, v):
        """设置门触发高电平（全局输出电压）；后续 GateOutputEvent(True) 据此置 CV。"""
        self._gate_level = v

    def set_gate(self, idx, state):
        """将某个通道的 Gate（在本脚本中即 CV 门）置为触发/拉低。"""
        if state:
            self.cv[idx].voltage(self._gate_level)
        else:
            self.cv[idx].off()

    def set_cv(self, idx, voltage):
        self.cv[idx].voltage(voltage)

    def off_cv(self, idx):
        self.cv[idx].off()

    def off_all_cvs(self):
        turn_off_all_cvs()

    # --- 显示（Renderer 经此绘制，不直接碰 oled）---
    def display_clear(self):
        self.oled.fill(0)

    def display_show(self):
        self.oled.show()

    def display_text(self, s, x, y):
        self.oled.text(s, x, y, 1)

    def display_fill_rect(self, x, y, w, h, c):
        self.oled.fill_rect(x, y, w, h, c)


# ---------------------------------------------------------------------------
# Input Manager —— 硬件稳定采样 → 语义事件（独立于 Hardware 的输入解释层）
# ---------------------------------------------------------------------------

class InputManager:
    """把 Hardware 的稳定采样转化为语义事件（KnobTurn / ButtonEvent / ClockEvent）。
    不修改任何应用状态；按钮做短按/长按边沿判定。完全独立于平台（只依赖 Hardware
    接口），可单独单元测试。"""

    def __init__(self, hw):
        self.hw = hw
        self._b1_down = False
        self._b2_down = False
        self._b1_press_t = 0
        self._b2_press_t = 0
        self._b1_long = False
        self._b2_long = False
        # 复用单例事件对象，避免每轮分配
        self._knob1 = KnobTurn(1, 0.0)
        self._knob2 = KnobTurn(2, 0.0)
        # 旋钮事件驱动：仅当稳定采样值变化（>=1% 死区）时才产生事件
        self._knob1_last = -1.0  # -1 强制首轮发出初始状态
        self._knob2_last = -1.0

    def poll(self):
        """统一事件入口：返回本轮所有输入事件 [KnobTurn, ButtonEvent, ClockEvent]。
        旋钮/按键去抖在 Hardware 采样层完成；这里的边沿/长按时序属“事件层”语义。"""
        events = []
        now = ticks_ms()
        d1 = self.hw.button1()
        d2 = self.hw.button2()

        if d1 and d2:
            # 双按 = 返回菜单（交由系统处理），不产出事件，并重置边沿状态
            self._b1_down = d1
            self._b2_down = d2
            return events

        # B1 边沿 + 长按判定
        if d1 and not self._b1_down:
            self._b1_press_t = now
            self._b1_long = False
        if d1 and not self._b1_long and ticks_diff(now, self._b1_press_t) >= 500:
            self._b1_long = True
            events.append(ButtonEvent("B1", "long"))
        if not d1 and self._b1_down:
            if not self._b1_long:
                events.append(ButtonEvent("B1", "press"))
        self._b1_down = d1

        # B2 边沿 + 长按判定
        if d2 and not self._b2_down:
            self._b2_press_t = now
            self._b2_long = False
        if d2 and not self._b2_long and ticks_diff(now, self._b2_press_t) >= 500:
            self._b2_long = True
            events.append(ButtonEvent("B2", "long"))
        if not d2 and self._b2_down:
            if not self._b2_long:
                events.append(ButtonEvent("B2", "press"))
        self._b2_down = d2

        # 旋钮：事件驱动，仅当变化 >= KNOB_DEADBAND（1%）才发出事件（抑抖/降事件率由此阈值承担）
        v1 = self.hw.knob1()
        if abs(v1 - self._knob1_last) >= KNOB_DEADBAND:
            self._knob1_last = v1
            self._knob1.value = v1
            events.append(self._knob1)
        v2 = self.hw.knob2()
        if abs(v2 - self._knob2_last) >= KNOB_DEADBAND:
            self._knob2_last = v2
            self._knob2.value = v2
            events.append(self._knob2)

        # 外部时钟：取走 ISR 推入的 ClockEvent，并入同一事件流
        for c in self.hw.take_clock_events():
            events.append(c)
        return events


# ---------------------------------------------------------------------------
# Sequencer Core —— Pattern / TrackSettings / TrackPlayer / Sequencer
# （不含任何硬件调用；产出语义输出事件）
# ---------------------------------------------------------------------------

class Pattern:
    """Pattern 接口（重构计划步骤 4）：单个发生器的音乐内容（what to play）。
    具体算法（如欧几里得）实现本接口。所有 Sequencer 只依赖此接口，
    不依赖具体算法——更换/新增算法只需新增 Pattern 子类。"""

    steps = 1
    pattern = []

    def regenerate(self):
        """根据 steps/pulses/rot 等参数重新生成 pattern[]。"""
        raise NotImplementedError

    def output(self, pos):
        """返回播放头位于 pos 时该步是否应触发（已含概率等「音乐」判定）。"""
        raise NotImplementedError

    def to_dict(self):
        raise NotImplementedError

    def from_dict(self, d):
        raise NotImplementedError


class EuclidPattern(Pattern):
    """EuclidPattern：欧几里得发生器的音乐内容（what to play）。
    不含运行时播放头（pos 由 TrackPlayer 持有）。概率作为「音乐数据」在此判定，
    不影响存储序列。"""

    def __init__(self, steps, pulses, rot, prob):
        self.steps = steps
        self.pulses = pulses
        self.rot = rot
        self.prob = prob
        self.pattern = []
        self.regenerate()

    def regenerate(self):
        self.pattern = generate_euclidean_pattern(self.steps, self.pulses, self.rot)

    def output(self, pos):
        """该播放头步是否应触发（pattern 且通过概率保留判定）。"""
        if not self.pattern[pos]:
            return False
        if self.prob >= 100:
            return True
        if self.prob <= 0:
            return False
        return random.random() < self.prob / 100.0

    def set_steps(self, steps):
        self.steps = min(max(steps, MIN_STEPS), MAX_STEPS)
        if self.pulses > self.steps:
            self.pulses = steps
        if self.rot > steps:
            self.rot = steps
        self.regenerate()

    def set_pulses(self, pulses):
        self.pulses = min(max(pulses, 0), self.steps)
        self.regenerate()

    def set_rot(self, rot):
        self.rot = min(max(rot, 0), self.steps)
        self.regenerate()

    def set_prob(self, prob):
        # 概率不影响存储序列，仅触发时判定，无需 regenerate
        self.prob = min(max(prob, 0), 100)

    # —— 序列化（模型自管，Controller 只做编排）——
    def to_dict(self):
        return {"steps": self.steps, "pulses": self.pulses,
                "rot": self.rot, "prob": self.prob}

    def from_dict(self, d):
        if not d:
            return
        # 直接赋值（与原始 set_state 行为一致，不在此 clamp，交由上层保证存储合法）
        self.steps = d.get("steps", self.steps)
        self.pulses = d.get("pulses", self.pulses)
        self.rot = d.get("rot", self.rot)
        self.prob = d.get("prob", self.prob)
        self.regenerate()  # 统一重算一次，消除「哪些字段需 regenerate」的隐式知识


class TrackSettings:
    """TrackSettings（重构计划步骤 3）：播放配置（how should it be played）。
    当前持有 merge（合并模式）。预留 length / clock_division / direction /
    swing / mute / transpose 等通用字段位（按计划语义），本脚本未全部启用以免
    改变既有行为；统一在此自管序列化。"""

    def __init__(self, merge=MERGE_OR):
        self.merge = merge

    # —— 序列化（模型自管，Track 只做编排）——
    def to_dict(self):
        return {"merge": self.merge}

    def from_dict(self, d):
        if not d:
            return
        self.merge = d.get("merge", self.merge)


class TrackPlayer:
    """TrackPlayer（重构计划步骤 3）：运行时状态（where are we playing）。
    持有每路发生器的播放头与当前步触发状态；推进时按合并+概率计算 ON/OFF。
    不含任何音乐内容（那在 Pattern）也不含播放配置（那在 TrackSettings）。"""

    def __init__(self, patterns, settings):
        self.patterns = patterns
        self.settings = settings
        self.positions = [p.steps - 1 for p in patterns]  # 首次 advance 后落于 0
        self.last_out = False  # 当前步（最近一次时钟事件）的触发状态

    def output(self):
        """推进一拍：各播放头前进，按合并+概率计算 ON/OFF。
        返回该通道本拍是否触发（True/False）。"""
        ons = []
        for i, p in enumerate(self.patterns):
            self.positions[i] = (self.positions[i] + 1) % p.steps
            ons.append(p.output(self.positions[i]))
        out = combine_outputs(ons[0], ons[1], self.settings.merge)
        self.last_out = out  # 记录当前步触发状态，供屏幕底部唱头行显示
        return out

    # —— 序列化（运行时状态；通常不持久化，但自管以便可选保存）——
    def to_dict(self):
        return {"positions": list(self.positions), "last_out": self.last_out}

    def from_dict(self, d):
        if not d:
            return
        pos = d.get("positions")
        if pos:
            self.positions = pos
        self.last_out = d.get("last_out", self.last_out)


class Track:
    """Track = Pattern × 2 + TrackSettings + TrackPlayer（一个通道，重构计划步骤 3）。
    - Pattern:      g1 / g2 的欧几里得序列（steps/pulses/rot/prob + pattern[]）
    - TrackSettings: merge（输出电压为全局项，见 Transport.level）
    - TrackPlayer:  g1_pos / g2_pos 播放头（输出由 g1/g2 合并直接计算，无移位寄存器）
    为兼容既有渲染/页面代码，暴露 g1 / g2 / merge / last_out 便捷访问。
    """

    def __init__(self, cv_index, g1, g2, merge):
        self.cv_index = cv_index
        self.patterns = [g1, g2]            # 音乐内容（双机器）
        self.settings = TrackSettings(merge) # 播放配置
        self.player = TrackPlayer(self.patterns, self.settings)  # 运行时

    # —— 便捷访问（保持渲染/页面对原字段名的引用）——
    @property
    def g1(self):
        return self.patterns[0]

    @property
    def g2(self):
        return self.patterns[1]

    @property
    def merge(self):
        return self.settings.merge

    @merge.setter
    def merge(self, v):
        self.settings.merge = v

    @property
    def last_out(self):
        return self.player.last_out

    # —— TrackSettings 命令（由 UI 页译为命令后调用）——
    def set_gen(self, which, attr, val):
        g = self.g1 if which == 1 else self.g2
        if attr == "steps":
            g.set_steps(val)
        elif attr == "pulses":
            g.set_pulses(val)
        elif attr == "rot":
            g.set_rot(val)
        elif attr == "prob":
            g.set_prob(val)

    def set_merge(self, val):
        self.settings.merge = val

    # —— TrackPlayer 运行时（Sequencer 经此推进）——
    def output(self):
        return self.player.output()

    # —— 序列化（模型自管，Controller 只做编排）——
    def to_dict(self):
        return {"g1": self.g1.to_dict(), "g2": self.g2.to_dict(),
                "merge": self.settings.merge}

    def from_dict(self, d):
        if not d:
            return
        self.g1.from_dict(d.get("g1", {}))
        self.g2.from_dict(d.get("g2", {}))
        self.settings.merge = d.get("merge", self.settings.merge)


class Sequencer:
    """Sequencer（重构计划步骤 1 & 2）：拥有演奏。接收 ClockTick（来自 Transport
    或外部时钟），分发给各 TrackPlayer，并将结果翻译为**语义输出事件**
    （GateOutputEvent / CVOutputEvent / ClockOutputEvent）。
    自身不接触任何 EuroPi API，也不持有 Hardware 引用——完全硬件无关。
    门的「GATE_MS 后拉低」以定时输出事件形式在内部调度，由 Controller.pump 取出 apply。
    """

    def __init__(self):
        self.tracks = []
        self._scheduled = []  # [(due_us, OutputEvent), ...] 待发输出事件（如门控关闭）

    def add_track(self, track):
        self.tracks.append(track)

    def tick(self, now_us):
        """一个 ClockTick：推进全部通道并产出本轮输出事件。
        触发通道额外调度一个 GATE_MS 后的 GateOutputEvent(channel, False) 以自动拉低。"""
        events = []
        for t in self.tracks:
            on = t.output()
            if on:
                events.append(GateOutputEvent(t.cv_index, True))
                self._scheduled.append(
                    (ticks_add(now_us, GATE_MS * 1000), GateOutputEvent(t.cv_index, False))
                )
            else:
                events.append(GateOutputEvent(t.cv_index, False))
        return events

    def pump(self, now_us):
        """取出已达/超过应发时刻的调度输出事件（如门控关闭）。"""
        due = []
        remain = []
        for due_us, ev in self._scheduled:
            if ticks_diff(now_us, due_us) >= 0:
                due.append(ev)
            else:
                remain.append((due_us, ev))
        self._scheduled = remain
        return due


# ---------------------------------------------------------------------------
# Transport —— 拥有全局音乐时间（when）
# ---------------------------------------------------------------------------

class Transport:
    """Transport：拥有全局时间（tempo / 时钟源 / 运行状态）。
    INT 模式由主循环以 microsecond tick 轮询驱动 ClockTick（见 update()）；
    EXT 模式由 din 上升沿驱动。不直接接触任何 EuroPi API（Hardware 是唯一入口）；
    仅通过回调通知节拍。"""

    def __init__(self, on_tick):
        self._on_tick = on_tick
        self.bpm = 120
        self.mul = 4  # 时钟倍率：实际时钟 = BPM x mul
        self.level = 5  # 全局输出 CV 电平（0~10V）
        self.source = "INT"
        self.running = False
        self.beat_index = 0  # 全局已发生节拍数（第 k 拍后 = k），供显示唱头分页对齐
        self.next_clock_us = 0  # 下一次内部时钟事件应发生的 tick（us）

    def set_on_tick(self, cb):
        """（重构）允许 Application 在构造后注入节拍回调，解耦 Transport 与 Controller。"""
        self._on_tick = cb

    def beat_us(self):
        # 实际每拍间隔 = 60s / (BPM x mul)
        eff = self.bpm * self.mul
        return max(1, 60_000_000 // eff)

    def start(self):
        # 重置相位：下一次时钟事件安排在 beat_us 之后
        self.next_clock_us = ticks_add(ticks_us(), self.beat_us())
        self.beat_index = 0  # 唱头从 0 起步
        self.running = True

    def stop(self):
        self.running = False

    def update(self, now_us):
        """主循环每轮调用：若已达/超过下一次时钟事件，则触发一次节拍并重新锚定调度。
        无论错过多少 tick 都只补一次（重新锚定到 now_us 之后），避免阻塞后爆发式追拍。"""
        if self.source != "INT" or not self.running or self._on_tick is None:
            return
        if ticks_diff(now_us, self.next_clock_us) >= 0:
            self._on_tick()
            self.beat_index += 1  # 已发生一拍（在 tick 之后累加，保证与 pos/last_out 同相）
            # 重新锚定到当前时刻之后一个 beat，丢弃所有错过的 tick
            self.next_clock_us = ticks_add(now_us, self.beat_us())

    def time_to_next_clock_us(self, now_us):
        """距离下一次内部时钟事件的微秒数；非 INT 运行态返回 None。"""
        if self.source != "INT" or not self.running:
            return None
        return ticks_diff(self.next_clock_us, now_us)

    def ext_tick(self, t=None):
        # 仅在外部时钟源时由 din 上升沿驱动（INT 时忽略）
        if self.source == "EXT" and self._on_tick is not None:
            self._on_tick()
            self.beat_index += 1  # 已发生一拍（与 pos/last_out 同相）

    def set_source(self, src):
        if src == self.source:
            return
        self.source = src
        if src == "INT":
            self.start()
        else:
            self.stop()

    def set_bpm(self, bpm):
        bpm = min(max(bpm, MIN_BPM), MAX_BPM)
        if bpm == self.bpm:
            return
        self.bpm = bpm
        if self.source == "INT":
            self.start()  # 以新周期重启

    def set_mul(self, mul):
        mul = min(max(mul, MIN_MUL), MAX_MUL)
        if mul == self.mul:
            return
        self.mul = mul
        if self.source == "INT":
            self.start()  # 以新周期重启

    def set_level(self, level):
        self.level = min(max(level, 0), 10)

    # —— 序列化（模型自管，Controller 只做编排）——
    def to_dict(self):
        return {"source": self.source, "bpm": self.bpm,
                "mul": self.mul, "level": self.level}

    def from_dict(self, d):
        if not d:
            return
        # 直接赋值（与原始 set_state 行为一致，不在此 restart/start）；
        # 启动/停止由 Controller 在加载后依据 source 显式决定（见 __init__）。
        self.source = d.get("source", self.source)
        self.bpm = d.get("bpm", self.bpm)
        self.mul = d.get("mul", self.mul)
        self.level = d.get("level", self.level)


# ---------------------------------------------------------------------------
# Application State —— 仅 UI 状态（单一事实来源之一）
# ---------------------------------------------------------------------------

class AppState:
    """ApplicationState：只保存 UI 状态。
    不保存 Pattern 数据，也不保存播放状态（二者分别在 Pattern / TrackPlayer）。"""

    def __init__(self):
        self.page = 0
        self.sel = 0
        self.k2_picked = False
        self.dirty = True  # 显示需要重绘

    def _set_page(self, p):
        self.page = p
        n = len(GLOBAL_PARAMS) if p == 0 else len(CH_PARAMS)
        if self.sel >= n:
            self.sel = n - 1
        self.k2_picked = False
        self.dirty = True

    def prev_page(self):
        self._set_page((self.page - 1) % NUM_PAGES)

    def next_page(self):
        self._set_page((self.page + 1) % NUM_PAGES)

    def goto_global(self):
        self._set_page(0)


# ---------------------------------------------------------------------------
# ViewModel —— 渲染用视图模型（重构计划步骤 6）：把模型读数翻译为渲染就绪视图
# Renderer 只读取本模型，不直接访问 Transport/Track 内部结构。
# ---------------------------------------------------------------------------

class SequencerViewModel:
    """无状态视图模型：消费 AppState + Sequencer + Transport，呈现渲染所需视图。
    渲染复杂时（如唱头分页/参数取值）在此集中计算，保持 Renderer 纯粹为「画」。"""

    def __init__(self, app, seq, transport):
        self.app = app
        self.seq = seq
        self.transport = transport

    @property
    def page(self):
        return self.app.page

    def status(self):
        """返回 (页标记串, 顶栏参数串)。"""
        if self.app.page == 0:
            abbr, _, _, _, _, _, fmt = GLOBAL_PARAMS[self.app.sel]
            return f"P{self.app.page}", f"{abbr}:{fmt(self.transport)}"
        abbr, kind = CH_PARAMS[self.app.sel]
        track = self.seq.tracks[self.app.page - 1]
        if kind == "merge":
            val = MERGE_MODES[track.merge]
        else:
            val = self._param_value(track, kind)
        return f"P{self.app.page}", f"{abbr}:{val}"

    def channel_sequence(self):
        """返回 (track, wstart, cur) 供序列行与唱头绘制。"""
        track = self.seq.tracks[self.app.page - 1]
        bi = self.transport.beat_index
        cur = bi - 1 if bi > 0 else 0          # 当前全局步（0 基）
        wstart = (cur // 16) * 16              # 当前 16 步页起点
        return track, wstart, cur

    def _param_value(self, track, kind):
        mapping = {
            "rot1": track.g1.rot,
            "rot2": track.g2.rot,
            "steps1": track.g1.steps,
            "steps2": track.g2.steps,
            "pulses1": track.g1.pulses,
            "pulses2": track.g2.pulses,
            "prob1": track.g1.prob,
            "prob2": track.g2.prob,
        }
        return mapping.get(kind, "")


# ---------------------------------------------------------------------------
# Renderer —— 无状态渲染（读 ViewModel，经 Hardware 绘制）
# ---------------------------------------------------------------------------

class Renderer:
    """Renderer：无状态。每次 render() 经 ViewModel 重新读取模型计算 OLED 内容，
    不修改任何状态，不持有持久 UI 数据。"""

    def render(self, app, seq, transport, hw):
        hw.display_clear()
        vm = SequencerViewModel(app, seq, transport)
        if app.page == 0:
            self._draw_global(vm, hw)
        else:
            self._draw_channel(vm, hw)
        hw.display_show()

    def _draw_global(self, vm, hw):
        # 全局时钟页只显示顶栏：页标记右上角，参数左上角；其余行留空。
        # P0 无对应 track，左下角不绘制触发指示。显示完全由 GLOBAL_PARAMS 表驱动。
        page_str, val_str = vm.status()
        hw.display_text(page_str, OLED_WIDTH - len(page_str) * 8, 0)  # 右上角页标记
        hw.display_text(val_str, 0, 0)                                # 左上角参数（左对齐）

    def _draw_channel(self, vm, hw):
        page_str, val_str = vm.status()
        hw.display_text(page_str, OLED_WIDTH - len(page_str) * 8, 0)  # 右上角页标记
        hw.display_text(val_str, 0, 0)                               # 左上角参数（左对齐）
        track, wstart, cur = vm.channel_sequence()
        # 全局 16 步窗口：按 beat_index 以 16 步分页（满 16 步翻到下一页）。
        # 序列长度 ≠ 16 时循环补足 16 列；两序列共用同一窗口偏移，唱头对齐二者。
        self._draw_seq_row(track.g1, 8, wstart, hw)
        self._draw_seq_row(track.g2, 16, wstart, hw)
        # 底部唱头：随节拍向右，仅唱头所在列显示方块(触发)/点(未触发)
        self._draw_playhead(track, cur % 16, 26, hw)

    def _draw_seq_row(self, gen, y, wstart, hw):
        # 16 列窗口（8px/列，整屏宽度）：列 c 对应全局步 (wstart + c)。
        # 序列长度 < 16 → 循环补足 16 列；> 16 → 显示连续 16 步；下一页由调用方推进 wstart。
        for c in range(16):
            idx = (wstart + c) % gen.steps
            if gen.pattern[idx]:
                hw.display_fill_rect(c * 8, y, 6, 6, 1)      # 触发步：实心方块
            else:
                hw.display_fill_rect(c * 8 + 2, y + 2, 2, 2, 1)  # 非触发：点

    def _draw_playhead(self, track, play_pos, y, hw):
        # 仅唱头所在列显示：触发→实心方块，未触发→点；其余列留空（无滚动窗口噪声）
        if track.last_out:
            hw.display_fill_rect(play_pos * 8, y, 6, 6, 1)
        else:
            hw.display_fill_rect(play_pos * 8 + 2, y + 2, 2, 2, 1)


# ---------------------------------------------------------------------------
# UI Pages —— 把交互译为命令（重构计划步骤 5：与 Controller 分离）
# 仅产出 Command，不直接改模型；经注入的 exec_cmd 回调统一执行。
# ---------------------------------------------------------------------------

class Pages:
    """UI Pages：把旋钮/按键交互翻译为命令（Command），不直改模型。
    通过构造时注入的 exec_cmd(Command) 回调提交变更；选中项变化经 app.dirty 触发重绘。
    """

    def __init__(self, app, transport, seq, exec_cmd):
        self.app = app
        self.transport = transport
        self.seq = seq
        self._exec = exec_cmd

    def set_exec(self, exec_cmd):
        self._exec = exec_cmd

    # —— K1：参数选择（带 ±0.5 档迟滞，防边界抖动）——
    def on_knob1(self, p):
        n = len(GLOBAL_PARAMS) if self.app.page == 0 else len(CH_PARAMS)
        idx = int(p * n)  # 截断实现 ±0.5 档迟滞
        if idx >= n:
            idx = n - 1
        if idx != self.app.sel:
            self.app.sel = idx
            self.app.k2_picked = False
            self._changed()  # 选中项变了，立即请求重绘（修复停钟时不刷新的问题）

    # —— K2：参数数值调节（拾取策略）——
    def on_knob2(self, p):
        if self.app.page == 0:
            self._edit_global(p)
        else:
            self._edit_channel(self.seq.tracks[self.app.page - 1], p)

    def _edit_global(self, p):
        # 全部全局参数走同一套拾取/编辑流程（GLOBAL_PARAMS 表驱动）；
        # 时钟源作为 discrete 离散参数（0=INT/1=EXT），与 BPM 等对称，无特判。
        _, pmin, pmax, discrete, get_cur, make_cmd, _ = GLOBAL_PARAMS[self.app.sel]
        cur = get_cur(self.transport)
        self._apply_pickup(
            cur, pmin, pmax,
            lambda v: make_cmd(self.transport, v), p, discrete=discrete
        )

    def _edit_channel(self, track, p):
        kind = CH_PARAMS[self.app.sel][1]
        if kind == "rot1":
            self._apply_pickup(track.g1.rot, 0, track.g1.steps,
                               lambda v: SetTrackParam(track, 1, "rot", v), p)
        elif kind == "rot2":
            self._apply_pickup(track.g2.rot, 0, track.g2.steps,
                               lambda v: SetTrackParam(track, 2, "rot", v), p)
        elif kind == "steps1":
            self._apply_pickup(track.g1.steps, MIN_STEPS, MAX_STEPS,
                               lambda v: SetTrackParam(track, 1, "steps", v), p)
        elif kind == "steps2":
            self._apply_pickup(track.g2.steps, MIN_STEPS, MAX_STEPS,
                               lambda v: SetTrackParam(track, 2, "steps", v), p)
        elif kind == "pulses1":
            self._apply_pickup(track.g1.pulses, 0, track.g1.steps,
                               lambda v: SetTrackParam(track, 1, "pulses", v), p)
        elif kind == "pulses2":
            self._apply_pickup(track.g2.pulses, 0, track.g2.steps,
                               lambda v: SetTrackParam(track, 2, "pulses", v), p)
        elif kind == "prob1":
            self._apply_pickup(track.g1.prob, 0, 100,
                               lambda v: SetTrackParam(track, 1, "prob", v), p)
        elif kind == "prob2":
            self._apply_pickup(track.g2.prob, 0, 100,
                               lambda v: SetTrackParam(track, 2, "prob", v), p)
        elif kind == "merge":
            self._apply_pickup(track.merge, 0, 4,
                               lambda v: SetMerge(track, v), p, discrete=True)

    def _apply_pickup(self, current, pmin, pmax, make_cmd, p, discrete=False):
        """K2 拾取策略：旋钮位置匹配当前值（tol 容差）后才生效；生效后值变化即提交。
        提交经注入的 exec_cmd(command) 统一执行（改模型 + 置 dirty）。"""
        val = round(p * (pmax - pmin)) + pmin
        val = min(max(val, pmin), pmax)
        tol = 0 if discrete else max(1, (pmax - pmin) // 32)
        if not self.app.k2_picked:
            if abs(val - current) <= tol:
                self.app.k2_picked = True
            return
        if val == current:
            return
        self._exec(make_cmd(val))

    def _changed(self):
        self.app.dirty = True


# ---------------------------------------------------------------------------
# Controller —— 事件分发 + 命令派发 + 主循环（重构计划步骤 5：与 Application/Pages 分离）
# 持有模型与硬件适配器；把输入事件经 Pages 译为命令执行，把 Sequencer 输出事件经
# Hardware 适配器落盘。几乎无业务逻辑（业务在模型/页面内）。
# ---------------------------------------------------------------------------

class Controller:
    def __init__(self, hw, app, transport, seq, input_mgr, renderer, pages, on_save=None):
        self.hw = hw
        self.app = app
        self.transport = transport
        self.seq = seq
        self.input = input_mgr
        self.renderer = renderer
        self.pages = pages
        self._on_save = on_save

    # —— 节拍（ClockTick 事件）：产出输出事件并 apply ——
    def _on_beat(self):
        with PROFILER.section("on_beat"):
            # 同步门触发高电平（全局输出电压）后再应用本轮输出事件
            self.hw.set_gate_level(self.transport.level)
            events = self.seq.tick(ticks_us())
            self._apply_outputs(events)
        self.app.dirty = True

    # —— 把输出事件 apply 到硬件（Hardware 是唯一出口）——
    def _apply_outputs(self, events):
        for ev in events:
            if isinstance(ev, GateOutputEvent):
                self.hw.set_gate(ev.channel, ev.state)
            elif isinstance(ev, CVOutputEvent):
                self.hw.set_cv(ev.channel, ev.voltage)
            elif isinstance(ev, ClockOutputEvent):
                pass  # 当前脚本无独立时钟输出引脚

    # —— 事件分发（Controller 的职责之一）——
    def dispatch(self, ev):
        if isinstance(ev, KnobTurn):
            if ev.knob == 1:
                self.pages.on_knob1(ev.value)
            else:
                self.pages.on_knob2(ev.value)
        elif isinstance(ev, ButtonEvent):
            if ev.button == "B1":
                if ev.kind == "long":
                    if self._on_save:
                        self._on_save()  # 长按 K1：显式保存（唯一触发 save 的途径）
                else:
                    self.app.prev_page()
            else:  # B2
                if ev.kind == "long":
                    self.app.goto_global()
                else:
                    self.app.next_page()
        elif isinstance(ev, ClockEvent):
            # 外部时钟 tick：仅 EXT 源生效（Transport.ext_tick 内含 source 守卫）
            self.transport.ext_tick()

    # —— 命令执行（Controller → Model 统一出口，与输入侧事件对称）——
    def _exec(self, cmd):
        """执行一个命令：改模型 + 置 dirty。所有模型变更都经此单一出口，
        便于后续录制/回放/单测（交互层只产出命令，不直接改模型）。"""
        cmd.execute()
        self.on_changed()

    def on_changed(self):
        self.app.dirty = True

    # —— 主循环（microsecond tick 驱动）——
    def main(self):
        while True:
            now_us = ticks_us()
            with PROFILER.section("loop"):
                # 内部时钟 / 门控关闭：到达应发生的 tick 时由主循环触发
                self.transport.update(now_us)
                # 取走已达时刻的调度输出事件（门控关闭等）并 apply
                due = self.seq.pump(now_us)
                if due:
                    self._apply_outputs(due)
                with PROFILER.section("poll"):
                    events = self.input.poll()
                for ev in events:
                    with PROFILER.section("dispatch"):
                        self.dispatch(ev)
                # 屏幕刷新：预留 t_show，距离下次时钟事件 < t_show 时跳过，避免抢占时钟精度
                if self.app.dirty:
                    gap = self.transport.time_to_next_clock_us(now_us)
                    if gap is None or gap >= T_SHOW_US:
                        with PROFILER.section("render"):
                            self.renderer.render(self.app, self.seq, self.transport, self.hw)
                        self.app.dirty = False
            time.sleep_ms(0)
            PROFILER.maybe_print(now_us)


# ---------------------------------------------------------------------------
# Application —— EuroPiScript 入口（重构计划步骤 5：与 Controller/Pages 分离）
# 负责装配 Hardware / 模型 / Controller，以及状态持久化（依赖 EuroPiScript 的
# save_state_json / load_state_json）。
# ---------------------------------------------------------------------------

class Euclidean2(EuroPiScript):
    @classmethod
    def display_name(cls):
        return "Euclid2 6ch"

    def __init__(self):
        super().__init__()

        # 装配硬件与模型（Platform 无关层在此接线）
        hw = Hardware()
        app = AppState()
        transport = Transport(None)
        seq = Sequencer()

        # 6 通道默认节奏（差异化）
        for i in range(6):
            g1 = EuclidPattern(16, 5, 0, 100)
            g2 = EuclidPattern(16, 3, (i * 2) % 16, 100)
            seq.add_track(Track(i, g1, g2, MERGE_OR))

        input_mgr = InputManager(hw)
        renderer = Renderer()
        pages = Pages(app, transport, seq, None)  # exec 回调在 Controller 建好后注入

        controller = Controller(
            hw, app, transport, seq, input_mgr, renderer, pages,
            on_save=self.save_state,
        )
        pages.set_exec(controller._exec)      # 注入命令执行出口
        transport.set_on_tick(controller._on_beat)  # 注入节拍回调
        hw.set_gate_level(transport.level)

        # 暴露给持久化方法
        self.hw = hw
        self.app = app
        self.transport = transport
        self.seq = seq
        self.controller = controller

        # 时钟输入由 Hardware 自管理：on_clock_rise 已在 Hardware.__init__ 注册，
        # 外部时钟上升沿经队列作为 ClockEvent 流出，dispatch 中驱动 Transport.ext_tick
        if SAVE_STATES:
            self.load_state()
        # 启动时钟（依据当前 source：默认 INT）
        if self.transport.source == "INT":
            self.transport.start()
        else:
            self.transport.stop()

    def main(self):
        self.controller.main()

    # —— 状态持久化（受 SAVE_STATES 开关控制；依赖 EuroPiScript 的 JSON 接口）——
    def on_changed(self):
        self.app.dirty = True

    def get_state(self):
        return {
            "clock": self.transport.to_dict(),
            "channels": [t.to_dict() for t in self.seq.tracks],
        }

    def set_state(self, state):
        try:
            self.transport.from_dict(state.get("clock", {}))
            for track, d in zip(self.seq.tracks, state.get("channels") or []):
                track.from_dict(d)
        except Exception as e:
            print("Euclidean2: failed to load state:", e)

    def load_state(self):
        state = self.load_state_json()
        if state:
            self.set_state(state)

    def save_state(self):
        if not SAVE_STATES:
            return
        self.save_state_json(self.get_state())


if __name__ == "__main__":
    Euclidean2().main()
