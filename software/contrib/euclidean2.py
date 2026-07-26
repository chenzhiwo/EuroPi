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
  全局页可切换 内部时钟(INT, 可调 BPM) / 外部时钟(EXT, DIN 上升沿驱动)

页面：
  P0 全局时钟  ->  P1..P6 各通道编辑页  ->  循环
  每通道 10 项参数（K1 遍历）：
    ROT1 Gen1旋转  ROT2 Gen2旋转  PLS1 Gen1脉冲  PLS2 Gen2脉冲
    STP1 Gen1步数  STP2 Gen2步数  PRB1 Gen1概率  PRB2 Gen2概率
    MERG 合并模式  LEVL 通道CV输出电平

UI（128x32）：
  行0 状态栏  P{page} {缩写}:{值}（全局页 P0 仅显示此行）
  行1 Gen1 序列（实心=触发，点=空步），当前步固定在最左(col0)
  行2 Gen2 序列，同左锚定前瞻滚动窗（最多 16 步，超出按播放头左滚）
  行3 合并结果序列（移位寄存器 out_reg，长度 = min(16, 最短序列)）

基于 europi_hardware.py / europi.py 实机定义实现。
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

import machine
import random
import time
from utime import ticks_diff, ticks_ms


# 合并模式（对齐 Digitakt 2 双机器）
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

# 通道参数顺序（K1 遍历），(缩写, 内部kind)，缩写统一 4 字符便于顶栏阅读
CH_PARAMS = [
    ("ROT1", "rot1"),
    ("PLS1", "pulses1"),
    ("STP1", "steps1"),
    ("ROT2", "rot2"),
    ("PLS2", "pulses2"),
    ("STP2", "steps2"),
    ("PRB1", "prob1"),
    ("PRB2", "prob2"),
    ("MERG", "merge"),
    ("LEVL", "level"),
]

NUM_PAGES = 7  # 0 全局 + 1..6 通道

MIN_BPM = 20
MAX_BPM = 240
MAX_STEPS = 64  # 单发生器序列最大步数
GATE_MS = 5  # 时钟事件后统一拉低输出的延迟（ms）
K2_DEBOUNCE_MS = 40  # K2 数值提交消抖窗口（ms）


class Gen:
    """单路欧几里得发生器：Björklund 生成 + 旋转，概率仅在触发时判定。"""

    def __init__(self, steps, pulses, rot, prob):
        self.steps = steps
        self.pulses = pulses
        self.rot = rot
        self.prob = prob
        self.pos = steps - 1  # 首次 advance 后落于 0
        self.pattern = []
        self.regenerate()

    def regenerate(self):
        self.pattern = generate_euclidean_pattern(self.steps, self.pulses, self.rot)
        if self.pos >= self.steps:
            self.pos %= self.steps

    def set_steps(self, steps):
        self.steps = steps
        if self.pulses > steps:
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

    def advance(self):
        self.pos = (self.pos + 1) % self.steps

    def active(self):
        """当前播放头步是否应触发（pattern 且通过概率保留判定）。"""
        if not self.pattern[self.pos]:
            return False
        if self.prob >= 100:
            return True
        if self.prob <= 0:
            return False
        return random.random() < self.prob / 100.0


class Channel:
    """一个通道：双发生器 + 合并模式 + 输出电平 + 移位寄存器式输出序列。"""

    def __init__(self, cv, g1, g2, merge, level):
        self.cv = cv
        self.g1 = g1
        self.g2 = g2
        self.merge = merge
        self.level = level
        self.rebuild_reg()

    def reg_len(self):
        return min(16, min(self.g1.steps, self.g2.steps))

    def rebuild_reg(self):
        """依当前双序列重建移位寄存器（输出序列），长度 = min(16, 最短序列)。"""
        n = self.reg_len()
        m = min(self.g1.steps, self.g2.steps)
        self.out_reg = []
        for i in range(n):
            idx = (self.g1.pos + i) % m
            self.out_reg.append(
                combine_outputs(self.g1.pattern[idx], self.g2.pattern[idx], self.merge)
            )

    def output(self):
        # 先推进播放头，再对当前步做合并与概率运算
        self.g1.advance()
        self.g2.advance()
        on1 = self.g1.active()
        on2 = self.g2.active()
        out = combine_outputs(on1, on2, self.merge)
        if out:
            self.cv.voltage(self.level)
        else:
            self.cv.off()
        # 移位寄存器一步操作：左端取出（已播放），右侧按输入序列压入新值
        if self.out_reg:
            n = len(self.out_reg)
            m = min(self.g1.steps, self.g2.steps)
            idx = (self.g1.pos + (n - 1)) % m
            new_val = combine_outputs(
                self.g1.pattern[idx], self.g2.pattern[idx], self.merge
            )
            self.out_reg.pop(0)
            self.out_reg.append(new_val)


class Euclidean2(EuroPiScript):
    @classmethod
    def display_name(cls):
        return "Euclid2 6ch"

    def __init__(self):
        super().__init__()

        self.source = "INT"
        self.bpm = 120

        cv_list = [cv1, cv2, cv3, cv4, cv5, cv6]
        self.channels = []
        for i in range(6):
            # 给不同通道一点差异化的默认节奏
            g1 = Gen(16, 5, 0, 100)
            g2 = Gen(16, 3, (i * 2) % 16, 100)
            ch = Channel(cv_list[i], g1, g2, MERGE_OR, 5)
            self.channels.append(ch)

        self.page = 0
        self.sel = 0
        self.k2_picked = False
        self.k2_pending = None  # 消抖候选值
        self.k2_pending_t = 0  # 候选值首次出现时刻
        self.dirty = True
        self._dirty_save = False

        # 按钮边沿状态（用于短按/长按判定）
        self.b1_down = False
        self.b2_down = False
        self.b1_press_t = 0
        self.b2_press_t = 0
        self.b1_long = False
        self.b2_long = False

        # 时钟：一个周期 Timer 驱动 tick；一个复用的 one-shot Timer 关门（9.13）
        # 用软件定时器 (Timer())，避免硬件 Timer ID 在本固件不可用
        self.clock_timer = machine.Timer()
        self.gate_timer = machine.Timer()
        self.clock_running = False

        self.load_state()

        @din.handler
        def on_clock_rise():
            if self.source == "EXT":
                self._fire()

        if self.source == "INT":
            self._start_clock()
        else:
            self._stop_clock()

    # ---------------- 时钟 ----------------
    def beat_ms(self):
        return max(1, 60000 // self.bpm)

    def _tick(self, t=None):
        # 时钟事件：先置位各通道输出，再用 one-shot 定时器在 GATE_MS 后统一拉低
        for ch in self.channels:
            ch.output()
        self._schedule_gate_off()
        self.dirty = True

    def _fire(self):
        # 外部/手动触发与内部周期触发共用同一置位 + 定时拉低逻辑
        self._tick()

    def _schedule_gate_off(self):
        self.gate_timer.init(
            period=GATE_MS,
            mode=machine.Timer.ONE_SHOT,
            callback=self._gates_off_irq,
        )

    def _gates_off_irq(self, t=None):
        self._gates_off()

    def _gates_off(self):
        turn_off_all_cvs()

    def _start_clock(self):
        if self.clock_running:
            self.clock_timer.deinit()
        self.clock_timer.init(
            period=self.beat_ms(), mode=machine.Timer.PERIODIC, callback=self._tick
        )
        self.clock_running = True

    def _stop_clock(self):
        if self.clock_running:
            self.clock_timer.deinit()
            self.clock_running = False
        self._gates_off()

    def set_source(self, src):
        if src == self.source:
            return
        self.source = src
        if src == "INT":
            self._start_clock()
        else:
            self._stop_clock()
        self.on_changed()

    def set_bpm(self, bpm):
        bpm = min(max(bpm, MIN_BPM), MAX_BPM)
        if bpm == self.bpm:
            return
        self.bpm = bpm
        if self.source == "INT":
            self._start_clock()  # 以新周期重启
        self.on_changed()

    def manual_advance(self):
        self._fire()

    # ---------------- 页面 ----------------
    def _set_page(self, p):
        self.page = p
        n = 2 if p == 0 else len(CH_PARAMS)
        if self.sel >= n:
            self.sel = n - 1
        self.k2_picked = False
        self.k2_pending = None
        # 切换页面先清空整屏，避免残影
        oled.fill(0)
        oled.show()
        self.dirty = True

    def prev_page(self):
        self._set_page((self.page - 1) % NUM_PAGES)

    def next_page(self):
        self._set_page((self.page + 1) % NUM_PAGES)

    def goto_global(self):
        self._set_page(0)

    # ---------------- 旋钮 ----------------
    def handle_knob1(self):
        n = 2 if self.page == 0 else len(CH_PARAMS)
        p = k1.percent()
        idx = int(p * n)  # 截断实现 ±0.5 档迟滞，防边界抖动
        if idx >= n:
            idx = n - 1
        if idx != self.sel:
            self.sel = idx
            self.k2_picked = False
            self.k2_pending = None

    def k2_value(self, pmin, pmax):
        return round(k2.percent() * (pmax - pmin)) + pmin

    def apply_pickup(self, current, pmin, pmax, setter, discrete=False):
        val = self.k2_value(pmin, pmax)
        val = min(max(val, pmin), pmax)
        tol = 0 if discrete else max(1, (pmax - pmin) // 32)
        if not self.k2_picked:
            if abs(val - current) <= tol:
                self.k2_picked = True
                self.k2_pending = None
            return
        if val == current:
            self.k2_pending = None
            return
        # 消抖：目标值需在 K2_DEBOUNCE_MS 窗口内保持稳定（无跳变）才提交，
        # 抑制 ADC 噪声导致的数值抖动/误触发
        now = ticks_ms()
        if val != self.k2_pending:
            self.k2_pending = val
            self.k2_pending_t = now
            return
        if ticks_diff(now, self.k2_pending_t) < K2_DEBOUNCE_MS:
            return
        setter(val)
        self.on_changed()
        self.k2_pending = None

    def edit_global(self):
        if self.sel == 0:  # 时钟源（离散）
            val = 1 if k2.percent() > 0.5 else 0
            cur = 0 if self.source == "INT" else 1
            if not self.k2_picked:
                if val == cur:
                    self.k2_picked = True
                return
            if val != cur:
                self.set_source("EXT" if val == 1 else "INT")
                self.on_changed()
        else:  # BPM
            self.apply_pickup(self.bpm, MIN_BPM, MAX_BPM, self.set_bpm)

    def _set_merge(self, ch, v):
        ch.merge = v
        self.on_changed()

    def _set_level(self, ch, v):
        ch.level = v
        self.on_changed()

    def edit_channel(self, ch):
        kind = CH_PARAMS[self.sel][1]
        if kind == "rot1":
            self.apply_pickup(ch.g1.rot, 0, ch.g1.steps, lambda v: (ch.g1.set_rot(v), ch.rebuild_reg()))
        elif kind == "rot2":
            self.apply_pickup(ch.g2.rot, 0, ch.g2.steps, lambda v: (ch.g2.set_rot(v), ch.rebuild_reg()))
        elif kind == "steps1":
            self.apply_pickup(ch.g1.steps, 4, MAX_STEPS, lambda v: (ch.g1.set_steps(v), ch.rebuild_reg()))
        elif kind == "steps2":
            self.apply_pickup(ch.g2.steps, 4, MAX_STEPS, lambda v: (ch.g2.set_steps(v), ch.rebuild_reg()))
        elif kind == "pulses1":
            self.apply_pickup(ch.g1.pulses, 0, ch.g1.steps, lambda v: (ch.g1.set_pulses(v), ch.rebuild_reg()))
        elif kind == "pulses2":
            self.apply_pickup(ch.g2.pulses, 0, ch.g2.steps, lambda v: (ch.g2.set_pulses(v), ch.rebuild_reg()))
        elif kind == "prob1":
            self.apply_pickup(ch.g1.prob, 0, 100, ch.g1.set_prob)
        elif kind == "prob2":
            self.apply_pickup(ch.g2.prob, 0, 100, ch.g2.set_prob)
        elif kind == "merge":
            self.apply_pickup(
                ch.merge, 0, 4, lambda v: (self._set_merge(ch, v), ch.rebuild_reg()), discrete=True
            )
        elif kind == "level":
            self.apply_pickup(
                ch.level, 0, 10, lambda v: self._set_level(ch, v)
            )

    # ---------------- 按钮（短按/长按；让出双按退出菜单） ----------------
    def handle_buttons(self):
        now = ticks_ms()
        d1 = b1.value() == HIGH
        d2 = b2.value() == HIGH
        if d1 and d2:
            # 双按 = 返回菜单，交给系统 both-handler，本脚本不处理
            self.b1_down = d1
            self.b2_down = d2
            return

        # B1
        if d1 and not self.b1_down:
            self.b1_press_t = now
            self.b1_long = False
        if d1 and not self.b1_long and ticks_diff(now, self.b1_press_t) >= 500:
            self.b1_long = True
            self.manual_advance()
        if not d1 and self.b1_down:
            if not self.b1_long:
                self.prev_page()
        self.b1_down = d1

        # B2
        if d2 and not self.b2_down:
            self.b2_press_t = now
            self.b2_long = False
        if d2 and not self.b2_long and ticks_diff(now, self.b2_press_t) >= 500:
            self.b2_long = True
            self.goto_global()
        if not d2 and self.b2_down:
            if not self.b2_long:
                self.next_page()
        self.b2_down = d2

    # ---------------- 显示 ----------------
    def redraw(self):
        oled.fill(0)
        if self.page == 0:
            self.draw_global()
        else:
            self.draw_channel(self.channels[self.page - 1])
        oled.show()

    def draw_global(self):
        # 全局时钟页只显示顶栏（P0 CLK:.. 或 P0 BPM:..），其余行留空
        if self.sel == 0:
            oled.text(f"P0 CLK:{self.source}", 0, 0, 1)
        else:
            oled.text(f"P0 BPM:{self.bpm}", 0, 0, 1)

    def _param_value(self, ch):
        kind = CH_PARAMS[self.sel][1]
        mapping = {
            "rot1": ch.g1.rot,
            "rot2": ch.g2.rot,
            "steps1": ch.g1.steps,
            "steps2": ch.g2.steps,
            "pulses1": ch.g1.pulses,
            "pulses2": ch.g2.pulses,
            "prob1": ch.g1.prob,
            "prob2": ch.g2.prob,
        }
        return mapping.get(kind, "")

    def draw_channel(self, ch):
        abbr, kind = CH_PARAMS[self.sel]
        if kind == "merge":
            val = MERGE_MODES[ch.merge]
        elif kind == "level":
            val = f"{ch.level}V"
        else:
            val = self._param_value(ch)
        oled.text(f"P{self.page} {abbr}:{val}", 0, 0, 1)
        self.draw_seq_row(ch.g1, 8)
        self.draw_seq_row(ch.g2, 16)
        self.draw_result_row(ch, 24)

    def draw_seq_row(self, gen, y):
        # 移位寄存器式滚动：当前步固定 col0，向右取至多 16 格；若序列步数 < 16 则只显示实际步数。
        # 触发以实心块表示，不触发以一点(2x2)表示，无播放头标记
        for i in range(min(16, gen.steps)):
            idx = (gen.pos + i) % gen.steps
            x = i * 8
            if gen.pattern[idx]:
                oled.fill_rect(x, y, 6, 6, 1)
            else:
                oled.fill_rect(x + 2, y + 2, 2, 2, 1)

    def draw_result_row(self, ch, y):
        # 底行 = 移位寄存器风格的输出序列（ch.out_reg），长度 = min(16, 最短序列)
        # 触发实心块、不触发一点(2x2)，无播放头标记
        for i, on in enumerate(ch.out_reg):
            x = i * 8
            if on:
                oled.fill_rect(x, y, 6, 6, 1)
            else:
                oled.fill_rect(x + 2, y + 2, 2, 2, 1)

    # ---------------- 状态持久化 ----------------
    def on_changed(self):
        self.dirty = True
        self._dirty_save = True

    def get_state(self):
        return {
            "clock": {"source": self.source, "bpm": self.bpm},
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
                    "level": c.level,
                }
                for c in self.channels
            ],
        }

    def set_state(self, state):
        try:
            clk = state.get("clock", {})
            self.source = clk.get("source", "INT")
            self.bpm = clk.get("bpm", 120)
            for i, c in enumerate(self.channels):
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
                c.level = d["level"]
                c.rebuild_reg()
        except Exception as e:
            print("Euclidean2: failed to load state:", e)

    def load_state(self):
        state = self.load_state_json()
        if state:
            self.set_state(state)

    def save_state(self):
        if not self._dirty_save:
            return
        if self.last_saved() < 1000:
            return
        self.save_state_json(self.get_state())
        self._dirty_save = False

    # ---------------- 主循环 ----------------
    def main(self):
        while True:
            self.handle_buttons()
            self.handle_knob1()
            if self.page == 0:
                self.edit_global()
            else:
                self.edit_channel(self.channels[self.page - 1])
            if self.dirty:
                self.redraw()
                self.dirty = False
            self.save_state()
            time.sleep_ms(5)


if __name__ == "__main__":
    Euclidean2().main()
