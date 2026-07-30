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
Seq2 - 6-track multi-engine sequencer
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

电压范围下沉到每轨（v_lo / v_hi）；门高电平由引擎翻译成两次电压事件。

控制 / 时钟 / 页面交互见 seq2.md。本文件保持单文件，严格按分层架构：
  Hardware → InputManager → Controller → (Pages / AppState / Sequencer /
  TrackEngine / Transport) → Renderer。仅 Hardware 接触 EuroPi API。
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
    def section(self, name): return self._NullSection()
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

NUM_TRACKS = 3       # 3 条轨道，每条占用一对 CV（主 + 门/钟）
NUM_PAGES = 4        # 0 全局 + 1..3 轨道
T_SHOW_US = 20_000
DEBOUNCE_MS = 30
KNOB_DEADBAND = 0.01


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
    __slots__ = ()


# ---------------------------------------------------------------------------
# 语义输出事件（硬件无关）
# ---------------------------------------------------------------------------

class OutputEvent:
    pass


class CVOutputEvent(OutputEvent):
    """某 CV 通道置为指定电压（电平已由引擎计算后携带）。"""
    __slots__ = ("channel", "voltage")
    def __init__(self, channel, voltage):
        self.channel = channel
        self.voltage = voltage


class ClockOutputEvent(OutputEvent):
    __slots__ = ()


# ---------------------------------------------------------------------------
# 参数槽（表驱动 UI：全局参数 / 引擎参数 / 步槽 统一抽象）
# ---------------------------------------------------------------------------

class Param:
    """一个 K2 可编辑的参数槽。pmin / pmax 可为 int 或 callable(ctx)（动态范围）。

    get_cur(ctx) 读取当前值；make(ctx, v) 造命令；fmt(ctx) 顶栏显示。
    ctx 对全局参数 = transport，对轨道参数 = track。
    """
    __slots__ = ("abbr", "pmin", "pmax", "discrete", "_get", "_make", "_fmt")

    def __init__(self, abbr, pmin, pmax, discrete, get_cur, make_cmd, fmt):
        self.abbr = abbr
        self.pmin = pmin
        self.pmax = pmax
        self.discrete = discrete
        self._get = get_cur
        self._make = make_cmd
        self._fmt = fmt

    def _resolve(self, ctx, v):
        return v(ctx) if callable(v) else v

    def get_cur(self, ctx):
        return self._get(ctx)

    def make(self, ctx, v):
        return self._make(ctx, v)

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
        exec_cmd(self._make(ctx, val))


class StepSlot:
    """CV 序列的步槽：编辑第 idx 步的 0~127 值。"""
    __slots__ = ("idx",)
    pmin = 0
    pmax = CV_VAL_MAX
    discrete = False

    def __init__(self, idx):
        self.idx = idx

    @property
    def abbr(self):
        return f"S{self.idx + 1:02d}"

    def get_cur(self, track):
        return track.engine.values[self.idx]

    def make(self, track, v):
        return SetCVStep(track.engine, self.idx, v)

    def fmt(self, track):
        # 编辑步时只显示 cv 数值（0~127），不显示电压换算
        return f"{track.engine.values[self.idx]}"

    def edit(self, track, p, exec_cmd, app=None):
        # 编辑步：K2 采用 jump 策略（旋钮位置直接映射步值，无拾取死区）
        val = round(p * CV_VAL_MAX)
        val = min(max(val, 0), CV_VAL_MAX)
        if val == self.get_cur(track):
            return
        exec_cmd(self.make(track, val))


# ---------------------------------------------------------------------------
# 命令（Controller → Model 统一出口）
# ---------------------------------------------------------------------------

class Command:
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


class SetTrackType(Command):
    def __init__(self, track, typ, app=None):
        self._t, self._v, self._app = track, typ, app
    def execute(self):
        self._t.set_type(self._v)
        if self._app is not None:
            self._app.k2_picked = False
            # 仅在轨道页切换类型时归零选择；全局页(P0)保持当前 T 槽位，避免跳回 CLK
            if self._app.page != 0:
                self._app.sel = 0


class SetTrackVLo(Command):
    def __init__(self, track, v):
        self._t, self._v = track, v
    def execute(self):
        self._t.set_v_lo(self._v)


class SetTrackVHi(Command):
    def __init__(self, track, v):
        self._t, self._v = track, v
    def execute(self):
        self._t.set_v_hi(self._v)


class SetTrackParam(Command):
    def __init__(self, track, which, attr, val):
        self._t, self._w, self._a, self._v = track, which, attr, val
    def execute(self):
        self._t.set_gen(self._w, self._a, self._v)


class SetMerge(Command):
    def __init__(self, track, val):
        self._t, self._v = track, val
    def execute(self):
        self._t.set_merge(self._v)


class SetCVLength(Command):
    def __init__(self, engine, n):
        self._e, self._v = engine, n
    def execute(self):
        self._e.set_length(self._v)


class SetCVStep(Command):
    def __init__(self, engine, idx, val):
        self._e, self._i, self._v = engine, idx, val
    def execute(self):
        self._e.set_step(self._i, self._v)


class SetCVGateLen(Command):
    def __init__(self, engine, pct):
        self._e, self._v = engine, pct
    def execute(self):
        self._e.set_gate_len(self._v)


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
        self._clock_q = []
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

    def _push_clock(self, _=None):
        self._clock_q.append(ClockEvent())

    def take_clock_events(self):
        if not self._clock_q:
            return []
        q = self._clock_q
        self._clock_q = []
        return q

    def on_clock_rise(self, cb):
        pin = self.din.pin
        def _isr(*_):
            try:
                micropython.schedule(cb, None)
            except (ValueError, RuntimeError):
                pass
        pin.irq(trigger=pin.IRQ_FALLING, handler=_isr)

    # --- CV 输出（输出事件的落点）---
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

        for c in self.hw.take_clock_events():
            events.append(c)
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
        self.steps = steps
        self.pulses = pulses
        self.rot = rot
        self.prob = prob
        self.pattern = []
        self.regenerate()

    def regenerate(self):
        self.pattern = generate_euclidean_pattern(self.steps, self.pulses, self.rot)

    def output(self, pos):
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
        self.prob = min(max(prob, 0), 100)

    def to_dict(self):
        return {"steps": self.steps, "pulses": self.pulses,
                "rot": self.rot, "prob": self.prob}

    def from_dict(self, d):
        if not d:
            return
        self.steps = d.get("steps", self.steps)
        self.pulses = d.get("pulses", self.pulses)
        self.rot = d.get("rot", self.rot)
        self.prob = d.get("prob", self.prob)
        self.regenerate()


class TrackPlayer:
    def __init__(self, patterns, merge):
        self.patterns = patterns
        self.merge = merge
        self.positions = [p.steps - 1 for p in patterns]
        self.last_out = False

    def output(self):
        ons = []
        for i, p in enumerate(self.patterns):
            self.positions[i] = (self.positions[i] + 1) % p.steps
            ons.append(p.output(self.positions[i]))
        out = combine_outputs(ons[0], ons[1], self.merge)
        self.last_out = out
        return out

    def reset(self):
        self.positions = [p.steps - 1 for p in self.patterns]
        self.last_out = False

    def to_dict(self):
        return {"positions": list(self.positions), "last_out": self.last_out}

    def from_dict(self, d):
        if not d:
            return
        pos = d.get("positions")
        if pos:
            self.positions = pos
        self.last_out = d.get("last_out", self.last_out)


# ---------------------------------------------------------------------------
# TrackEngine 接口与两种引擎
# ---------------------------------------------------------------------------

class TrackEngine:
    TYPE = ""

    def param_defs(self, track):
        raise NotImplementedError

    def step_slots(self, track):
        return 0

    def slots(self, track):
        return self.param_defs(track) + [StepSlot(i) for i in range(self.step_slots(track))]

    def tick(self, ch, settings, bus, now_us, track, transport):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def to_dict(self):
        raise NotImplementedError

    def from_dict(self, d):
        raise NotImplementedError


class EuclidEngine(TrackEngine):
    TYPE = "EUC"

    def __init__(self, g1, g2, merge):
        self.g1 = g1
        self.g2 = g2
        self.patterns = [g1, g2]
        self.merge = merge
        self.player = TrackPlayer(self.patterns, merge)
        self.last_out = False

    def set_merge(self, v):
        self.merge = v
        self.player.merge = v

    def param_defs(self, track):
        return [
            Param("ROT1", 0, lambda t: self.g1.steps, False,
                  lambda t: self.g1.rot,
                  lambda t, v: SetTrackParam(track, 1, "rot", v),
                  lambda t: self.g1.rot),
            Param("ROT2", 0, lambda t: self.g2.steps, False,
                  lambda t: self.g2.rot,
                  lambda t, v: SetTrackParam(track, 2, "rot", v),
                  lambda t: self.g2.rot),
            Param("PLS1", 0, lambda t: self.g1.steps, False,
                  lambda t: self.g1.pulses,
                  lambda t, v: SetTrackParam(track, 1, "pulses", v),
                  lambda t: self.g1.pulses),
            Param("PLS2", 0, lambda t: self.g2.steps, False,
                  lambda t: self.g2.pulses,
                  lambda t, v: SetTrackParam(track, 2, "pulses", v),
                  lambda t: self.g2.pulses),
            Param("STP1", MIN_STEPS, MAX_STEPS, False,
                  lambda t: self.g1.steps,
                  lambda t, v: SetTrackParam(track, 1, "steps", v),
                  lambda t: self.g1.steps),
            Param("STP2", MIN_STEPS, MAX_STEPS, False,
                  lambda t: self.g2.steps,
                  lambda t, v: SetTrackParam(track, 2, "steps", v),
                  lambda t: self.g2.steps),
            Param("PRB1", 0, 100, False,
                  lambda t: self.g1.prob,
                  lambda t, v: SetTrackParam(track, 1, "prob", v),
                  lambda t: self.g1.prob),
            Param("PRB2", 0, 100, False,
                  lambda t: self.g2.prob,
                  lambda t, v: SetTrackParam(track, 2, "prob", v),
                  lambda t: self.g2.prob),
            Param("MERG", 0, 4, True,
                  lambda t: self.merge,
                  lambda t, v: SetMerge(track, v),
                  lambda t: MERGE_MODES[self.merge]),
        ]

    def tick(self, ch_pitch, ch_gate, settings, bus, now_us, track, transport):
        on = self.player.output()
        self.last_out = on
        v_hi = settings.v_hi
        v_lo = settings.v_lo
        # EUC 固定配对双通道：第一个 CV（cv1~3）出门（欧几里得节奏），
        # 第二个 CV（cv4~6）出时钟（每个步稳定脉冲）
        if on:
            bus.emit(CVOutputEvent(ch_pitch, v_hi))
            bus.emit_after(GATE_MS * 1000, CVOutputEvent(ch_pitch, v_lo))
        else:
            bus.emit(CVOutputEvent(ch_pitch, v_lo))
        bus.emit(CVOutputEvent(ch_gate, v_hi))
        bus.emit_after(GATE_MS * 1000, CVOutputEvent(ch_gate, v_lo))

    def reset(self):
        self.player.reset()

    def to_dict(self):
        return {"g1": self.g1.to_dict(), "g2": self.g2.to_dict(),
                "merge": self.merge}

    def from_dict(self, d):
        if not d:
            return
        self.g1.from_dict(d.get("g1", {}))
        self.g2.from_dict(d.get("g2", {}))
        self.merge = d.get("merge", self.merge)
        self.player = TrackPlayer(self.patterns, self.merge)


class CVSeqEngine(TrackEngine):
    TYPE = "CVSEQ"

    def __init__(self):
        self.length = 8
        self.values = [(i * CV_VAL_MAX) // (CV_LEN - 1) for i in range(CV_LEN)]
        self.gate_len = 50   # 门长度（占每步百分比），恒定输出在配对门通道
        self.pos = 0
        self.gate_last = False

    def param_defs(self, track):
        params = [
            Param("LEN", 1, CV_LEN, True,
                  lambda t: t.engine.length,
                  lambda t, v: SetCVLength(t.engine, v),
                  lambda t: t.engine.length),
            Param("VLO", CV_MIN, CV_MAX, False,
                  lambda t: t.v_lo,
                  lambda t, v: SetTrackVLo(t, v),
                  lambda t: f"{t.v_lo}V"),
            Param("VHI", CV_MIN, CV_MAX, False,
                  lambda t: t.v_hi,
                  lambda t, v: SetTrackVHi(t, v),
                  lambda t: f"{t.v_hi}V"),
        ]
        params.append(
            Param("GLEN", 5, 100, False,
                  lambda t: t.engine.gate_len,
                  lambda t, v: SetCVGateLen(t.engine, v),
                  lambda t: f"{t.engine.gate_len}%")
        )
        return params

    def step_slots(self, track):
        return self.length

    def set_length(self, n):
        # values 固定为 CV_LEN 长度，缩短只改变播放窗口，不破坏已编辑音型
        self.length = min(max(n, 1), CV_LEN)

    def set_step(self, i, v):
        if 0 <= i < CV_LEN:
            self.values[i] = min(max(v, 0), CV_VAL_MAX)

    def set_gate_len(self, pct):
        self.gate_len = min(max(pct, 5), 100)

    def tick(self, ch_pitch, ch_gate, settings, bus, now_us, track, transport):
        self.pos = (self.pos + 1) % self.length
        v = self.values[self.pos]
        volt = settings.v_lo + (settings.v_hi - settings.v_lo) * v / CV_VAL_MAX
        bus.emit(CVOutputEvent(ch_pitch, volt))
        # 配对门通道（cv4~6）输出门脉冲，长度由 GLEN 决定；cv=0 时不出门
        if v > 0:
            step_us = transport.beat_us()
            glen_us = step_us * self.gate_len // 100
            bus.emit(CVOutputEvent(ch_gate, settings.v_hi))
            bus.emit_after(glen_us, CVOutputEvent(ch_gate, settings.v_lo))
        self.gate_last = v > 0

    def reset(self):
        self.pos = 0
        self.gate_last = False

    def to_dict(self):
        return {"length": self.length,
                "values": list(self.values),
                "gate_len": self.gate_len,
                "pos": self.pos}

    def from_dict(self, d):
        if not d:
            return
        self.length = min(max(d.get("length", self.length), 1), CV_LEN)
        vals = d.get("values", [])
        self.values = [min(max(x, 0), CV_VAL_MAX) for x in vals[:CV_LEN]]
        while len(self.values) < CV_LEN:
            self.values.append(0)
        self.gate_len = min(max(d.get("gate_len", self.gate_len), 5), 100)
        self.pos = d.get("pos", 0) % self.length


# ---------------------------------------------------------------------------
# Track —— 容器：设置 + 引擎实例（常驻）
# ---------------------------------------------------------------------------

class Track:
    def __init__(self, idx, g1, g2, merge):
        self.index = idx
        self.v_lo = 0
        self.v_hi = 5
        self.type = "EUC"
        # 固定配对：主通道 = cv(idx)，门/钟通道 = cv(idx+3)
        self.cv_index = idx
        self.gate_index = idx + 3
        self.engines = {
            "EUC": EuclidEngine(g1, g2, merge),
            "CVSEQ": CVSeqEngine(),
        }

    @property
    def engine(self):
        return self.engines["EUC"] if self.type == "EUC" else self.engines["CVSEQ"]

    def set_type(self, typ):
        if typ not in TYPE_NAMES:
            return
        self.type = typ
        self.engine.reset()  # 播放头归零，与其他轨对齐

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

    def to_dict(self):
        return {
            "type": self.type,
            "v_lo": self.v_lo,
            "v_hi": self.v_hi,
            "EUC": self.engines["EUC"].to_dict(),
            "CVSEQ": self.engines["CVSEQ"].to_dict(),
        }

    def from_dict(self, d):
        if not d:
            return
        self.type = d.get("type", self.type)
        if self.type not in TYPE_NAMES:
            self.type = "EUC"
        self.v_lo = d.get("v_lo", self.v_lo)
        self.v_hi = d.get("v_hi", self.v_hi)
        self.engines["EUC"].from_dict(d.get("EUC", {}))
        self.engines["CVSEQ"].from_dict(d.get("CVSEQ", {}))


# ---------------------------------------------------------------------------
# OutputBus + Sequencer
# ---------------------------------------------------------------------------

class OutputBus:
    """引擎 tick 时接收输出事件的落点：emit 立即事件，emit_after 延时事件。"""
    __slots__ = ("_seq", "_now", "events")

    def __init__(self, seq, now_us):
        self._seq = seq
        self._now = now_us
        self.events = []

    def emit(self, ev):
        self.events.append(ev)

    def emit_after(self, delay_us, ev):
        self._seq._scheduled.append((ticks_add(self._now, delay_us), ev))


class Sequencer:
    def __init__(self):
        self.tracks = []
        self._scheduled = []             # [(due_us, OutputEvent), ...]

    def add_track(self, track):
        self.tracks.append(track)

    def tick(self, now_us, transport):
        bus = OutputBus(self, now_us)
        for t in self.tracks:
            if t.type == "OFF":
                bus.emit(CVOutputEvent(t.cv_index, 0))
                bus.emit(CVOutputEvent(t.gate_index, 0))
                continue
            if t.type == "EUC":
                t.engines["EUC"].tick(t.cv_index, t.gate_index, t, bus, now_us, t, transport)
            else:  # CV：主通道出音高，配对门通道出门脉冲
                t.engines["CVSEQ"].tick(t.cv_index, t.gate_index, t, bus, now_us, t, transport)
        return bus.events

    def pump(self, now_us):
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
# Transport —— 拥有全局音乐时间（无全局电平）
# ---------------------------------------------------------------------------

class Transport:
    def __init__(self, on_tick):
        self._on_tick = on_tick
        self.bpm = 120
        self.mul = 4
        self.source = "INT"
        self.running = False
        self.beat_index = 0
        self.next_clock_us = 0

    def set_on_tick(self, cb):
        self._on_tick = cb

    def beat_us(self):
        eff = self.bpm * self.mul
        return max(1, 60_000_000 // eff)

    def start(self):
        self.next_clock_us = ticks_add(ticks_us(), self.beat_us())
        self.beat_index = 0
        self.running = True

    def stop(self):
        self.running = False

    def update(self, now_us):
        if self.source != "INT" or not self.running or self._on_tick is None:
            return
        if ticks_diff(now_us, self.next_clock_us) >= 0:
            self._on_tick()
            self.beat_index += 1
            self.next_clock_us = ticks_add(now_us, self.beat_us())

    def time_to_next_clock_us(self, now_us):
        if self.source != "INT" or not self.running:
            return None
        return ticks_diff(self.next_clock_us, now_us)

    def ext_tick(self, t=None):
        if self.source == "EXT" and self._on_tick is not None:
            self._on_tick()
            self.beat_index += 1

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
            self.start()

    def set_mul(self, mul):
        mul = min(max(mul, MIN_MUL), MAX_MUL)
        if mul == self.mul:
            return
        self.mul = mul
        if self.source == "INT":
            self.start()

    def to_dict(self):
        return {"source": self.source, "bpm": self.bpm, "mul": self.mul}

    def from_dict(self, d):
        if not d:
            return
        self.source = d.get("source", self.source)
        self.bpm = d.get("bpm", self.bpm)
        self.mul = d.get("mul", self.mul)


# ---------------------------------------------------------------------------
# Application State —— 仅 UI 状态
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self.page = 0
        self.sel = 0
        self.k2_picked = False
        self.dirty = True

    def _set_page(self, p):
        self.page = p
        self.sel = 0
        self.k2_picked = False
        self.dirty = True

    def prev_page(self):
        self._set_page((self.page - 1) % NUM_PAGES)

    def next_page(self):
        self._set_page((self.page + 1) % NUM_PAGES)

    def goto_global(self):
        self._set_page(0)


# ---------------------------------------------------------------------------
# Renderer —— 无状态渲染
# ---------------------------------------------------------------------------

class Renderer:
    def render(self, app, seq, transport, hw):
        hw.display_clear()
        if app.page == 0:
            self._draw_global(app, seq, transport, hw)
        else:
            self._draw_channel(app, seq, transport, hw)
        hw.display_show()

    def _status(self, app, seq, transport):
        if app.page == 0:
            slot = build_global_params(seq, transport, None)[app.sel]
            return f"P{app.page}", f"{slot.abbr}:{slot.fmt(transport)}"
        track = seq.tracks[app.page - 1]
        slot = track.engine.slots(track)[app.sel]
        return f"P{app.page}", f"{slot.abbr}:{slot.fmt(track)}"

    def _draw_global(self, app, seq, transport, hw):
        page_str, val_str = self._status(app, seq, transport)
        hw.display_text(page_str, OLED_WIDTH - len(page_str) * 8, 0)
        hw.display_text(val_str, 0, 0)

    def _draw_channel(self, app, seq, transport, hw):
        page_str, val_str = self._status(app, seq, transport)
        hw.display_text(page_str, OLED_WIDTH - len(page_str) * 8, 0)
        hw.display_text(val_str, 0, 0)
        track = seq.tracks[app.page - 1]
        if track.type == "OFF":
            hw.display_text("OFF", 52, 12)
            return
        if track.type == "EUC":
            self._draw_euclid(track, transport, hw)
        else:
            self._draw_cv(track, app, hw)

    def _draw_euclid(self, track, transport, hw):
        eng = track.engine
        bi = transport.beat_index
        cur = bi - 1 if bi > 0 else 0
        wstart = (cur // 16) * 16
        self._draw_seq_row(eng.g1, 8, wstart, hw)
        self._draw_seq_row(eng.g2, 16, wstart, hw)
        self._draw_playhead(track, cur % 16, 26, hw)

    def _draw_cv(self, track, app, hw):
        eng = track.engine
        for c in range(CV_LEN):
            if c < eng.length:
                h = round(eng.values[c] / CV_VAL_MAX * 16)
                if h > 0:
                    hw.display_fill_rect(c * 8, 25 - h, 6, h, 1)
            else:
                hw.display_fill_rect(c * 8 + 2, 25, 2, 1, 1)
        # 选中步槽：底部播放头行画一条横线（宽 6，与步条一致）作为编辑指示
        npar = len(eng.param_defs(track))
        step_sel = app.sel - npar
        if 0 <= step_sel < eng.length:
            hw.display_fill_rect(step_sel * 8, 29, 6, 1, 1)
        # 唱头（播放实时位置），与编辑头可同时显示
        if 0 <= eng.pos < CV_LEN:
            hw.display_fill_rect(eng.pos * 8, 27, 6, 3, 1)

    def _draw_seq_row(self, gen, y, wstart, hw):
        for c in range(16):
            idx = (wstart + c) % gen.steps
            if gen.pattern[idx]:
                hw.display_fill_rect(c * 8, y, 6, 6, 1)
            else:
                hw.display_fill_rect(c * 8 + 2, y + 2, 2, 2, 1)

    def _draw_playhead(self, track, play_pos, y, hw):
        if track.engine.last_out:
            hw.display_fill_rect(play_pos * 8, y, 6, 6, 1)
        else:
            hw.display_fill_rect(play_pos * 8 + 2, y + 2, 2, 2, 1)


# ---------------------------------------------------------------------------
# UI Pages —— 把交互译为命令（槽位模型，表驱动）
# ---------------------------------------------------------------------------

def build_global_params(seq, transport, app):
    """P0 全局参数：CLK / BPM / MUL / T1..T6 / R1..R6。"""
    params = [
        Param("CLK", 0, 1, True,
              lambda t: 0 if t.source == "INT" else 1,
              lambda t, v: SetTransportSource(t, "EXT" if v else "INT"),
              lambda t: t.source),
        Param("BPM", MIN_BPM, MAX_BPM, False,
              lambda t: t.bpm,
              lambda t, v: SetBpm(t, v),
              lambda t: t.bpm),
        Param("MUL", MIN_MUL, MAX_MUL, False,
              lambda t: t.mul,
              lambda t, v: SetMul(t, v),
              lambda t: t.mul),
    ]
    for i in range(NUM_TRACKS):
        tid = i
        params.append(
            Param(f"T{i + 1}", 0, len(TYPE_NAMES) - 1, True,
                  lambda tr, tid=tid: TYPE_NAMES.index(seq.tracks[tid].type),
                  lambda tr, v, tid=tid: SetTrackType(seq.tracks[tid], TYPE_NAMES[v], app),
                  lambda tr, tid=tid: seq.tracks[tid].type)
        )
    return params


class Pages:
    def __init__(self, app, transport, seq, exec_cmd):
        self.app = app
        self.transport = transport
        self.seq = seq
        self._exec = exec_cmd

    def set_exec(self, exec_cmd):
        self._exec = exec_cmd

    def _slot_count(self):
        if self.app.page == 0:
            return len(build_global_params(self.seq, self.transport, self.app))
        track = self.seq.tracks[self.app.page - 1]
        return len(track.engine.slots(track))

    def on_knob1(self, p):
        n = self._slot_count()
        idx = int(p * n)
        if idx >= n:
            idx = n - 1
        if idx != self.app.sel:
            self.app.sel = idx
            self.app.k2_picked = False
            self.app.dirty = True

    def on_knob2(self, p):
        if self.app.page == 0:
            ctx = self.transport
            slots = build_global_params(self.seq, self.transport, self.app)
        else:
            track = self.seq.tracks[self.app.page - 1]
            ctx = track
            slots = track.engine.slots(track)
        slots[self.app.sel].edit(ctx, p, self._exec, self.app)


# ---------------------------------------------------------------------------
# Controller
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

    def _on_beat(self):
        with PROFILER.section("on_beat"):
            events = self.seq.tick(ticks_us(), self.transport)
            self._apply_outputs(events)
        self.app.dirty = True

    def _apply_outputs(self, events):
        for ev in events:
            if isinstance(ev, CVOutputEvent):
                self.hw.set_cv(ev.channel, ev.voltage)
            elif isinstance(ev, ClockOutputEvent):
                pass

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
                        self._on_save()
                else:
                    self.app.prev_page()
            else:
                if ev.kind == "long":
                    self.app.goto_global()
                else:
                    self.app.next_page()
        elif isinstance(ev, ClockEvent):
            self.transport.ext_tick()

    def _exec(self, cmd):
        cmd.execute()
        self.on_changed()

    def on_changed(self):
        self.app.dirty = True

    def main(self):
        while True:
            now_us = ticks_us()
            with PROFILER.section("loop"):
                self.transport.update(now_us)
                due = self.seq.pump(now_us)
                if due:
                    self._apply_outputs(due)
                with PROFILER.section("poll"):
                    events = self.input.poll()
                for ev in events:
                    with PROFILER.section("dispatch"):
                        self.dispatch(ev)
                if self.app.dirty:
                    gap = self.transport.time_to_next_clock_us(now_us)
                    if gap is None or gap >= T_SHOW_US:
                        with PROFILER.section("render"):
                            self.renderer.render(self.app, self.seq, self.transport, self.hw)
                        self.app.dirty = False
            time.sleep_ms(0)
            PROFILER.maybe_print(now_us)


# ---------------------------------------------------------------------------
# Application —— EuroPiScript 入口
# ---------------------------------------------------------------------------

class Seq2(EuroPiScript):
    @classmethod
    def display_name(cls):
        return "Seq2 6trk"

    def __init__(self):
        super().__init__()

        hw = Hardware()
        app = AppState()
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

        controller = Controller(
            hw, app, transport, seq, input_mgr, renderer, pages,
            on_save=self.save_state,
        )
        pages.set_exec(controller._exec)
        transport.set_on_tick(controller._on_beat)

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
        return {
            "clock": self.transport.to_dict(),
            "tracks": [t.to_dict() for t in self.seq.tracks],
        }

    def set_state(self, state):
        try:
            self.transport.from_dict(state.get("clock", {}))
            tracks = state.get("tracks") or []
            for track, d in zip(self.seq.tracks, tracks):
                track.from_dict(d)
        except Exception as e:
            print("Seq2: failed to load state:", e)

    def load_state(self):
        state = self.load_state_json()
        if state:
            self.set_state(state)

    def save_state(self):
        if not SAVE_STATES:
            return
        self.save_state_json(self.get_state())


if __name__ == "__main__":
    Seq2().main()
