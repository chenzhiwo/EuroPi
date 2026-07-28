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
  B1            短按：上一页    长按(>500ms)：手动推进一拍（无时钟时试听）
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
单文件分层架构（参照 europi-ws/ARCHITECTURE.md 的亮点，受"保持单文件"约束）
--------------------------------------------------------------------------------
本文件刻意保持为**单个脚本**，但内部按 ARCHITECTURE.md 的架构原则严格分层，
各层之间只通过「语义事件 / 命令 / 模型读取」交互，不直接互相改状态：

    EuroPi 硬件
        │
        ▼
    Hardware          ← 唯一允许调用 EuroPi API 的模块（硬件抽象）
        │
        ▼
    InputManager      ← 硬件状态 → 语义事件 (KnobTurn / ButtonEvent)
        │
        ▼
    Controller         ← Euclidean2：事件分发 + 命令派发 + 触发渲染（几乎无业务）
        ├─→ UI Pages   ← 把交互译为命令（只调用 Transport / Track 的设置器）
        ├─→ AppState   ← 仅 UI 状态（当前页 / 选中项 / K2 拾取）
        ├─→ Sequencer  ← 拥有演奏：接收 ClockTick → 分发给各 Track → 输出事件
        │      ├─ EuclidPattern  ← Pattern：单个发生器的音乐内容 (what)
        │      └─ Track          ← TrackSettings + TrackPlayer（每通道运行时）
        └─→ Transport   ← 拥有全局时间：BPM / 时钟源 / 运行状态 (when)
        │
        ▼
    Renderer           ← 无状态渲染：读 AppState + Sequencer/Transport，经 Hardware 绘制

关键约束（来自 ARCHITECTURE.md）：
  * Sequencer / Pattern / Track / Transport 绝不出现 oled/k1/cv1 等 EuroPi 调用；
    它们只产生语义输出（如 hw.set_cv(idx, v)），由 Hardware 适配器落盘到硬件。
  * Renderer 是无状态的纯函数：只读模型、不修改、不持有持久 UI 数据。
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

NUM_PAGES = 7  # 0 全局 + 1..6 通道

MIN_BPM = 20
MAX_BPM = 240
MIN_MUL = 1
MAX_MUL = 8  # 时钟倍率（实际时钟 = BPM x mul）；上限 8 以保证每拍间隔 (>31ms) 留出刷新窗口
MAX_STEPS = 64  # 单发生器序列最大步数
MIN_STEPS = 1   # 单发生器序列最小步数
GATE_MS = 5  # 时钟事件后统一拉低输出的延迟（ms）
T_SHOW_US = 20_000  # 屏幕刷新预留窗口（微秒）；距离下次时钟事件不足该值时跳过刷新


# ---------------------------------------------------------------------------
# 语义事件（InputManager → Controller 的通信载体）
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


# ---------------------------------------------------------------------------
# Hardware Adapter —— 唯一允许访问 EuroPi 硬件 API 的模块（硬件抽象）
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

    # --- 输入读取 ---
    def knob1(self):
        return self.k1.percent()

    def knob2(self):
        return self.k2.percent()

    def button1(self):
        return self.b1.value() == HIGH

    def button2(self):
        return self.b2.value() == HIGH

    def on_clock_rise(self, cb):
        # 注册 DIN 上升沿回调（外部时钟源时由 Transport 驱动）。
        # 中断上下文里只做轻量调度：用 micropython.schedule 把真正的回调推到
        # 主循环执行，避免在 ISR 中做 CV 输出等重活、并消除与主循环的共享状态竞争。
        def _isr(_):
            try:
                micropython.schedule(cb, None)
            except (ValueError, RuntimeError):
                pass  # 调度队列满（正常时钟速率下不会发生），丢弃本次 tick
        self.din.handler(_isr)

    # --- CV / Gate 输出（输出事件的落点）---
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
# Input Manager —— 硬件状态 → 语义事件
# ---------------------------------------------------------------------------

class InputManager:
    """把硬件状态转化为语义事件（KnobTurn / ButtonEvent）。
    不修改任何应用状态；按钮做短按/长按边沿判定。"""

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
        # 旋钮事件驱动：仅当相对上次发出值变化 >= 1% 时才产生事件
        self._knob1_last = -1.0  # -1 强制首轮发出初始状态
        self._knob2_last = -1.0

    def poll(self):
        events = []
        now = ticks_ms()
        d1 = self.hw.button1()
        d2 = self.hw.button2()

        if d1 and d2:
            # 双按 = 返回菜单（交由系统处理），本管理器不产出事件
            self._b1_down = d1
            self._b2_down = d2
            return events

        # B1
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

        # B2
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

        # 旋钮：事件驱动，仅当位置变化超过 1% 才发出事件（携带当前值）
        v1 = self.hw.knob1()
        if abs(v1 - self._knob1_last) >= 0.01:
            self._knob1_last = v1
            self._knob1.value = v1
            events.append(self._knob1)
        v2 = self.hw.knob2()
        if abs(v2 - self._knob2_last) >= 0.01:
            self._knob2_last = v2
            self._knob2.value = v2
            events.append(self._knob2)
        return events


# ---------------------------------------------------------------------------
# Sequencer Core —— Pattern / TrackPlayer / Sequencer（不含任何硬件调用）
# ---------------------------------------------------------------------------

class EuclidPattern:
    """Pattern：单个欧几里得发生器的音乐内容（what to play）。
    不含运行时播放头（pos 由 TrackPlayer / Track 持有）。"""

    def __init__(self, steps, pulses, rot, prob):
        self.steps = steps
        self.pulses = pulses
        self.rot = rot
        self.prob = prob
        self.pattern = []
        self.regenerate()

    def regenerate(self):
        self.pattern = generate_euclidean_pattern(self.steps, self.pulses, self.rot)

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


class Track:
    """Track = Pattern × 2 + TrackSettings + TrackPlayer（一个通道）。
    - Pattern:    g1 / g2 的欧几里得序列（steps/pulses/rot/prob + pattern[]）
    - TrackSettings: merge（输出电压为全局项，见 Transport.level）
    - TrackPlayer:  g1_pos / g2_pos 播放头（输出由 g1/g2 合并直接计算，无移位寄存器）
    """

    def __init__(self, cv_index, g1, g2, merge):
        self.cv_index = cv_index
        self.g1 = g1
        self.g2 = g2
        self.merge = merge
        self.g1_pos = g1.steps - 1  # 首次 advance 后落于 0
        self.g2_pos = g2.steps - 1
        self.last_out = False  # 当前步（最近一次时钟事件）的触发状态

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
        self.merge = val

    # —— TrackPlayer 运行时 ——
    def _active(self, gen, pos):
        """该播放头步是否应触发（pattern 且通过概率保留判定）。"""
        if not gen.pattern[pos]:
            return False
        if gen.prob >= 100:
            return True
        if gen.prob <= 0:
            return False
        return random.random() < gen.prob / 100.0

    def output(self):
        """推进一拍：两路播放头前进，按合并+概率计算 ON/OFF。
        返回该通道本拍是否触发（True/False）；由调用方翻译为 CV/Gate 输出事件。"""
        self.g1_pos = (self.g1_pos + 1) % self.g1.steps
        self.g2_pos = (self.g2_pos + 1) % self.g2.steps
        on1 = self._active(self.g1, self.g1_pos)
        on2 = self._active(self.g2, self.g2_pos)
        out = combine_outputs(on1, on2, self.merge)
        self.last_out = out  # 记录当前步触发状态，供屏幕左下角指示
        return out


class Sequencer:
    """Sequencer：拥有演奏。接收 ClockTick（来自 Transport），分发给各 TrackPlayer，
    并将结果翻译为 CV/Gate 输出事件，经 Hardware Adapter 应用到物理输出。
    自身不接触任何 EuroPi API。"""

    def __init__(self, hw, transport):
        self.hw = hw
        self.transport = transport
        self.tracks = []
        self.next_gate_off_us = None  # 下一次统一拉低门应发生的 tick（us）；None=无待办

    def add_track(self, track):
        self.tracks.append(track)

    def tick(self):
        """一个 ClockTick：推进全部通道并刷新门输出。"""
        level = self.transport.level  # 全局输出电压
        for t in self.tracks:
            on = t.output()
            if on:
                self.hw.set_cv(t.cv_index, level)
            else:
                self.hw.off_cv(t.cv_index)
        # 统一在 GATE_MS 后拉低所有门（由主循环 update() 触发）
        self.next_gate_off_us = ticks_add(ticks_us(), GATE_MS * 1000)

    def update(self, now_us):
        """主循环每轮调用：若已达/超过门控关闭时刻，则统一拉低所有门。"""
        if self.next_gate_off_us is not None and ticks_diff(now_us, self.next_gate_off_us) >= 0:
            self._gate_off()
            self.next_gate_off_us = None

    def _gate_off(self, _=None):
        self.hw.off_all_cvs()


# ---------------------------------------------------------------------------
# Transport —— 拥有全局音乐时间（when）
# ---------------------------------------------------------------------------

class Transport:
    """Transport：拥有全局时间（tempo / 时钟源 / 运行状态）。
    INT 模式由主循环以 microsecond tick 轮询驱动 ClockTick（见 update()）；
    EXT 模式由 din 上升沿驱动。不直接接触 OLED/按钮/旋钮；仅通过回调通知节拍。"""

    def __init__(self, hw, on_tick):
        self.hw = hw
        self._on_tick = on_tick
        self.bpm = 120
        self.mul = 4  # 时钟倍率：实际时钟 = BPM x mul
        self.level = 5  # 全局输出 CV 电平（0~10V）
        self.source = "INT"
        self.running = False
        self.next_clock_us = 0  # 下一次内部时钟事件应发生的 tick（us）

    def beat_us(self):
        # 实际每拍间隔 = 60s / (BPM x mul)
        eff = self.bpm * self.mul
        return max(1, 60_000_000 // eff)

    def start(self):
        # 重置相位：下一次时钟事件安排在 beat_us 之后
        self.next_clock_us = ticks_add(ticks_us(), self.beat_us())
        self.running = True

    def stop(self):
        self.running = False

    def update(self, now_us):
        """主循环每轮调用：若已达/超过下一次时钟事件，则触发一次节拍并重新锚定调度。
        无论错过多少 tick 都只补一次（重新锚定到 now_us 之后），避免阻塞后爆发式追拍。"""
        if self.source != "INT" or not self.running:
            return
        if ticks_diff(now_us, self.next_clock_us) >= 0:
            self._on_tick()
            # 重新锚定到当前时刻之后一个 beat，丢弃所有错过的 tick
            self.next_clock_us = ticks_add(now_us, self.beat_us())

    def time_to_next_clock_us(self, now_us):
        """距离下一次内部时钟事件的微秒数；非 INT 运行态返回 None。"""
        if self.source != "INT" or not self.running:
            return None
        return ticks_diff(self.next_clock_us, now_us)

    def ext_tick(self, t=None):
        # 仅在外部时钟源时由 din 上升沿驱动（INT 时忽略）
        if self.source == "EXT":
            self._on_tick()

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
        n = 4 if p == 0 else len(CH_PARAMS)  # P0：时钟源 / BPM / MUL / 输出电压
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
# Renderer —— 无状态渲染（读 AppState + Sequencer/Transport，经 Hardware 绘制）
# ---------------------------------------------------------------------------

class Renderer:
    """Renderer：无状态。每次 render() 重新读取模型计算 OLED 内容，
    不修改任何状态，不持有持久 UI 数据。"""

    def render(self, app, seq, transport, hw):
        hw.display_clear()
        if app.page == 0:
            self._draw_global(app, transport, hw)
        else:
            self._draw_channel(app, seq.tracks[app.page - 1], hw)
        hw.display_show()

    def _draw_global(self, app, transport, hw):
        # 全局时钟页只显示顶栏（P0 CLK:.. / P0 BPM:.. / P0 MUL:.. / P0 LVL:..），其余行留空；
        # P0 无对应 track，左下角不绘制触发指示
        if app.sel == 0:
            hw.display_text(f"P0 CLK:{transport.source}", 0, 0)
        elif app.sel == 1:
            hw.display_text(f"P0 BPM:{transport.bpm}", 0, 0)
        elif app.sel == 2:
            hw.display_text(f"P0 MUL:{transport.mul}", 0, 0)
        else:
            hw.display_text(f"P0 LVL:{transport.level}V", 0, 0)

    def _draw_channel(self, app, track, hw):
        abbr, kind = CH_PARAMS[app.sel]
        if kind == "merge":
            val = MERGE_MODES[track.merge]
        else:
            val = self._param_value(track, kind)
        hw.display_text(f"P{app.page} {abbr}:{val}", 0, 0)
        self._draw_seq_row(track.g1, track.g1_pos, 8, hw)
        self._draw_seq_row(track.g2, track.g2_pos, 16, hw)
        # 左下角指示：当前步（本通道）的触发状态
        self._draw_step_indicator(track.last_out, hw)

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

    def _draw_seq_row(self, gen, pos, y, hw):
        # 当前步固定 col0，向右取至多 16 格；序列步数 < 16 则只显示实际步数
        for i in range(min(16, gen.steps)):
            idx = (pos + i) % gen.steps
            x = i * 8
            if gen.pattern[idx]:
                hw.display_fill_rect(x, y, 6, 6, 1)
            else:
                hw.display_fill_rect(x + 2, y + 2, 2, 2, 1)

    def _draw_step_indicator(self, on, hw):
        # 屏幕最左下角：实心方块 = 当前步触发，点 = 未触发
        if on:
            hw.display_fill_rect(0, 26, 6, 6, 1)
        else:
            hw.display_fill_rect(2, 28, 2, 2, 1)

# ---------------------------------------------------------------------------
# Controller / Application —— 事件分发 + 命令派发 + 触发渲染（几乎无业务逻辑）
# ---------------------------------------------------------------------------

class Euclidean2(EuroPiScript):
    @classmethod
    def display_name(cls):
        return "Euclid2 6ch"

    def __init__(self):
        super().__init__()

        self.hw = Hardware()
        self.app = AppState()
        self.transport = Transport(self.hw, self._on_beat)
        self.seq = Sequencer(self.hw, self.transport)

        # 6 通道默认节奏（差异化）
        for i in range(6):
            g1 = EuclidPattern(16, 5, 0, 100)
            g2 = EuclidPattern(16, 3, (i * 2) % 16, 100)
            self.seq.add_track(Track(i, g1, g2, MERGE_OR))

        self.input = InputManager(self.hw)
        self.renderer = Renderer()

        # 时钟输入：注册 din 回调；仅在 EXT 时由 Transport.ext_tick 驱动
        self.hw.on_clock_rise(self.transport.ext_tick)
        if SAVE_STATES:
            self.load_state()
        # 启动时钟（依据当前 source：默认 INT）
        if self.transport.source == "INT":
            self.transport.start()
        else:
            self.transport.stop()

    # —— 节拍（ClockTick 事件）——
    def _on_beat(self):
        self.seq.tick()
        self.app.dirty = True

    # —— 事件分发（Controller 的职责之一）——
    def dispatch(self, ev):
        if isinstance(ev, KnobTurn):
            if ev.knob == 1:
                self._on_knob1(ev.value)
            else:
                self._on_knob2(ev.value)
        elif isinstance(ev, ButtonEvent):
            if ev.button == "B1":
                if ev.kind == "long":
                    self.save_state()  # 长按 K1：显式保存（唯一触发 save 的途径）
                else:
                    self.app.prev_page()
            else:  # B2
                if ev.kind == "long":
                    self.app.goto_global()
                else:
                    self.app.next_page()

    # —— UI Pages：把交互译为命令（只调用 Transport / Track 的设置器）——
    def _on_knob1(self, p):
        n = 4 if self.app.page == 0 else len(CH_PARAMS)
        idx = int(p * n)  # 截断实现 ±0.5 档迟滞，防边界抖动
        if idx >= n:
            idx = n - 1
        if idx != self.app.sel:
            self.app.sel = idx
            self.app.k2_picked = False

    def _on_knob2(self, p):
        if self.app.page == 0:
            self._edit_global(p)
        else:
            self._edit_channel(self.seq.tracks[self.app.page - 1], p)

    def _edit_global(self, p):
        if self.app.sel == 0:  # 时钟源（离散）
            val = 1 if p > 0.5 else 0
            cur = 0 if self.transport.source == "INT" else 1
            if not self.app.k2_picked:
                if val == cur:
                    self.app.k2_picked = True
                return
            if val == cur:
                return
            self.transport.set_source("EXT" if val == 1 else "INT")
            self.on_changed()
        elif self.app.sel == 1:  # BPM
            self._apply_pickup(
                self.transport.bpm, MIN_BPM, MAX_BPM, self.transport.set_bpm, p
            )
        elif self.app.sel == 2:  # mul（时钟倍率：实际时钟 = BPM x mul）
            self._apply_pickup(
                self.transport.mul, MIN_MUL, MAX_MUL, self.transport.set_mul, p
            )
        else:  # 输出电压（全局，0~10V）
            self._apply_pickup(
                self.transport.level, 0, 10, self.transport.set_level, p
            )

    def _edit_channel(self, track, p):
        kind = CH_PARAMS[self.app.sel][1]
        if kind == "rot1":
            self._apply_pickup(track.g1.rot, 0, track.g1.steps,
                               lambda v: track.set_gen(1, "rot", v), p)
        elif kind == "rot2":
            self._apply_pickup(track.g2.rot, 0, track.g2.steps,
                               lambda v: track.set_gen(2, "rot", v), p)
        elif kind == "steps1":
            self._apply_pickup(track.g1.steps, MIN_STEPS, MAX_STEPS,
                               lambda v: track.set_gen(1, "steps", v), p)
        elif kind == "steps2":
            self._apply_pickup(track.g2.steps, MIN_STEPS, MAX_STEPS,
                               lambda v: track.set_gen(2, "steps", v), p)
        elif kind == "pulses1":
            self._apply_pickup(track.g1.pulses, 0, track.g1.steps,
                               lambda v: track.set_gen(1, "pulses", v), p)
        elif kind == "pulses2":
            self._apply_pickup(track.g2.pulses, 0, track.g2.steps,
                               lambda v: track.set_gen(2, "pulses", v), p)
        elif kind == "prob1":
            self._apply_pickup(track.g1.prob, 0, 100,
                               lambda v: track.set_gen(1, "prob", v), p)
        elif kind == "prob2":
            self._apply_pickup(track.g2.prob, 0, 100,
                               lambda v: track.set_gen(2, "prob", v), p)
        elif kind == "merge":
            self._apply_pickup(track.merge, 0, 4, track.set_merge, p, discrete=True)

    def _apply_pickup(self, current, pmin, pmax, setter, p, discrete=False):
        """K2 拾取策略：旋钮位置匹配当前值（tol 容差）后才生效；生效后值变化即提交。
        去除时间消抖：仅保留 tol 拾取匹配。"""
        val = round(p * (pmax - pmin)) + pmin
        val = min(max(val, pmin), pmax)
        tol = 0 if discrete else max(1, (pmax - pmin) // 32)
        if not self.app.k2_picked:
            if abs(val - current) <= tol:
                self.app.k2_picked = True
            return
        if val == current:
            return
        setter(val)
        self.on_changed()

    # —— 状态持久化（受 SAVE_STATES 开关控制）——
    def on_changed(self):
        self.app.dirty = True

    def get_state(self):
        return {
            "clock": {
                "source": self.transport.source,
                "bpm": self.transport.bpm,
                "mul": self.transport.mul,
                "level": self.transport.level,
            },
            "channels": [
                {
                    "g1": {
                        "steps": c.g1.steps,
                        "pulses": c.g1.pulses,
                        "rot": c.g1.rot,
                        "prob": c.g1.prob,
                    },
                    "g2": {
                        "steps": c.g2.steps,
                        "pulses": c.g2.pulses,
                        "rot": c.g2.rot,
                        "prob": c.g2.prob,
                    },
                    "merge": c.merge,
                }
                for c in self.seq.tracks
            ],
        }

    def set_state(self, state):
        try:
            clk = state.get("clock", {})
            self.transport.source = clk.get("source", "INT")
            self.transport.bpm = clk.get("bpm", 120)
            self.transport.mul = clk.get("mul", 4)
            self.transport.level = clk.get("level", 5)
            for i, c in enumerate(self.seq.tracks):
                d = (state.get("channels") or [])[i]
                if d is None:
                    break
                g1 = d["g1"]
                g2 = d["g2"]
                c.g1.steps = g1["steps"]
                c.g1.pulses = g1["pulses"]
                c.g1.rot = g1["rot"]
                c.g1.prob = g1["prob"]
                c.g1.regenerate()
                c.g2.steps = g2["steps"]
                c.g2.pulses = g2["pulses"]
                c.g2.rot = g2["rot"]
                c.g2.prob = g2["prob"]
                c.g2.regenerate()
                c.merge = d["merge"]
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

    # —— 主循环（microsecond tick 驱动）——
    def main(self):
        while True:
            now_us = ticks_us()
            # 内部时钟 / 门控关闭：到达应发生的 tick 时由主循环触发
            self.transport.update(now_us)
            self.seq.update(now_us)
            for ev in self.input.poll():
                self.dispatch(ev)
            # 屏幕刷新：预留 t_show，距离下次时钟事件 < t_show 时跳过，避免抢占时钟精度
            if self.app.dirty:
                gap = self.transport.time_to_next_clock_us(now_us)
                if gap is None or gap >= T_SHOW_US:
                    self.renderer.render(self.app, self.seq, self.transport, self.hw)
                    self.app.dirty = False
            time.sleep_ms(0)


if __name__ == "__main__":
    Euclidean2().main()
