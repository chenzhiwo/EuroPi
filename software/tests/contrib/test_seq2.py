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
import contrib.seq2 as seq2
from contrib.seq2 import AppState, ButtonEvent, Controller, Renderer, Transport


def make_controller(on_save=None):
    app = AppState()
    transport = Transport(None)
    controller = Controller(None, app, transport, None, None, None, None, on_save=on_save)
    return controller, app, transport


def test_b1_long_toggles_internal_clock():
    controller, _, transport = make_controller()

    controller.dispatch(ButtonEvent("B1", "long"))
    assert transport.running

    controller.dispatch(ButtonEvent("B1", "long"))
    assert not transport.running


def test_b1_long_does_not_affect_external_clock():
    controller, _, transport = make_controller()
    transport.set_source("EXT")

    controller.dispatch(ButtonEvent("B1", "long"))

    assert transport.source == "EXT"
    assert not transport.running


def test_b2_long_saves_without_changing_page():
    saves = []
    controller, app, _ = make_controller(lambda: saves.append(True))
    app._set_page(2)

    controller.dispatch(ButtonEvent("B2", "long"))

    assert saves == [True]
    assert app.page == 2
    assert app.notice == "SAVED"


def test_save_notice_expires_after_one_second(monkeypatch):
    now = [100]
    monkeypatch.setattr(seq2, "ticks_ms", lambda: now[0])
    monkeypatch.setattr(seq2, "ticks_add", lambda value, delta: value + delta)
    monkeypatch.setattr(seq2, "ticks_diff", lambda left, right: left - right)
    app = AppState()

    app.show_notice("SAVED", 1_000)
    app.dirty = False
    now[0] = 1_099
    app.expire_notice(now[0])
    assert app.notice == "SAVED"
    assert not app.dirty

    now[0] = 1_100
    app.expire_notice(now[0])
    assert app.notice is None
    assert app.dirty


def test_save_notice_is_left_aligned_on_top_row():
    class DisplayHardware:
        def __init__(self):
            self.text = []
            self.rects = []

        def display_text(self, text, x, y):
            self.text.append((text, x, y))

        def display_fill_rect(self, x, y, width, height, color):
            self.rects.append((x, y, width, height, color))

    hw = DisplayHardware()

    Renderer()._draw_notice("SAVED", hw)

    assert hw.rects == [(0, 0, 128, 8, 0)]
    assert hw.text == [("SAVED", 0, 0)]


def test_failed_save_does_not_show_success_notice():
    controller, app, _ = make_controller(lambda: False)

    controller.dispatch(ButtonEvent("B2", "long"))

    assert app.notice is None


def test_short_button_presses_still_change_pages():
    controller, app, _ = make_controller()

    controller.dispatch(ButtonEvent("B2", "press"))
    assert app.page == 1

    controller.dispatch(ButtonEvent("B1", "press"))
    assert app.page == 0


def test_tempo_change_does_not_restart_stopped_internal_clock():
    _, _, transport = make_controller()
    transport.start()
    transport.stop()

    transport.set_bpm(121)
    transport.set_mul(5)

    assert not transport.running
