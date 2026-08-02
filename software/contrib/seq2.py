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
Seq2 - 3-track multi-engine sequencer
=====================================

3 条轨道，每条占用一对固定的 CV 输出（共 6 路），可在三种类型间切换：
  OFF  该轨关闭（两端输出 0V）
  EUC  欧几里得节奏引擎（沿用 euclidean2 的双发生器 + 合并逻辑）
  CV   最长 16 步的 CV 步进音序器（保持型电压，步值 = MIDI note 0~127）

固定配对（无需路由表）：
  track1 → CV1(主/音高) + CV4(门/钟)
  track2 → CV2(主/音高) + CV5(门/钟)
  track3 → CV3(主/音高) + CV6(门/钟)
  - EUC 时：第一个 CV（cv1~3）出门（欧几里得节奏），第二个 CV（cv4~6）出时钟（每个步稳定脉冲）
  - CV  时：主通道出保持型音高，门通道出门脉冲（长度 GLEN）；步值=0 时不出门

电压范围下沉到每轨（v_lo / v_hi）；门由固定容量 OutputScheduler 维护电平与截止时间。

控制 / 时钟 / 页面交互见 seq2.md。本文件保持单文件，严格按分层架构：
  Hardware / ClockCapture → InputManager → Controller → Pages / UiState /
  ClockService / TransportRuntime / Sequencer / TrackFeature / OutputScheduler；
  ViewSnapshot → Renderer。仅 Hardware 接触 EuroPi API。
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

import random
import time
import micropython
from utime import ticks_diff, ticks_ms, ticks_us, ticks_add


# 状态持久化总开关
SAVE_STATES = True

# ---------------------------------------------------------------------------
# 性能分析（全局开关 + 统计器）
# ---------------------------------------------------------------------------
PROFILE = False

class _NullProfiler:
    class _NullSection:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    _section = _NullSection()
    def section(self, name): return self._section
    def record(self, name, dt_us): pass
    def maybe_print(self, now_us): pass
    def print_stats(self): pass


class _ProfSection:
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
    def __init__(self):
        self._stats = {}
        self._last_print_us = 0
        self._start_us = ticks_us()
        self._warmed = False

    def section(self, name):
        return _ProfSection(self, name)

    def record(self, name, dt_us):
        s = self._stats.get(name)
        if s is None:
            self._stats[name] = [dt_us, dt_us, 1, dt_us]
        else:
            s[0] = dt_us
            if dt_us > s[1]:
                s[1] = dt_us
            s[2] += 1
            s[3] += dt_us

    def maybe_print(self, now_us):
        if not self._warmed:
            if now_us - self._start_us >= 1_000_000:
                self._warmed = True
                self._stats = {}
                self._last_print_us = now_us
            return
        if now_us - self._last_print_us >= 1_000_000:
            self._last_print_us = now_us
            self.print_stats()
            for s in self._stats.values():
                s[2] = 0
                s[3] = 0

    def print_stats(self):
        if not self._stats:
            return
        parts = []
        for name, s in self._stats.items():
            last, mx, n, tot = s
            avg = (tot / n) / 1000 if n else 0
            parts.append("%s cur=%.2f avg=%.2f max=%.2f ms (n=%d)"
                         % (name, last / 1000, avg, mx / 1000, n))
        print("PROF:", " | ".join(parts))


PROFILER = Profiler() if PROFILE else _NullProfiler()


# ---------------------------------------------------------------------------
# 配置与纯函数（与硬件无关）
# ---------------------------------------------------------------------------

MERGE_MODES = ["OR", "AND", "XOR", "G1", "G2"]
MERGE_OR, MERGE_AND, MERGE_XOR, MERGE_G1, MERGE_G2 = range(5)

def combine_outputs(on1, on2, mode):
    if mode == MERGE_OR:
        return on1 or on2
    if mode == MERGE_AND:
        return on1 and on2
    if mode == MERGE_XOR:
        return on1 ^ on2
    if mode == MERGE_G1:
        return on1
    return on2


# 轨道类型
TYPE_NAMES = ["OFF", "EUC", "CV"]

MIN_BPM = 20
MAX_BPM = 240
MIN_MUL = 1
MAX_MUL = 8
MAX_STEPS = 64
MIN_STEPS = 1
GATE_MS = 5
CV_MAX = 10          # CV 输出上限（0~10V）
CV_MIN = 0
CV_LEN = 16          # CV 序列最大步数
CV_VAL_MAX = 127     # 步值分辨率 = MIDI note 0~127
CV_GATE_MIN = 10     # 保留足够的最短脉宽，避开 OLED/GC 抖动窗口
CV_GATE_MAX = 90     # 每步保留明确 LOW 间隙，不使用 100% legato

NUM_TRACKS = 3       # 3 条轨道，每条占用一对 CV（主 + 门/钟）
NUM_PAGES = 4        # 0 全局 + 1..3 轨道
PAGE_LABELS = ("P0", "P1", "P2", "P3")
SAVE_GUARD_US = 20_000
T_SHOW_US = SAVE_GUARD_US  # Backwards-compatible name; OLED now uses page guard.
OLED_PAGE_GUARD_US = 6_000
OLED_FULL_RENDER_GUARD_US = 25_000
OLED_PAGE_COUNT = OLED_HEIGHT // 8
INTERNAL_PPQN = 24
DISPLAY_TIMING_MAX_BPM = 240
DISPLAY_TIMING_PPQN = INTERNAL_PPQN
DISPLAY_TIMING_MIN_PERIOD_US = (
    60_000_000 // (DISPLAY_TIMING_MAX_BPM * DISPLAY_TIMING_PPQN)
)
DEBOUNCE_MS = 30
KNOB_DEADBAND = 0.01
KNOB_ACTIVE_DEADBAND = 0.002
KNOB_ACTIVE_TIMEOUT_MS = 200
SAVE_NOTICE_MS = 1_000
SAVE_NOTICE_TEXT = "SAVED"
SAVE_PENDING_TEXT = "SAVING"
SAVE_FAILED_TEXT = "SAVE ERR"
CLOCK_QUEUE_CAPACITY = 16
MAX_CLOCK_CATCH_UP = 4
EXTERNAL_TIMEOUT_PERIODS = 4
STATE_SCHEMA_VERSION = 2


def _clamped_int(value, minimum, maximum, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


class _EuclideanWorkspace:
    """Fixed scratch storage shared by non-realtime Euclidean regeneration."""
    __slots__ = ("counts", "remainders", "levels", "children", "scratch")

    def __init__(self):
        self.counts = bytearray(MAX_STEPS + 1)
        self.remainders = bytearray(MAX_STEPS + 1)
        self.levels = bytearray(MAX_STEPS + 1)
        self.children = bytearray(MAX_STEPS + 1)
        self.scratch = bytearray(MAX_STEPS)


_EUCLIDEAN_WORKSPACE = _EuclideanWorkspace()


def _generate_euclidean_into(target, steps, pulses, workspace):
    """Write canonical Euclid(pulses, steps) into a preallocated buffer.

    The output has the same orientation as the legacy shared generator with
    ``rot=0``. The iterative expansion and canonicalization use only fixed
    workspace; rotation deliberately remains a read-time concern.
    """
    if steps < MIN_STEPS or steps > MAX_STEPS:
        raise ValueError("Steps out of range")
    if pulses < 0 or pulses > steps:
        raise ValueError("Pulses out of range")

    write_pos = 0
    if pulses == 0:
        while write_pos < steps:
            target[write_pos] = 0
            write_pos += 1
        return

    counts = workspace.counts
    remainders = workspace.remainders
    divisor = steps - pulses
    remainders[0] = pulses
    level = 0
    while True:
        counts[level] = divisor // remainders[level]
        remainders[level + 1] = divisor % remainders[level]
        divisor = remainders[level]
        level += 1
        if remainders[level] <= 1:
            break
    counts[level] = divisor

    # Iterative depth-first expansion of the legacy recursive build(level).
    levels = workspace.levels
    children = workspace.children
    stack_pos = 0
    levels[0] = level + 2  # bytearray cannot store -1/-2, so offset by two.
    children[0] = 0
    while stack_pos >= 0:
        build_level = levels[stack_pos] - 2
        if build_level == -1:
            target[write_pos] = 0
            write_pos += 1
            stack_pos -= 1
        elif build_level == -2:
            target[write_pos] = 1
            write_pos += 1
            stack_pos -= 1
        else:
            child = children[stack_pos]
            repeated_children = counts[build_level]
            if child < repeated_children:
                children[stack_pos] = child + 1
                stack_pos += 1
                levels[stack_pos] = build_level + 1  # (level - 1) + 2
                children[stack_pos] = 0
            elif child == repeated_children and remainders[build_level] != 0:
                children[stack_pos] = child + 1
                stack_pos += 1
                levels[stack_pos] = build_level  # (level - 2) + 2
                children[stack_pos] = 0
            else:
                stack_pos -= 1

    # Preserve the legacy orientation: the first pulse is canonical index 0.
    first_pulse = 0
    while target[first_pulse] == 0:
        first_pulse += 1
    if first_pulse:
        scratch = workspace.scratch
        read_pos = 0
        while read_pos < steps:
            scratch[read_pos] = target[read_pos]
            read_pos += 1
        write_pos = 0
        read_pos = first_pulse
        while write_pos < steps:
            target[write_pos] = scratch[read_pos]
            write_pos += 1
            read_pos += 1
            if read_pos == steps:
                read_pos = 0


# ---------------------------------------------------------------------------
# 语义输入事件
# ---------------------------------------------------------------------------

class KnobTurn:
    __slots__ = ("knob", "value")
    def __init__(self, knob, value):
        self.knob = knob
        self.value = value


class ButtonEvent:
    __slots__ = ("button", "kind")
    def __init__(self, button, kind):
        self.button = button
        self.kind = kind


class ClockEvent:
    __slots__ = ("timestamp_us",)

    def __init__(self, timestamp_us=0):
        self.timestamp_us = timestamp_us


class ClockCapture:
    """Fixed-capacity timestamp ring used between the DIN ISR and main loop."""
    __slots__ = ("_timestamps", "_head", "_tail", "_count", "overflows")

    def __init__(self, capacity=CLOCK_QUEUE_CAPACITY):
        self._timestamps = [0] * capacity
        self._head = 0
        self._tail = 0
        self._count = 0
        self.overflows = 0

    @property
    def capacity(self):
        return len(self._timestamps)

    @property
    def count(self):
        return self._count

    def push(self, timestamp_us):
        if self._count == len(self._timestamps):
            self.overflows += 1
            return False
        self._timestamps[self._tail] = timestamp_us
        self._tail += 1
        if self._tail == len(self._timestamps):
            self._tail = 0
        self._count += 1
        return True

    def pop(self):
        if self._count == 0:
            return None
        timestamp_us = self._timestamps[self._head]
        self._head += 1
        if self._head == len(self._timestamps):
            self._head = 0
        self._count -= 1
        return timestamp_us


# ---------------------------------------------------------------------------
# 参数槽（表驱动 UI：全局参数 / 引擎参数 / 步槽 统一抽象）
# ---------------------------------------------------------------------------

class Param:
    """Static parameter descriptor with a direct, allocation-free setter."""
    __slots__ = ("abbr", "pmin", "pmax", "discrete", "_get", "_set", "_fmt")

    def __init__(self, abbr, pmin, pmax, discrete, get_cur, set_value, fmt):
        self.abbr = abbr
        self.pmin = pmin
        self.pmax = pmax
        self.discrete = discrete
        self._get = get_cur
        self._set = set_value
        self._fmt = fmt

    def _resolve(self, ctx, v):
        return v(ctx) if callable(v) else v

    def get_cur(self, ctx):
        return self._get(ctx)

    def set_value(self, ctx, value):
        self._set(ctx, value)

    def fmt(self, ctx):
        return self._fmt(ctx)

    def edit(self, ctx, p, exec_cmd, app):
        pmin = self._resolve(ctx, self.pmin)
        pmax = self._resolve(ctx, self.pmax)
        cur = self._get(ctx)
        val = round(p * (pmax - pmin)) + pmin
        val = min(max(val, pmin), pmax)
        tol = 0 if self.discrete else max(1, (pmax - pmin) // 32)
        if not app.k2_picked:
            if abs(val - cur) <= tol:
                app.k2_picked = True
            return
        if val == cur:
            return
        exec_cmd(self, ctx, val)


class StepSlot:
    """CV 序列的步槽：编辑第 idx 步的 0~127 值。"""
    __slots__ = ("idx", "_abbr")
    pmin = 0
    pmax = CV_VAL_MAX
    discrete = False

    def __init__(self, idx):
        self.idx = idx
        self._abbr = "S%02d" % (idx + 1)

    @property
    def abbr(self):
        return self._abbr

    def get_cur(self, track):
        return track.engine.values[self.idx]

    def set_value(self, track, value):
        track.engine.set_step(self.idx, value)

    def fmt(self, track):
        # 编辑步时只显示 cv 数值（0~127），不显示电压换算
        return f"{track.engine.values[self.idx]}"

    def edit(self, track, p, exec_cmd, app=None):
        # 编辑步：K2 采用 jump 策略（旋钮位置直接映射步值，无拾取死区）
        val = round(p * CV_VAL_MAX)
        val = min(max(val, 0), CV_VAL_MAX)
        if val == self.get_cur(track):
            return
        exec_cmd(self, track, val)


# ---------------------------------------------------------------------------
# Hardware Adapter —— 唯一允许访问 EuroPi 硬件 API 的模块
# ---------------------------------------------------------------------------

class Hardware:
    def __init__(self):
        self.oled = oled
        self.k1 = k1
        self.k2 = k2
        self.b1 = b1
        self.b2 = b2
        self.cv = [cv1, cv2, cv3, cv4, cv5, cv6]
        self.din = din

        self._btn = {
            "b1": {"state": False, "pending": False, "t": 0},
            "b2": {"state": False, "pending": False, "t": 0},
        }
        self.clock_capture = ClockCapture()
        self.clock_schedule_failures = 0
        self.on_clock_rise(self._push_clock)

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
        return self.k1.percent()

    def knob2(self):
        return self.k2.percent()

    def button1(self):
        return self._debounced("b1", self.b1.value() == HIGH)

    def button2(self):
        return self._debounced("b2", self.b2.value() == HIGH)

    def _push_clock(self, timestamp_us=None):
        if timestamp_us is None:
            timestamp_us = ticks_us()
        self.clock_capture.push(timestamp_us)

    def pop_clock_timestamp(self):
        return self.clock_capture.pop()

    @property
    def clock_overflows(self):
        return self.clock_capture.overflows

    @property
    def dropped_clock_events(self):
        return self.clock_capture.overflows + self.clock_schedule_failures

    def on_clock_rise(self, cb):
        pin = self.din.pin
        def _isr(*_):
            try:
                micropython.schedule(cb, ticks_us())
            except (ValueError, RuntimeError):
                self.clock_schedule_failures += 1
        pin.irq(trigger=pin.IRQ_FALLING, handler=_isr)

    # --- CV 输出落点 ---
    def set_cv(self, idx, voltage):
        if 0 <= idx < len(self.cv):
            self.cv[idx].voltage(voltage)

    def off_cv(self, idx):
        if 0 <= idx < len(self.cv):
            self.cv[idx].off()

    def off_all_cvs(self):
        turn_off_all_cvs()

    # --- 显示 ---
    def display_clear(self):
        self.oled.fill(0)

    def display_show(self):
        self.oled.show()

    def display_show_page(self, page):
        self.oled.show_page(page)

    def display_text(self, s, x, y):
        self.oled.text(s, x, y, 1)

    def display_fill_rect(self, x, y, w, h, c):
        self.oled.fill_rect(x, y, w, h, c)


# ---------------------------------------------------------------------------
# Input Manager
# ---------------------------------------------------------------------------

class InputManager:
    def __init__(self, hw):
        self.hw = hw
        self._b1_down = False
        self._b2_down = False
        self._b1_press_t = 0
        self._b2_press_t = 0
        self._b1_long = False
        self._b2_long = False
        self._knob1 = KnobTurn(1, 0.0)
        self._knob2 = KnobTurn(2, 0.0)
        self._knob1_last = -1.0
        self._knob2_last = -1.0
        self._knob1_active_until = None
        self._knob2_active_until = None
        self._clock_events = [ClockEvent() for _ in range(CLOCK_QUEUE_CAPACITY)]

    def poll(self):
        events = []
        now = ticks_ms()
        d1 = self.hw.button1()
        d2 = self.hw.button2()

        if d1 and d2:
            self._b1_down = d1
            self._b2_down = d2
            return events

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

        v1 = min(max(self.hw.knob1(), 0.0), 1.0)
        k1_active = (
            self._knob1_active_until is not None
            and ticks_diff(now, self._knob1_active_until) < 0
        )
        if not k1_active:
            self._knob1_active_until = None
        k1_threshold = KNOB_ACTIVE_DEADBAND if k1_active else KNOB_DEADBAND
        if self._knob1_last < 0.0 or abs(v1 - self._knob1_last) >= k1_threshold:
            if self._knob1_last >= 0.0:
                self._knob1_active_until = ticks_add(now, KNOB_ACTIVE_TIMEOUT_MS)
            self._knob1_last = v1
            self._knob1.value = v1
            events.append(self._knob1)
        v2 = min(max(self.hw.knob2(), 0.0), 1.0)
        k2_active = (
            self._knob2_active_until is not None
            and ticks_diff(now, self._knob2_active_until) < 0
        )
        if not k2_active:
            self._knob2_active_until = None
        k2_threshold = KNOB_ACTIVE_DEADBAND if k2_active else KNOB_DEADBAND
        if self._knob2_last < 0.0 or abs(v2 - self._knob2_last) >= k2_threshold:
            if self._knob2_last >= 0.0:
                self._knob2_active_until = ticks_add(now, KNOB_ACTIVE_TIMEOUT_MS)
            self._knob2_last = v2
            self._knob2.value = v2
            events.append(self._knob2)

        clock_index = 0
        while clock_index < CLOCK_QUEUE_CAPACITY:
            timestamp_us = self.hw.pop_clock_timestamp()
            if timestamp_us is None:
                break
            clock_event = self._clock_events[clock_index]
            clock_event.timestamp_us = timestamp_us
            events.append(clock_event)
            clock_index += 1
        return events


# ---------------------------------------------------------------------------
# Sequencer Core —— Pattern / TrackEngine / Track / Sequencer（无硬件调用）
# ---------------------------------------------------------------------------

class Pattern:
    steps = 1
    pattern = []

    def regenerate(self):
        raise NotImplementedError

    def output(self, pos):
        raise NotImplementedError

    def to_dict(self):
        raise NotImplementedError

    def from_dict(self, d):
        raise NotImplementedError


class EuclidPattern(Pattern):
    def __init__(self, steps, pulses, rot, prob):
        steps = _clamped_int(steps, MIN_STEPS, MAX_STEPS, MIN_STEPS)
        pulses = _clamped_int(pulses, 0, steps, 0)
        self.rot = _clamped_int(rot, 0, steps, 0)
        self.prob = _clamped_int(prob, 0, 100, 100)
        self._buffers = (bytearray(MAX_STEPS), bytearray(MAX_STEPS))
        self._slot_steps = bytearray(2)
        self._slot_pulses = bytearray(2)
        self._active_index = 0
        self._slot_steps[0] = steps
        self._slot_pulses[0] = pulses
        _generate_euclidean_into(
            self._buffers[0], steps, pulses, _EUCLIDEAN_WORKSPACE
        )

    @property
    def steps(self):
        return self._slot_steps[self._active_index]

    @property
    def pulses(self):
        return self._slot_pulses[self._active_index]

    @property
    def pattern(self):
        """Canonical, unrotated active buffer; consumers should use value_at()."""
        return self._buffers[self._active_index]

    def regenerate(self, steps=None, pulses=None):
        if steps is None:
            steps = self.steps
        if pulses is None:
            pulses = self.pulses
        if steps == self.steps and pulses == self.pulses:
            return False

        inactive = 1 - self._active_index
        _generate_euclidean_into(
            self._buffers[inactive], steps, pulses, _EUCLIDEAN_WORKSPACE
        )
        self._slot_steps[inactive] = steps
        self._slot_pulses[inactive] = pulses
        # Publish metadata and its completed buffer together from readers' view.
        self._active_index = inactive
        return True

    def value_at(self, pos):
        active = self._active_index
        steps = self._slot_steps[active]
        source_pos = pos - self.rot
        if source_pos < 0:
            source_pos += steps
        return self._buffers[active][source_pos]

    def is_on(self, pos):
        return bool(self.value_at(pos))

    def output(self, pos):
        if not self.is_on(pos):
            return False
        if self.prob >= 100:
            return True
        if self.prob <= 0:
            return False
        return random.random() < self.prob / 100.0

    def set_steps(self, steps):
        steps = _clamped_int(steps, MIN_STEPS, MAX_STEPS, self.steps)
        pulses = min(self.pulses, steps)
        # Clamp rotation before publishing a potentially shorter pattern so an
        # interrupting reader always observes an in-range offset.
        if self.rot > steps:
            self.rot = steps
        self.regenerate(steps, pulses)

    def set_pulses(self, pulses):
        pulses = _clamped_int(pulses, 0, self.steps, self.pulses)
        self.regenerate(self.steps, pulses)

    def set_rot(self, rot):
        self.rot = _clamped_int(rot, 0, self.steps, self.rot)

    def set_prob(self, prob):
        self.prob = _clamped_int(prob, 0, 100, self.prob)

    def to_dict(self):
        return {"steps": self.steps, "pulses": self.pulses,
                "rot": self.rot, "prob": self.prob}

    def from_dict(self, d):
        if not isinstance(d, dict) or not d:
            return
        steps = _clamped_int(
            d.get("steps", self.steps), MIN_STEPS, MAX_STEPS, self.steps
        )
        pulses = _clamped_int(d.get("pulses", self.pulses), 0, steps, self.pulses)
        rot = _clamped_int(d.get("rot", self.rot), 0, steps, self.rot)
        prob = _clamped_int(d.get("prob", self.prob), 0, 100, self.prob)
        self.rot = min(rot, self.steps)
        self.regenerate(steps, pulses)
        self.rot = rot
        self.prob = prob


class EuclidRuntime:
    def __init__(self, patterns, merge):
        self.patterns = patterns
        self.merge = merge
        self.positions = [p.steps - 1 for p in patterns]
        self.last_out = False

    def output(self):
        pattern = self.patterns[0]
        position = (self.positions[0] + 1) % pattern.steps
        self.positions[0] = position
        on1 = EuclidPattern.output(pattern, position)

        pattern = self.patterns[1]
        position = (self.positions[1] + 1) % pattern.steps
        self.positions[1] = position
        on2 = EuclidPattern.output(pattern, position)

        out = combine_outputs(on1, on2, self.merge)
        self.last_out = out
        return out

    def reset(self):
        self.positions[0] = self.patterns[0].steps - 1
        self.positions[1] = self.patterns[1].steps - 1
        self.last_out = False


# Backwards-compatible name; runtime state is intentionally not serializable.
TrackPlayer = EuclidRuntime


# ---------------------------------------------------------------------------
# TrackConfig 接口与两种配置
# ---------------------------------------------------------------------------

class TrackConfig:
    TYPE = ""

    def param_defs(self, track):
        raise NotImplementedError

    def step_slots(self, track):
        return 0

    def slots(self, track):
        count = self.step_slots(track)
        if count == 0:
            return self.param_defs(track)
        return self.param_defs(track) + [StepSlot(i) for i in range(count)]

    def to_dict(self):
        raise NotImplementedError

    def from_dict(self, d):
        raise NotImplementedError


# Backwards-compatible name retained for external imports.
TrackEngine = TrackConfig


class EuclidEngine(TrackConfig):
    TYPE = "EUC"

    def __init__(self, g1, g2, merge):
        self.g1 = g1
        self.g2 = g2
        self.patterns = [g1, g2]
        self.merge = merge
        self._params = None

    def set_merge(self, v):
        self.merge = v

    def param_defs(self, track):
        if self._params is not None:
            return self._params
        self._params = [
            Param("ROT1", 0, lambda t: self.g1.steps, False,
                  lambda t: self.g1.rot,
                  lambda t, v: t.set_gen(1, "rot", v),
                  lambda t: self.g1.rot),
            Param("ROT2", 0, lambda t: self.g2.steps, False,
                  lambda t: self.g2.rot,
                  lambda t, v: t.set_gen(2, "rot", v),
                  lambda t: self.g2.rot),
            Param("PLS1", 0, lambda t: self.g1.steps, False,
                  lambda t: self.g1.pulses,
                  lambda t, v: t.set_gen(1, "pulses", v),
                  lambda t: self.g1.pulses),
            Param("PLS2", 0, lambda t: self.g2.steps, False,
                  lambda t: self.g2.pulses,
                  lambda t, v: t.set_gen(2, "pulses", v),
                  lambda t: self.g2.pulses),
            Param("STP1", MIN_STEPS, MAX_STEPS, False,
                  lambda t: self.g1.steps,
                  lambda t, v: t.set_gen(1, "steps", v),
                  lambda t: self.g1.steps),
            Param("STP2", MIN_STEPS, MAX_STEPS, False,
                  lambda t: self.g2.steps,
                  lambda t, v: t.set_gen(2, "steps", v),
                  lambda t: self.g2.steps),
            Param("PRB1", 0, 100, False,
                  lambda t: self.g1.prob,
                  lambda t, v: t.set_gen(1, "prob", v),
                  lambda t: self.g1.prob),
            Param("PRB2", 0, 100, False,
                  lambda t: self.g2.prob,
                  lambda t, v: t.set_gen(2, "prob", v),
                  lambda t: self.g2.prob),
            Param("MERG", 0, 4, True,
                  lambda t: self.merge,
                  lambda t, v: t.set_merge(v),
                  lambda t: MERGE_MODES[self.merge]),
        ]
        return self._params

    def to_dict(self):
        return {"g1": self.g1.to_dict(), "g2": self.g2.to_dict(),
                "merge": self.merge}

    def from_dict(self, d):
        if not isinstance(d, dict) or not d:
            return
        g1 = d.get("g1", {})
        g2 = d.get("g2", {})
        if isinstance(g1, dict):
            self.g1.from_dict(g1)
        if isinstance(g2, dict):
            self.g2.from_dict(g2)
        self.merge = _clamped_int(d.get("merge", self.merge), 0, 4, self.merge)


class CVSeqEngine(TrackConfig):
    TYPE = "CVSEQ"

    def __init__(self):
        self.length = 8
        self.values = [(i * CV_VAL_MAX) // (CV_LEN - 1) for i in range(CV_LEN)]
        self.gate_len = 50   # 门长度（占每步百分比），恒定输出在配对门通道
        self._params = None
        self._slots = None

    def param_defs(self, track):
        if self._params is not None:
            return self._params
        self._params = [
            Param("LEN", 1, CV_LEN, True,
                  lambda t: t.engine.length,
                  lambda t, v: t.engine.set_length(v),
                  lambda t: t.engine.length),
            Param("VLO", CV_MIN, CV_MAX, False,
                  lambda t: t.v_lo,
                  lambda t, v: t.set_v_lo(v),
                  lambda t: f"{t.v_lo}V"),
            Param("VHI", CV_MIN, CV_MAX, False,
                  lambda t: t.v_hi,
                  lambda t, v: t.set_v_hi(v),
                  lambda t: f"{t.v_hi}V"),
            Param("GLEN", CV_GATE_MIN, CV_GATE_MAX, False,
                  lambda t: t.engine.gate_len,
                  lambda t, v: t.engine.set_gate_len(v),
                  lambda t: f"{t.engine.gate_len}%")
        ]
        return self._params

    def slots(self, track):
        if self._slots is None:
            self._slots = self.param_defs(track) + [
                StepSlot(index) for index in range(CV_LEN)
            ]
        return self._slots

    def step_slots(self, track):
        return self.length

    def set_length(self, n):
        # values 固定为 CV_LEN 长度，缩短只改变播放窗口，不破坏已编辑音型
        self.length = _clamped_int(n, 1, CV_LEN, self.length)

    def set_step(self, i, v):
        if 0 <= i < CV_LEN:
            self.values[i] = _clamped_int(v, 0, CV_VAL_MAX, self.values[i])

    def set_gate_len(self, pct):
        self.gate_len = _clamped_int(
            pct, CV_GATE_MIN, CV_GATE_MAX, self.gate_len
        )

    def to_project_dict(self):
        return {"length": self.length,
                "values": list(self.values),
                "gate_len": self.gate_len}

    def from_dict(self, d):
        if not isinstance(d, dict) or not d:
            return
        self.length = _clamped_int(d.get("length", self.length), 1, CV_LEN, self.length)
        vals = d.get("values", [])
        if isinstance(vals, (list, tuple)):
            index = 0
            while index < CV_LEN:
                if index < len(vals):
                    self.values[index] = _clamped_int(
                        vals[index], 0, CV_VAL_MAX, self.values[index]
                    )
                index += 1
        self.gate_len = _clamped_int(
            d.get("gate_len", self.gate_len),
            CV_GATE_MIN,
            CV_GATE_MAX,
            self.gate_len,
        )

    def from_project_dict(self, d):
        self.from_dict(d)


class CVSeqRuntime:
    __slots__ = ("pos", "gate_last")

    def __init__(self):
        self.pos = 0
        self.gate_last = False

    def on_tick(self, config, track, outputs, context):
        self.pos = (self.pos + 1) % config.length
        value = config.values[self.pos]
        voltage = track.v_lo + (track.v_hi - track.v_lo) * value / CV_VAL_MAX
        outputs.set_cv(track.cv_index, voltage)
        if value > 0:
            gate_us = context.period_us * config.gate_len // 100
            outputs.trigger_gate(
                track.gate_index,
                track.v_hi,
                track.v_lo,
                gate_us,
                context.actual_us,
            )
        else:
            outputs.cancel_channel(track.gate_index, track.v_lo)
        self.gate_last = value > 0

    def reset(self):
        self.pos = 0
        self.gate_last = False


# ---------------------------------------------------------------------------
# Track —— 容器：设置 + 引擎实例（常驻）
# ---------------------------------------------------------------------------

class TrackFeature:
    __slots__ = ("type_id", "engine_key", "project_key")

    def __init__(self, type_id, engine_key=None, project_key=None):
        self.type_id = type_id
        self.engine_key = engine_key
        self.project_key = project_key

    def create_engine(self, track, g1, g2, merge):
        return None

    def create_runtime(self, track, engine):
        return engine

    def engine(self, track):
        if self.engine_key is None:
            return None
        return track.engines[self.engine_key]

    def runtime(self, track):
        if self.engine_key is None:
            return None
        return track.runtimes[self.engine_key]

    def slots(self, track):
        engine = self.engine(track)
        return () if engine is None else engine.slots(track)

    def slot_count(self, track):
        return len(self.slots(track))

    def reset(self, track):
        runtime = self.runtime(track)
        if runtime is not None:
            runtime.reset()

    def on_tick(self, track, outputs, context):
        raise NotImplementedError

    def update(self, track, outputs, now_us, context):
        pass

    def write_snapshot(self, track, app, transport, snapshot):
        raise NotImplementedError

    def render(self, snapshot, hw):
        raise NotImplementedError

    def render_dynamic(self, snapshot, hw):
        self.render(snapshot, hw)

    def encode_project(self, track):
        return None

    def decode_project(self, track, data, project=False):
        pass


class NullTrackFeature(TrackFeature):
    def __init__(self):
        super().__init__("OFF")

    def on_tick(self, track, outputs, context):
        outputs.cancel_channel(track.cv_index, 0)
        outputs.cancel_channel(track.gate_index, 0)

    def write_snapshot(self, track, app, transport, snapshot):
        pass

    def render(self, snapshot, hw):
        hw.display_text("OFF", 52, 12)


class EuclidTrackFeature(TrackFeature):
    def __init__(self):
        super().__init__("EUC", "EUC", "EUC")

    def create_engine(self, track, g1, g2, merge):
        return EuclidEngine(g1, g2, merge)

    def create_runtime(self, track, engine):
        return EuclidRuntime(engine.patterns, engine.merge)

    def on_tick(self, track, outputs, context):
        runtime = track.runtimes["EUC"]
        on = EuclidRuntime.output(runtime)
        if on:
            outputs.trigger_gate(
                track.cv_index,
                track.v_hi,
                track.v_lo,
                GATE_MS * 1000,
                context.actual_us,
            )
        else:
            outputs.cancel_channel(track.cv_index, track.v_lo)
        outputs.trigger_gate(
            track.gate_index,
            track.v_hi,
            track.v_lo,
            GATE_MS * 1000,
            context.actual_us,
        )

    def write_snapshot(self, track, app, transport, snapshot):
        engine = track.engines["EUC"]
        current = transport.beat_index - 1 if transport.beat_index > 0 else 0
        window_start = (current // 16) * 16
        column = 0
        while column < 16:
            snapshot.sequence_a[column] = engine.g1.is_on(
                (window_start + column) % engine.g1.steps
            )
            snapshot.sequence_b[column] = engine.g2.is_on(
                (window_start + column) % engine.g2.steps
            )
            column += 1
        snapshot.playhead = current % 16
        snapshot.last_output = track.runtimes["EUC"].last_out

    def render(self, snapshot, hw):
        row = 0
        while row < 2:
            bits = snapshot.sequence_a if row == 0 else snapshot.sequence_b
            y = 8 if row == 0 else 16
            column = 0
            while column < 16:
                if bits[column]:
                    hw.display_fill_rect(column * 8, y, 6, 6, 1)
                else:
                    hw.display_fill_rect(column * 8 + 2, y + 2, 2, 2, 1)
                column += 1
            row += 1
        if snapshot.last_output:
            hw.display_fill_rect(snapshot.playhead * 8, 26, 6, 6, 1)
        else:
            hw.display_fill_rect(snapshot.playhead * 8 + 2, 28, 2, 2, 1)

    def render_dynamic(self, snapshot, hw):
        hw.display_fill_rect(0, 26, OLED_WIDTH, 6, 0)
        if snapshot.last_output:
            hw.display_fill_rect(snapshot.playhead * 8, 26, 6, 6, 1)
        else:
            hw.display_fill_rect(snapshot.playhead * 8 + 2, 28, 2, 2, 1)

    def encode_project(self, track):
        return track.engines["EUC"].to_dict()

    def decode_project(self, track, data, project=False):
        engine = track.engines["EUC"]
        engine.from_dict(data)
        runtime = track.runtimes["EUC"]
        runtime.merge = engine.merge
        runtime.reset()


class CVTrackFeature(TrackFeature):
    def __init__(self):
        super().__init__("CV", "CVSEQ", "CVSEQ")

    def create_engine(self, track, g1, g2, merge):
        return CVSeqEngine()

    def create_runtime(self, track, engine):
        return CVSeqRuntime()

    def slot_count(self, track):
        engine = track.engines["CVSEQ"]
        return len(engine.param_defs(track)) + engine.length

    def on_tick(self, track, outputs, context):
        CVSeqRuntime.on_tick(
            track.runtimes["CVSEQ"],
            track.engines["CVSEQ"],
            track,
            outputs,
            context,
        )

    def write_snapshot(self, track, app, transport, snapshot):
        engine = track.engines["CVSEQ"]
        index = 0
        while index < CV_LEN:
            snapshot.cv_values[index] = engine.values[index]
            index += 1
        snapshot.cv_length = engine.length
        snapshot.cv_position = track.runtimes["CVSEQ"].pos
        selected = app.sel - len(engine.param_defs(track))
        snapshot.selected_step = selected if 0 <= selected < engine.length else -1

    def render(self, snapshot, hw):
        column = 0
        while column < CV_LEN:
            if column < snapshot.cv_length:
                height = round(snapshot.cv_values[column] / CV_VAL_MAX * 16)
                if height > 0:
                    hw.display_fill_rect(column * 8, 25 - height, 6, height, 1)
            else:
                hw.display_fill_rect(column * 8 + 2, 25, 2, 1, 1)
            column += 1
        if snapshot.selected_step >= 0:
            hw.display_fill_rect(snapshot.selected_step * 8, 29, 6, 1, 1)
        if 0 <= snapshot.cv_position < CV_LEN:
            hw.display_fill_rect(snapshot.cv_position * 8, 27, 6, 3, 1)

    def render_dynamic(self, snapshot, hw):
        hw.display_fill_rect(0, 27, OLED_WIDTH, 3, 0)
        if snapshot.selected_step >= 0:
            hw.display_fill_rect(snapshot.selected_step * 8, 29, 6, 1, 1)
        if 0 <= snapshot.cv_position < CV_LEN:
            hw.display_fill_rect(snapshot.cv_position * 8, 27, 6, 3, 1)

    def encode_project(self, track):
        return track.engines["CVSEQ"].to_project_dict()

    def decode_project(self, track, data, project=False):
        engine = track.engines["CVSEQ"]
        engine.from_dict(data)
        runtime = track.runtimes["CVSEQ"]
        if project:
            runtime.reset()
        else:
            runtime.pos = _clamped_int(
                data.get("pos", 0), 0, engine.length - 1, 0
            )
            runtime.gate_last = False


TRACK_FEATURES = {}


def register_track_feature(feature):
    TRACK_FEATURES[feature.type_id] = feature


register_track_feature(NullTrackFeature())
register_track_feature(EuclidTrackFeature())
register_track_feature(CVTrackFeature())


class Track:
    def __init__(self, idx, g1, g2, merge):
        self.index = idx
        self.v_lo = 0
        self.v_hi = 5
        self.type = "EUC"
        # 固定配对：主通道 = cv(idx)，门/钟通道 = cv(idx+3)
        self.cv_index = idx
        self.gate_index = idx + 3
        self.engines = {}
        for feature in TRACK_FEATURES.values():
            engine = feature.create_engine(self, g1, g2, merge)
            if engine is not None:
                self.engines[feature.engine_key] = engine
        self.runtimes = {}
        for feature in TRACK_FEATURES.values():
            engine = self.engines.get(feature.engine_key)
            runtime = feature.create_runtime(self, engine)
            if runtime is not None:
                self.runtimes[feature.engine_key] = runtime
        # Allocate all built-in descriptors and step slots before playback.
        for feature in TRACK_FEATURES.values():
            feature.slots(self)
        self._feature = TRACK_FEATURES[self.type]
        self._on_tick = self._feature.on_tick
        self._update = self._feature.update

    @property
    def engine(self):
        return self.feature.engine(self)

    @property
    def feature(self):
        return self._feature

    def set_type(self, typ):
        if typ not in TRACK_FEATURES:
            return
        self.type = typ
        self._feature = TRACK_FEATURES[typ]
        self._on_tick = self._feature.on_tick
        self._update = self._feature.update
        self._feature.reset(self)  # 播放头归零，与其他轨对齐

    def set_v_lo(self, v):
        v = min(max(v, CV_MIN), CV_MAX)
        if v > self.v_hi:
            self.v_hi = v
        self.v_lo = v

    def set_v_hi(self, v):
        v = min(max(v, CV_MIN), CV_MAX)
        if v < self.v_lo:
            self.v_lo = v
        self.v_hi = v

    def set_gen(self, which, attr, val):
        g = self.engines["EUC"].g1 if which == 1 else self.engines["EUC"].g2
        if attr == "steps":
            g.set_steps(val)
        elif attr == "pulses":
            g.set_pulses(val)
        elif attr == "rot":
            g.set_rot(val)
        elif attr == "prob":
            g.set_prob(val)

    def set_merge(self, val):
        self.engines["EUC"].set_merge(val)
        self.runtimes["EUC"].merge = val

    def to_dict(self):
        cvseq = self.engines["CVSEQ"].to_project_dict()
        cvseq["pos"] = self.runtimes["CVSEQ"].pos
        return {
            "type": self.type,
            "v_lo": self.v_lo,
            "v_hi": self.v_hi,
            "EUC": self.engines["EUC"].to_dict(),
            "CVSEQ": cvseq,
        }

    def to_project_dict(self):
        data = {
            "type": self.type,
            "v_lo": self.v_lo,
            "v_hi": self.v_hi,
        }
        for feature in TRACK_FEATURES.values():
            if feature.project_key is not None:
                feature_data = feature.encode_project(self)
                if feature_data is not None:
                    data[feature.project_key] = feature_data
        return data

    def from_dict(self, d):
        self._load_dict(d, False)

    def _load_dict(self, d, project):
        if not isinstance(d, dict) or not d:
            return
        track_type = d.get("type", self.type)
        if track_type in TYPE_NAMES:
            self.type = track_type
        elif "type" in d:
            self.type = "EUC"
        self._feature = TRACK_FEATURES[self.type]
        self._on_tick = self._feature.on_tick
        self._update = self._feature.update
        low = _clamped_int(d.get("v_lo", self.v_lo), CV_MIN, CV_MAX, self.v_lo)
        high = _clamped_int(d.get("v_hi", self.v_hi), CV_MIN, CV_MAX, self.v_hi)
        self.v_lo = min(low, high)
        self.v_hi = max(low, high)
        for feature in TRACK_FEATURES.values():
            if feature.project_key is None:
                continue
            feature_data = d.get(feature.project_key, {})
            if isinstance(feature_data, dict):
                feature.decode_project(self, feature_data, project)

    def from_project_dict(self, d):
        self._load_dict(d, True)
        for feature in TRACK_FEATURES.values():
            feature.reset(self)


# ---------------------------------------------------------------------------
# OutputScheduler + Sequencer
# ---------------------------------------------------------------------------

class OutputScheduler:
    """Fixed-size CV state and one replaceable gate deadline per channel."""
    __slots__ = (
        "_desired",
        "_applied",
        "_pending",
        "_gate_active",
        "_gate_low",
        "_gate_deadline",
        "late_edges",
        "last_late_edge_us",
        "max_late_edge_us",
    )

    def __init__(self, channel_count=6):
        self._desired = [0] * channel_count
        self._applied = [None] * channel_count
        self._pending = bytearray(channel_count)
        self._gate_active = bytearray(channel_count)
        self._gate_low = [0] * channel_count
        self._gate_deadline = [0] * channel_count
        self.late_edges = 0
        self.last_late_edge_us = 0
        self.max_late_edge_us = 0

    def _queue_voltage(self, channel, voltage):
        self._desired[channel] = voltage
        self._pending[channel] = self._applied[channel] != voltage

    def set_cv(self, channel, voltage):
        self._queue_voltage(channel, voltage)

    def trigger_gate(self, channel, high, low, length_us, now_us):
        self._gate_low[channel] = low
        self._gate_deadline[channel] = ticks_add(now_us, length_us)
        self._gate_active[channel] = 1
        self._queue_voltage(channel, high)

    def cancel_channel(self, channel, voltage=0):
        self._gate_active[channel] = 0
        self._queue_voltage(channel, voltage)

    def cancel_all(self, voltage=0):
        channel = 0
        while channel < len(self._desired):
            self.cancel_channel(channel, voltage)
            channel += 1

    def update(self, now_us):
        changed = False
        channel = 0
        while channel < len(self._desired):
            if self._gate_active[channel]:
                lateness = ticks_diff(now_us, self._gate_deadline[channel])
                if lateness >= 0:
                    self._gate_active[channel] = 0
                    self._queue_voltage(channel, self._gate_low[channel])
                    if lateness > 0:
                        self.late_edges += 1
                        self.last_late_edge_us = lateness
                        if lateness > self.max_late_edge_us:
                            self.max_late_edge_us = lateness
                    changed = True
            channel += 1
        return changed

    def time_to_next_edge_us(self, now_us):
        """Return the nearest pending gate edge, or None when none is active."""
        nearest = None
        channel = 0
        while channel < len(self._desired):
            if self._gate_active[channel]:
                gap = ticks_diff(self._gate_deadline[channel], now_us)
                if gap < 0:
                    gap = 0
                if nearest is None or gap < nearest:
                    nearest = gap
            channel += 1
        return nearest

    def flush(self, write_cv):
        wrote = False
        channel = 0
        while channel < len(self._desired):
            if self._pending[channel]:
                voltage = self._desired[channel]
                write_cv(channel, voltage)
                self._applied[channel] = voltage
                self._pending[channel] = 0
                wrote = True
            channel += 1
        return wrote

    def metrics(self):
        return {
            "late_edges": self.late_edges,
            "last_late_edge_us": self.last_late_edge_us,
            "max_late_edge_us": self.max_late_edge_us,
        }


class TickContext:
    """Reusable timing snapshot passed through the complete tick path."""
    __slots__ = (
        "tick_index",
        "scheduled_us",
        "actual_us",
        "period_us",
        "transport",
    )

    def __init__(self, transport):
        self.tick_index = 0
        self.scheduled_us = 0
        self.actual_us = 0
        self.period_us = 1
        self.transport = transport

    def write(self, tick_index, scheduled_us, actual_us, period_us):
        self.tick_index = tick_index
        self.scheduled_us = scheduled_us
        self.actual_us = actual_us
        self.period_us = period_us


class SequencerRuntime:
    def __init__(self):
        self.tracks = []
        self.outputs = OutputScheduler(6)
        self._track_types = [None] * NUM_TRACKS
        self._global_params = None

    def add_track(self, track):
        self.tracks.append(track)
        self._track_types[track.index] = track.type

    def sync_track_types(self):
        for track in self.tracks:
            if self._track_types[track.index] != track.type:
                self.outputs.cancel_channel(track.cv_index, 0)
                self.outputs.cancel_channel(track.gate_index, 0)
                self._track_types[track.index] = track.type

    def cancel_all(self):
        self.outputs.cancel_all(0)

    def reset(self):
        for track in self.tracks:
            for feature in TRACK_FEATURES.values():
                feature.reset(track)
        self.cancel_all()

    def tick(self, context):
        self.sync_track_types()
        for t in self.tracks:
            t._on_tick(t, self.outputs, context)

    def update(self, now_us, context):
        for track in self.tracks:
            track._update(track, self.outputs, now_us, context)

    def pump(self, now_us):
        return self.outputs.update(now_us)

    def time_to_next_output_edge_us(self, now_us):
        return self.outputs.time_to_next_edge_us(now_us)

    def flush_outputs(self, write_cv):
        return self.outputs.flush(write_cv)


# Backwards-compatible public name used by existing imports.
Sequencer = SequencerRuntime


# ---------------------------------------------------------------------------
# ClockService + TransportRuntime —— 调度与音乐播放状态分离
# ---------------------------------------------------------------------------

class ClockService:
    """Absolute-deadline internal clock with bounded catch-up."""
    __slots__ = (
        "running",
        "next_deadline_us",
        "max_catch_up",
        "overruns",
        "dropped_ticks",
        "last_dropped_ticks",
        "last_lateness_us",
        "max_lateness_us",
    )

    def __init__(self, max_catch_up=MAX_CLOCK_CATCH_UP):
        self.running = False
        self.next_deadline_us = 0
        self.max_catch_up = max_catch_up
        self.overruns = 0
        self.dropped_ticks = 0
        self.last_dropped_ticks = 0
        self.last_lateness_us = 0
        self.max_lateness_us = 0

    def start(self, now_us, period_us):
        self.next_deadline_us = ticks_add(now_us, period_us)
        self.running = True

    def stop(self):
        self.running = False

    def update(self, now_us, period_us, on_tick):
        self.last_dropped_ticks = 0
        if not self.running or on_tick is None:
            return 0

        emitted = 0
        while (
            ticks_diff(now_us, self.next_deadline_us) >= 0
            and emitted < self.max_catch_up
        ):
            scheduled_us = self.next_deadline_us
            lateness = ticks_diff(now_us, scheduled_us)
            self.last_lateness_us = lateness
            if lateness > self.max_lateness_us:
                self.max_lateness_us = lateness
            self.next_deadline_us = ticks_add(self.next_deadline_us, period_us)
            on_tick(scheduled_us, now_us, period_us)
            emitted += 1
            if not self.running:
                return emitted

        if ticks_diff(now_us, self.next_deadline_us) >= 0:
            missed = ticks_diff(now_us, self.next_deadline_us) // period_us + 1
            self.next_deadline_us = ticks_add(
                self.next_deadline_us, missed * period_us
            )
            self.overruns += 1
            self.dropped_ticks += missed
            self.last_dropped_ticks = missed
        return emitted

    def metrics(self):
        return {
            "overruns": self.overruns,
            "dropped_ticks": self.dropped_ticks,
            "last_lateness_us": self.last_lateness_us,
            "max_lateness_us": self.max_lateness_us,
        }


class TransportRuntime:
    def __init__(self, on_tick):
        self._on_tick = on_tick
        self._on_stop = None
        self._on_reset = None
        self.clock = ClockService()
        self._clock_tick = self._emit_internal_base_tick
        self.bpm = 120
        self.mul = 4
        self.source = "INT"
        self.beat_index = 0
        self.base_tick_index = 0
        self.internal_phase = 0
        self.dropped_music_steps = 0
        self.tick_context = TickContext(self)
        self.external_ticks = 0
        self.last_external_us = 0
        self.last_external_interval_us = 0
        self.external_jitter_us = 0
        self.max_external_jitter_us = 0
        self.external_timeouts = 0
        self.external_timed_out = False

    @property
    def running(self):
        return self.clock.running

    @running.setter
    def running(self, running):
        self.clock.running = running

    @property
    def next_clock_us(self):
        return self.clock.next_deadline_us

    @next_clock_us.setter
    def next_clock_us(self, deadline_us):
        self.clock.next_deadline_us = deadline_us

    @property
    def max_catch_up(self):
        return self.clock.max_catch_up

    @max_catch_up.setter
    def max_catch_up(self, count):
        self.clock.max_catch_up = count

    @property
    def overruns(self):
        return self.clock.overruns

    @property
    def dropped_ticks(self):
        return self.clock.dropped_ticks

    @property
    def last_lateness_us(self):
        return self.clock.last_lateness_us

    @property
    def max_lateness_us(self):
        return self.clock.max_lateness_us

    def set_on_tick(self, cb):
        self._on_tick = cb

    def set_runtime_callbacks(self, on_stop=None, on_reset=None):
        self._on_stop = on_stop
        self._on_reset = on_reset

    def beat_us(self):
        eff = self.bpm * self.mul
        return max(1, 60_000_000 // eff)

    def base_tick_us(self):
        return max(1, 60_000_000 // (self.bpm * INTERNAL_PPQN))

    def _reset_internal_phase(self):
        self.internal_phase = 0

    def _next_music_step_ticks(self):
        remaining = INTERNAL_PPQN - self.internal_phase
        return (remaining + self.mul - 1) // self.mul

    def start(self):
        if self._on_reset is not None:
            self._on_reset()
        self._reset_internal_phase()
        self.base_tick_index = 0
        self.clock.start(ticks_us(), self.base_tick_us())
        self.beat_index = 0

    def continue_(self):
        if self.source != "INT" or self.running:
            return
        self._reset_internal_phase()
        self.clock.start(ticks_us(), self.base_tick_us())

    def stop(self):
        self.clock.stop()
        if self._on_stop is not None:
            self._on_stop()

    def reset(self):
        self.beat_index = 0
        self.base_tick_index = 0
        self._reset_internal_phase()
        if self._on_reset is not None:
            self._on_reset()
        if self.source == "INT" and self.running:
            self.clock.start(ticks_us(), self.base_tick_us())

    def toggle_internal_clock(self):
        if self.source != "INT":
            return
        if self.running:
            self.stop()
        else:
            self.start()

    def update(self, now_us):
        if self.source == "EXT":
            self._check_external_timeout(now_us)
            return 0
        if not self.running:
            return 0
        beat_index = self.beat_index
        self.clock.update(now_us, self.base_tick_us(), self._clock_tick)
        if self.clock.last_dropped_ticks:
            self._advance_dropped_base_ticks(self.clock.last_dropped_ticks)
        return self.beat_index - beat_index

    def _advance_dropped_base_ticks(self, count):
        phase = self.internal_phase + count * self.mul
        self.dropped_music_steps += phase // INTERNAL_PPQN
        self.internal_phase = phase % INTERNAL_PPQN

    def _emit_internal_base_tick(self, scheduled_us, actual_us, period_us):
        self.base_tick_index += 1
        self.internal_phase += self.mul
        if self.internal_phase < INTERNAL_PPQN:
            return
        self.internal_phase -= INTERNAL_PPQN
        if self._on_tick is None:
            return
        step_period_us = self._next_music_step_ticks() * period_us
        self.tick_context.write(
            self.beat_index, scheduled_us, actual_us, step_period_us
        )
        self._on_tick(self.tick_context)
        self.beat_index += 1

    def time_to_next_clock_us(self, now_us):
        if self.source != "INT" or not self.running:
            return None
        return ticks_diff(self.clock.next_deadline_us, now_us)

    def time_to_next_step_us(self, now_us):
        if self.source != "INT" or not self.running:
            return None
        step_deadline_us = ticks_add(
            self.clock.next_deadline_us,
            (self._next_music_step_ticks() - 1) * self.base_tick_us(),
        )
        return ticks_diff(step_deadline_us, now_us)

    def ext_tick(self, t=None):
        if self.source == "EXT" and self._on_tick is not None:
            if t is None:
                t = ticks_us()
            interval_us = 0
            if self.external_ticks and not self.external_timed_out:
                interval_us = ticks_diff(t, self.last_external_us)
                if self.last_external_interval_us:
                    jitter_us = abs(interval_us - self.last_external_interval_us)
                    self.external_jitter_us = jitter_us
                    if jitter_us > self.max_external_jitter_us:
                        self.max_external_jitter_us = jitter_us
                self.last_external_interval_us = interval_us
            elif self.external_timed_out:
                self.last_external_interval_us = 0
                self.external_jitter_us = 0
                self.external_timed_out = False
            self.last_external_us = t
            self.external_ticks += 1
            actual_us = ticks_us()
            period_us = interval_us if interval_us > 0 else self.beat_us()
            self.tick_context.write(self.beat_index, t, actual_us, period_us)
            self._on_tick(self.tick_context)
            self.beat_index += 1

    def _check_external_timeout(self, now_us):
        if (
            self.source == "EXT"
            and not self.external_timed_out
            and self.last_external_interval_us > 0
            and ticks_diff(now_us, self.last_external_us)
            >= self.last_external_interval_us * EXTERNAL_TIMEOUT_PERIODS
        ):
            self.external_timed_out = True
            self.external_timeouts += 1
            return True
        return False

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
        if self.source == "INT" and self.running:
            self._reset_internal_phase()
            self.clock.start(ticks_us(), self.base_tick_us())

    def set_mul(self, mul):
        mul = min(max(mul, MIN_MUL), MAX_MUL)
        if mul == self.mul:
            return
        self.mul = mul
        if self.source == "INT" and self.running:
            self._reset_internal_phase()
            self.clock.start(ticks_us(), self.base_tick_us())

    def to_dict(self):
        return {"source": self.source, "bpm": self.bpm, "mul": self.mul}

    def metrics(self):
        metrics = self.clock.metrics()
        metrics.update({
            "internal_ppqn": INTERNAL_PPQN,
            "internal_base_ticks": self.base_tick_index,
            "internal_phase": self.internal_phase,
            "dropped_music_steps": self.dropped_music_steps,
            "external_ticks": self.external_ticks,
            "external_interval_us": self.last_external_interval_us,
            "external_jitter_us": self.external_jitter_us,
            "max_external_jitter_us": self.max_external_jitter_us,
            "external_timeouts": self.external_timeouts,
        })
        return metrics

    def from_dict(self, d):
        if not isinstance(d, dict) or not d:
            return
        source = d.get("source", self.source)
        if source in ("INT", "EXT"):
            self.source = source
        self.bpm = _clamped_int(d.get("bpm", self.bpm), MIN_BPM, MAX_BPM, self.bpm)
        self.mul = _clamped_int(d.get("mul", self.mul), MIN_MUL, MAX_MUL, self.mul)
        self.base_tick_index = 0
        self._reset_internal_phase()


# Backwards-compatible public name used by existing scripts and tests.
Transport = TransportRuntime


class RuntimeState:
    """Non-persistent runtime aggregate for transport, players and outputs."""
    __slots__ = ("transport", "sequencer")

    def __init__(self, transport, sequencer):
        self.transport = transport
        self.sequencer = sequencer

    def reset_after_load(self):
        self.transport.stop()
        self.transport.beat_index = 0
        self.transport.base_tick_index = 0
        self.transport._reset_internal_phase()
        self.sequencer.reset()
        self.sequencer.sync_track_types()


class ProjectState:
    """Versioned, validated project data; runtime positions are excluded."""
    __slots__ = ("clock", "tracks", "source_version", "load_errors")

    def __init__(self, clock, tracks, source_version=STATE_SCHEMA_VERSION):
        self.clock = clock
        self.tracks = tracks
        self.source_version = source_version
        self.load_errors = 0

    @classmethod
    def capture(cls, transport, tracks):
        return cls(
            transport.to_dict(),
            [track.to_project_dict() for track in tracks],
        )

    @classmethod
    def decode(cls, state):
        if not isinstance(state, dict):
            return None
        version = state.get("schema_version", 1)
        if version not in (1, STATE_SCHEMA_VERSION):
            return None
        clock = state.get("clock", {})
        tracks = state.get("tracks", [])
        if not isinstance(clock, dict):
            clock = {}
        if not isinstance(tracks, list):
            tracks = []
        return cls(clock, tracks, version)

    def apply(self, transport, sequencer):
        transport.from_dict(self.clock)
        index = 0
        while index < len(sequencer.tracks) and index < len(self.tracks):
            track_data = self.tracks[index]
            if isinstance(track_data, dict):
                track_type = track_data.get("type")
                if track_type is not None and track_type not in TYPE_NAMES:
                    self.load_errors += 1
                try:
                    sequencer.tracks[index].from_project_dict(track_data)
                except (TypeError, ValueError, KeyError, IndexError):
                    self.load_errors += 1
            else:
                self.load_errors += 1
            index += 1
        RuntimeState(transport, sequencer).reset_after_load()

    def to_dict(self):
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "clock": self.clock,
            "tracks": self.tracks,
        }


class PersistenceService:
    """Separates project serialization from the EuroPiScript storage API."""
    __slots__ = ("_storage", "_pending")

    def __init__(self, storage):
        self._storage = storage
        self._pending = None

    def load(self):
        return ProjectState.decode(self._storage.load_state_json())

    def save(self, project):
        self._storage.save_state_json(project.to_dict())
        return True

    def request(self, project):
        self._pending = project
        return "pending"

    @property
    def pending(self):
        return self._pending is not None

    def flush(self):
        if self._pending is None:
            return None
        project = self._pending
        self._pending = None
        try:
            return self.save(project)
        except Exception as error:
            print("Seq2: failed to save state:", error)
            return False


# ---------------------------------------------------------------------------
# Application State —— 仅 UI 状态
# ---------------------------------------------------------------------------

class UiState:
    def __init__(self):
        self.page = 0
        self.sel = 0
        self.k2_picked = False
        self.dirty = True
        self.redraw_all = True
        self.notice = None
        self.notice_until_ms = 0

    def show_notice(self, text, duration_ms):
        self.notice = text
        self.notice_until_ms = ticks_add(ticks_ms(), duration_ms)
        self.dirty = True
        self.redraw_all = True

    def expire_notice(self, now_ms):
        if self.notice is not None and ticks_diff(now_ms, self.notice_until_ms) >= 0:
            self.notice = None
            self.dirty = True
            self.redraw_all = True

    def _set_page(self, p):
        self.page = p
        self.sel = 0
        self.k2_picked = False
        self.dirty = True
        self.redraw_all = True

    def prev_page(self):
        self._set_page((self.page - 1) % NUM_PAGES)

    def next_page(self):
        self._set_page((self.page + 1) % NUM_PAGES)

    def goto_global(self):
        self._set_page(0)


# Backwards-compatible public name while callers migrate to UiState.
AppState = UiState


# ---------------------------------------------------------------------------
# ViewSnapshot + Renderer —— 快照写入与无模型渲染
# ---------------------------------------------------------------------------

class ViewSnapshot:
    __slots__ = (
        "page_label",
        "header_value",
        "notice",
        "feature",
        "sequence_a",
        "sequence_b",
        "playhead",
        "last_output",
        "cv_values",
        "cv_length",
        "cv_position",
        "selected_step",
        "redraw_all",
        "_header_slot",
        "_header_value",
    )

    def __init__(self):
        self.page_label = PAGE_LABELS[0]
        self.header_value = ""
        self.notice = None
        self.feature = None
        self.sequence_a = bytearray(16)
        self.sequence_b = bytearray(16)
        self.playhead = 0
        self.last_output = False
        self.cv_values = bytearray(CV_LEN)
        self.cv_length = 0
        self.cv_position = -1
        self.selected_step = -1
        self.redraw_all = True
        self._header_slot = None
        self._header_value = None

    def write(self, app, sequencer, transport):
        self.redraw_all = app.redraw_all
        self.page_label = PAGE_LABELS[app.page]
        self.notice = app.notice
        if app.page == 0:
            slot = build_global_params(sequencer, transport, app)[app.sel]
            value = slot.get_cur(transport)
            if slot is not self._header_slot or value != self._header_value:
                self.header_value = "%s:%s" % (slot.abbr, slot.fmt(transport))
                self._header_slot = slot
                self._header_value = value
            self.feature = None
            return

        track = sequencer.tracks[app.page - 1]
        self.feature = track.feature
        slots = self.feature.slots(track)
        if slots:
            slot = slots[app.sel]
            value = slot.get_cur(track)
            if slot is not self._header_slot or value != self._header_value:
                self.header_value = "%s:%s" % (slot.abbr, slot.fmt(track))
                self._header_slot = slot
                self._header_value = value
        else:
            self.header_value = "OFF"
            self._header_slot = None
            self._header_value = None
        self.feature.write_snapshot(track, app, transport, self)


class Renderer:
    def draw(self, snapshot, hw):
        if not snapshot.redraw_all:
            if snapshot.feature is not None:
                snapshot.feature.render_dynamic(snapshot, hw)
            return
        hw.display_clear()
        hw.display_text(
            snapshot.page_label,
            OLED_WIDTH - len(snapshot.page_label) * 8,
            0,
        )
        hw.display_text(snapshot.header_value, 0, 0)
        if snapshot.feature is not None:
            snapshot.feature.render(snapshot, hw)
        if snapshot.notice is not None:
            self._draw_notice(snapshot.notice, hw)

    def show_page(self, page, hw):
        hw.display_show_page(page)

    def render(self, snapshot, hw):
        """Draw and submit a full frame for compatibility outside Controller."""
        self.draw(snapshot, hw)
        hw.display_show()

    def _draw_notice(self, text, hw):
        hw.display_fill_rect(0, 0, OLED_WIDTH, CHAR_HEIGHT, 0)
        hw.display_text(text, 0, 0)


# ---------------------------------------------------------------------------
# UI Pages —— 把交互译为 descriptor 编辑（槽位模型，表驱动）
# ---------------------------------------------------------------------------

def build_global_params(seq, transport, app):
    """P0 全局参数：CLK / BPM / MUL / T1..T3。"""
    if seq._global_params is not None:
        return seq._global_params
    params = [
        Param("CLK", 0, 1, True,
              lambda t: 0 if t.source == "INT" else 1,
              lambda t, v: t.set_source("EXT" if v else "INT"),
              lambda t: t.source),
        Param("BPM", MIN_BPM, MAX_BPM, False,
              lambda t: t.bpm,
              lambda t, v: t.set_bpm(v),
              lambda t: t.bpm),
        Param("MUL", MIN_MUL, MAX_MUL, False,
              lambda t: t.mul,
              lambda t, v: t.set_mul(v),
              lambda t: t.mul),
    ]
    for i in range(NUM_TRACKS):
        tid = i
        params.append(
            Param(f"T{i + 1}", 0, len(TYPE_NAMES) - 1, True,
                  lambda tr, tid=tid: TYPE_NAMES.index(seq.tracks[tid].type),
                  lambda tr, v, tid=tid: seq.tracks[tid].set_type(TYPE_NAMES[v]),
                  lambda tr, tid=tid: seq.tracks[tid].type)
        )
    seq._global_params = params
    return seq._global_params


class Pages:
    def __init__(self, app, transport, seq, exec_cmd):
        self.app = app
        self.transport = transport
        self.seq = seq
        self._exec = exec_cmd
        build_global_params(seq, transport, app)

    def set_exec(self, exec_cmd):
        self._exec = exec_cmd

    def _slot_count(self):
        if self.app.page == 0:
            return len(build_global_params(self.seq, self.transport, self.app))
        track = self.seq.tracks[self.app.page - 1]
        return track.feature.slot_count(track)

    def on_knob1(self, p):
        n = self._slot_count()
        if n == 0:
            return
        idx = int(p * n)
        if idx >= n:
            idx = n - 1
        if idx != self.app.sel:
            self.app.sel = idx
            self.app.k2_picked = False
            self.app.dirty = True
            self.app.redraw_all = True

    def on_knob2(self, p):
        if self.app.page == 0:
            ctx = self.transport
            slots = build_global_params(self.seq, self.transport, self.app)
        else:
            track = self.seq.tracks[self.app.page - 1]
            ctx = track
            slots = track.feature.slots(track)
        if not slots:
            return
        slots[self.app.sel].edit(ctx, p, self._exec, self.app)


# ---------------------------------------------------------------------------
# EditorService + Controller
# ---------------------------------------------------------------------------

class EditorService:
    """Applies descriptor edits and centralizes runtime side effects."""
    __slots__ = (
        "app",
        "transport",
        "sequencer",
        "_on_changed",
        "_flush_outputs",
    )

    def __init__(self, app, transport, sequencer, on_changed, flush_outputs):
        self.app = app
        self.transport = transport
        self.sequencer = sequencer
        self._on_changed = on_changed
        self._flush_outputs = flush_outputs

    def set(self, slot, ctx, value):
        was_running = self.transport.running
        slot.set_value(ctx, value)
        if slot.abbr.startswith("T"):
            self.app.k2_picked = False
        if self.sequencer is not None:
            self.sequencer.sync_track_types()
            if was_running and not self.transport.running:
                self.sequencer.cancel_all()
        self._flush_outputs()
        self._on_changed()


class Controller:
    def __init__(
        self,
        hw,
        app,
        transport,
        seq,
        input_mgr,
        renderer,
        pages,
        on_save=None,
        on_save_flush=None,
    ):
        self.hw = hw
        self._write_cv = hw.set_cv if hw is not None else None
        self.app = app
        self.transport = transport
        self.seq = seq
        self.input = input_mgr
        self.renderer = renderer
        self.snapshot = ViewSnapshot() if renderer is not None else None
        self.pages = pages
        self._on_save = on_save
        self._on_save_flush = on_save_flush
        self._save_pending = False
        self._display_page = -1
        self.editor = EditorService(
            app,
            transport,
            seq,
            self.on_changed,
            self._flush_outputs,
        )

    def _on_beat(self, context=None):
        if context is None:
            now_us = ticks_us()
            context = self.transport.tick_context
            context.write(
                self.transport.beat_index,
                now_us,
                now_us,
                self.transport.beat_us(),
            )
        with PROFILER.section("on_beat"):
            self.seq.tick(context)
            self._flush_outputs()
        if self.app.page != 0:
            self.app.dirty = True

    def _flush_outputs(self):
        if self._write_cv is not None and self.seq is not None:
            self.seq.flush_outputs(self._write_cv)

    def dispatch(self, ev):
        if isinstance(ev, KnobTurn):
            if ev.knob == 1:
                self.pages.on_knob1(ev.value)
            else:
                self.pages.on_knob2(ev.value)
        elif isinstance(ev, ButtonEvent):
            if ev.button == "B1":
                if ev.kind == "long":
                    was_running = self.transport.running
                    self.transport.toggle_internal_clock()
                    self._after_model_change(was_running)
                else:
                    self.app.prev_page()
            else:
                if ev.kind == "long":
                    if self._on_save:
                        saved = self._on_save()
                        if saved == "pending":
                            self._save_pending = True
                            self.app.show_notice(SAVE_PENDING_TEXT, SAVE_NOTICE_MS)
                        elif saved is not False:
                            self.app.show_notice(SAVE_NOTICE_TEXT, SAVE_NOTICE_MS)
                else:
                    self.app.next_page()
        elif isinstance(ev, ClockEvent):
            self.transport.ext_tick(ev.timestamp_us)

    def _after_model_change(self, was_running):
        if self.seq is not None:
            self.seq.sync_track_types()
            if was_running and not self.transport.running:
                self.seq.cancel_all()
        self._flush_outputs()
        self.on_changed()

    def on_changed(self):
        self.app.dirty = True
        self.app.redraw_all = True

    def _flush_pending_save(self, now_us):
        if not self._save_pending or self._on_save_flush is None:
            return
        gap = self.transport.time_to_next_step_us(now_us)
        if gap is not None and gap < SAVE_GUARD_US:
            return
        saved = self._on_save_flush()
        if saved is None:
            return
        self._save_pending = False
        notice = SAVE_NOTICE_TEXT if saved else SAVE_FAILED_TEXT
        self.app.show_notice(notice, SAVE_NOTICE_MS)

    def _prepare_display(self, now_us=None):
        if (
            self._display_page >= 0
            or not self.app.dirty
            or self.renderer is None
            or self.hw is None
        ):
            return False
        if now_us is None:
            now_us = ticks_us()
        if self.seq is not None and self.seq.pump(now_us):
            self._flush_outputs()
        if self.app.redraw_all and self.seq is not None:
            output_slack = self.seq.time_to_next_output_edge_us(now_us)
            if (
                output_slack is not None
                and output_slack <= OLED_FULL_RENDER_GUARD_US
            ):
                return False
        slack = self._display_slack_us(now_us)
        if slack is not None and slack <= OLED_PAGE_GUARD_US:
            return False
        self.snapshot.write(self.app, self.seq, self.transport)
        self.renderer.draw(self.snapshot, self.hw)
        self._display_page = 0
        self.app.dirty = False
        self.app.redraw_all = False
        return True

    def _display_slack_us(self, now_us):
        slack = None
        if self.transport.source == "INT":
            slack = self.transport.time_to_next_clock_us(now_us)
            if slack is not None and slack < 0:
                slack = 0
        if self.seq is not None:
            output_slack = self.seq.time_to_next_output_edge_us(now_us)
            if output_slack is not None and (
                slack is None or output_slack < slack
            ):
                slack = output_slack
        return slack

    def _flush_display_page(self, now_us=None):
        if self._display_page < 0:
            return False
        if now_us is None:
            now_us = ticks_us()
        if self.seq is not None and self.seq.pump(now_us):
            self._flush_outputs()
        slack = self._display_slack_us(now_us)
        if slack is not None and slack <= OLED_PAGE_GUARD_US:
            return False

        self.renderer.show_page(self._display_page, self.hw)
        self._display_page += 1
        if self._display_page == OLED_PAGE_COUNT:
            self._display_page = -1

        # Service internal deadlines immediately after the blocking I2C page.
        completed_us = ticks_us()
        self.transport.update(completed_us)
        if self.seq is not None and self.seq.pump(completed_us):
            self._flush_outputs()
        return True

    def main(self):
        while True:
            now_us = ticks_us()
            with PROFILER.section("loop"):
                self.app.expire_notice(ticks_ms())
                self.transport.update(now_us)
                self.seq.update(now_us, self.transport.tick_context)
                if self.seq.pump(now_us):
                    self._flush_outputs()
                with PROFILER.section("poll"):
                    events = self.input.poll()
                for ev in events:
                    with PROFILER.section("dispatch"):
                        self.dispatch(ev)
                self._flush_pending_save(now_us)
                if self._display_page < 0 and self.app.dirty:
                    with PROFILER.section("render"):
                        self._prepare_display()
                with PROFILER.section("display"):
                    self._flush_display_page()
            time.sleep_ms(0)
            PROFILER.maybe_print(now_us)


# ---------------------------------------------------------------------------
# Application —— EuroPiScript 入口
# ---------------------------------------------------------------------------

class Seq2(EuroPiScript):
    @classmethod
    def display_name(cls):
        return "Seq2"

    def __init__(self):
        super().__init__()

        hw = Hardware()
        app = UiState()
        transport = Transport(None)
        seq = Sequencer()

        for i in range(NUM_TRACKS):
            g1 = EuclidPattern(16, 5, 0, 100)
            g2 = EuclidPattern(16, 3, (i * 2) % 16, 100)
            seq.add_track(Track(i, g1, g2, MERGE_OR))
        # 固定配对，无需路由表：track i → cv(i) 主 / cv(i+3) 门

        input_mgr = InputManager(hw)
        renderer = Renderer()
        pages = Pages(app, transport, seq, None)

        self.persistence = PersistenceService(self)
        controller = Controller(
            hw,
            app,
            transport,
            seq,
            input_mgr,
            renderer,
            pages,
            on_save=self.request_save,
            on_save_flush=self.flush_save,
        )
        pages.set_exec(controller.editor.set)
        transport.set_on_tick(controller._on_beat)
        transport.set_runtime_callbacks(
            on_stop=seq.cancel_all,
            on_reset=seq.reset,
        )

        self.hw = hw
        self.app = app
        self.transport = transport
        self.seq = seq
        self.controller = controller

        if SAVE_STATES:
            self.load_state()
        if self.transport.source == "INT":
            self.transport.start()
        else:
            self.transport.stop()

    def main(self):
        self.controller.main()

    # —— 状态持久化 ——
    def get_state(self):
        return ProjectState.capture(self.transport, self.seq.tracks).to_dict()

    def set_state(self, state):
        project = ProjectState.decode(state)
        if project is None:
            return False
        project.apply(self.transport, self.seq)
        return True

    def load_state(self):
        project = self.persistence.load()
        if project is not None:
            project.apply(self.transport, self.seq)

    def save_state(self):
        if not SAVE_STATES:
            return False
        project = ProjectState.capture(self.transport, self.seq.tracks)
        return self.persistence.save(project)

    def request_save(self):
        if not SAVE_STATES:
            return False
        project = ProjectState.capture(self.transport, self.seq.tracks)
        return self.persistence.request(project)

    def flush_save(self):
        return self.persistence.flush()


if __name__ == "__main__":
    Seq2().main()
