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
from contrib.seq2 import (
    AppState,
    ButtonEvent,
    ClockCapture,
    ClockEvent,
    ClockService,
    Controller,
    EuclidPattern,
    InputManager,
    MERGE_G1,
    OutputScheduler,
    Pages,
    PersistenceService,
    ProjectState,
    Renderer,
    Sequencer,
    Track,
    TrackFeature,
    Transport,
    ViewSnapshot,
    build_global_params,
    register_track_feature,
)


def legacy_euclidean_pattern(steps, pulses, rotate=0):
    """Frozen copy of Seq2's pre-refactor Euclidean output semantics."""
    if pulses == 0:
        pattern = [0] * steps
    else:
        pattern = []
        counts = []
        remainders = [pulses]
        divisor = steps - pulses
        level = 0
        while True:
            counts.append(divisor // remainders[level])
            remainders.append(divisor % remainders[level])
            divisor = remainders[level]
            level += 1
            if remainders[level] <= 1:
                break
        counts.append(divisor)

        def build(build_level):
            if build_level == -1:
                pattern.append(0)
            elif build_level == -2:
                pattern.append(1)
            else:
                for _ in range(counts[build_level]):
                    build(build_level - 1)
                if remainders[build_level] != 0:
                    build(build_level - 2)

        build(level)
        first_pulse = pattern.index(1)
        pattern = pattern[first_pulse:] + pattern[:first_pulse]

    if rotate:
        pattern = pattern[-rotate:] + pattern[:-rotate]
    return pattern


def make_controller(on_save=None):
    app = AppState()
    transport = Transport(None)
    controller = Controller(None, app, transport, None, None, None, None, on_save=on_save)
    return controller, app, transport


class FakeClock:
    def __init__(self, now_us=0):
        self.now_us = now_us

    def advance(self, delta_us):
        self.now_us += delta_us
        return self.now_us


class FakeHardware:
    def __init__(self):
        self.cv_writes = []

    def set_cv(self, channel, voltage):
        self.cv_writes.append((channel, voltage))


def make_track(index=0):
    return Track(
        index,
        EuclidPattern(4, 1, 0, 100),
        EuclidPattern(4, 0, 0, 100),
        MERGE_G1,
    )


def make_sequencer(track_count=3):
    sequencer = Sequencer()
    for index in range(track_count):
        sequencer.add_track(make_track(index))
    return sequencer


def use_linear_ticks(monkeypatch):
    monkeypatch.setattr(seq2, "ticks_add", lambda value, delta: value + delta)
    monkeypatch.setattr(seq2, "ticks_diff", lambda left, right: left - right)


def tick_sequencer(sequencer, transport, now_us, scheduled_us=None):
    if scheduled_us is None:
        scheduled_us = now_us
    context = transport.tick_context
    context.write(
        transport.beat_index,
        scheduled_us,
        now_us,
        transport.beat_us(),
    )
    sequencer.tick(context)


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


def test_pending_save_is_flushed_outside_button_dispatch(monkeypatch):
    requests = []
    flushes = []
    app = AppState()
    transport = Transport(None)
    controller = Controller(
        None,
        app,
        transport,
        None,
        None,
        None,
        None,
        on_save=lambda: requests.append(True) or "pending",
        on_save_flush=lambda: flushes.append(True) or True,
    )

    controller.dispatch(ButtonEvent("B2", "long"))

    assert requests == [True]
    assert flushes == []
    assert app.notice == seq2.SAVE_PENDING_TEXT

    controller._flush_pending_save(0)

    assert flushes == [True]
    assert app.notice == seq2.SAVE_NOTICE_TEXT


def test_pending_save_failure_replaces_pending_notice():
    app = AppState()
    transport = Transport(None)
    controller = Controller(
        None,
        app,
        transport,
        None,
        None,
        None,
        None,
        on_save=lambda: "pending",
        on_save_flush=lambda: False,
    )

    controller.dispatch(ButtonEvent("B2", "long"))
    controller._flush_pending_save(0)

    assert app.notice == seq2.SAVE_FAILED_TEXT


def test_pending_save_waits_for_clock_budget(monkeypatch):
    use_linear_ticks(monkeypatch)
    flushes = []
    app = AppState()
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = 10_000
    controller = Controller(
        None,
        app,
        transport,
        None,
        None,
        None,
        None,
        on_save=lambda: "pending",
        on_save_flush=lambda: flushes.append(True) or True,
    )

    controller.dispatch(ButtonEvent("B2", "long"))
    gap = [seq2.SAVE_GUARD_US - 1]
    monkeypatch.setattr(
        transport, "time_to_next_step_us", lambda now_us: gap[0]
    )
    controller._flush_pending_save(0)
    assert flushes == []

    gap[0] = seq2.SAVE_GUARD_US
    controller._flush_pending_save(0)
    assert flushes == [True]


def test_save_request_holds_an_immutable_project_snapshot():
    class Storage:
        def __init__(self):
            self.saved = None

        def save_state_json(self, state):
            self.saved = state

    sequencer = make_sequencer(1)
    transport = Transport(None)
    storage = Storage()
    persistence = PersistenceService(storage)
    project = ProjectState.capture(transport, sequencer.tracks)

    persistence.request(project)
    sequencer.tracks[0].engines["EUC"].g1.set_steps(9)
    assert persistence.flush()

    assert storage.saved["tracks"][0]["EUC"]["g1"]["steps"] == 4


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


def test_euclidean_pattern_matches_frozen_legacy_output_for_full_parameter_space():
    for steps in range(seq2.MIN_STEPS, seq2.MAX_STEPS + 1):
        for pulses in range(steps + 1):
            pattern = EuclidPattern(steps, pulses, 0, 100)
            for rotate in range(steps + 1):
                pattern.set_rot(rotate)
                actual = [int(pattern.output(pos)) for pos in range(steps)]
                assert actual == legacy_euclidean_pattern(steps, pulses, rotate), (
                    steps,
                    pulses,
                    rotate,
                )


def test_euclidean_parameter_clamping_is_stable():
    pattern = EuclidPattern(8, 3, 2, 50)

    pattern.set_steps(seq2.MAX_STEPS + 1)
    assert (pattern.steps, pattern.pulses, pattern.rot) == (seq2.MAX_STEPS, 3, 2)

    pattern.set_pulses(seq2.MAX_STEPS + 1)
    assert pattern.pulses == seq2.MAX_STEPS

    pattern.set_steps(4)
    assert (pattern.steps, pattern.pulses, pattern.rot) == (4, 4, 2)

    pattern.set_rot(99)
    pattern.set_prob(-1)
    assert pattern.rot == 4
    assert pattern.prob == 0


def test_euclidean_rotate_and_unchanged_values_do_not_regenerate(monkeypatch):
    pattern = EuclidPattern(16, 5, 0, 100)
    canonical = bytes(pattern.pattern)
    buffer_ids = tuple(id(buffer) for buffer in pattern._buffers)
    calls = []
    generate = seq2._generate_euclidean_into

    def count_generate(target, steps, pulses, workspace):
        calls.append((steps, pulses))
        return generate(target, steps, pulses, workspace)

    monkeypatch.setattr(seq2, "_generate_euclidean_into", count_generate)

    pattern.set_rot(7)
    pattern.set_prob(81)
    pattern.set_steps(16)
    pattern.set_pulses(5)

    assert calls == []
    assert bytes(pattern.pattern) == canonical
    assert tuple(id(buffer) for buffer in pattern._buffers) == buffer_ids

    pattern.set_pulses(6)
    pattern.set_steps(17)

    assert calls == [(16, 6), (17, 6)]
    assert tuple(id(buffer) for buffer in pattern._buffers) == buffer_ids


def test_euclidean_from_dict_regenerates_at_most_once(monkeypatch):
    pattern = EuclidPattern(16, 5, 0, 100)
    calls = []
    generate = seq2._generate_euclidean_into

    def count_generate(target, steps, pulses, workspace):
        calls.append((steps, pulses))
        return generate(target, steps, pulses, workspace)

    monkeypatch.setattr(seq2, "_generate_euclidean_into", count_generate)

    pattern.from_dict({"steps": 13, "pulses": 8, "rot": 12, "prob": 73})

    assert calls == [(13, 8)]
    assert pattern.to_dict() == {"steps": 13, "pulses": 8, "rot": 12, "prob": 73}


def test_euclidean_generation_publishes_only_a_complete_inactive_buffer(monkeypatch):
    pattern = EuclidPattern(8, 3, 2, 100)
    active_before = pattern._active_index
    output_before = [pattern.value_at(pos) for pos in range(pattern.steps)]
    generate = seq2._generate_euclidean_into

    def observe_generate(target, steps, pulses, workspace):
        assert pattern._active_index == active_before
        assert [pattern.value_at(pos) for pos in range(pattern.steps)] == output_before
        generate(target, steps, pulses, workspace)
        assert pattern._active_index == active_before
        assert [pattern.value_at(pos) for pos in range(pattern.steps)] == output_before

    monkeypatch.setattr(seq2, "_generate_euclidean_into", observe_generate)

    pattern.set_steps(13)

    assert pattern._active_index != active_before
    assert [pattern.value_at(pos) for pos in range(pattern.steps)] == (
        legacy_euclidean_pattern(13, 3, 2)
    )


def test_euclidean_track_output_and_gate_timing_golden(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    sequencer.add_track(make_track())
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 10_000)
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(0, 5), (3, 5)]
    assert not sequencer.pump(14_999)
    assert not sequencer.flush_outputs(hardware.set_cv)
    assert sequencer.pump(15_000)
    sequencer.flush_outputs(hardware.set_cv)
    assert hardware.cv_writes == [(0, 5), (3, 5), (0, 0), (3, 0)]


def test_cv_track_output_and_gate_timing_golden(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.set_type("CV")
    track.engines["CVSEQ"].values[1] = 64
    sequencer.add_track(track)
    transport = Transport(None)
    monkeypatch.setattr(transport, "beat_us", lambda: 100_000)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 20_000)
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(0, 5 * 64 / 127), (3, 5)]
    assert not sequencer.pump(69_999)
    assert sequencer.pump(70_000)
    sequencer.flush_outputs(hardware.set_cv)
    assert hardware.cv_writes == [(0, 5 * 64 / 127), (3, 5), (3, 0)]


def test_track_state_v1_round_trip_fixture():
    fixture = {
        "type": "CV",
        "v_lo": 1,
        "v_hi": 8,
        "EUC": {
            "g1": {"steps": 13, "pulses": 5, "rot": 4, "prob": 91},
            "g2": {"steps": 7, "pulses": 2, "rot": 7, "prob": 42},
            "merge": 2,
        },
        "CVSEQ": {
            "length": 5,
            "values": list(range(16)),
            "gate_len": 75,
            "pos": 3,
        },
    }
    track = make_track()

    track.from_dict(fixture)

    assert track.to_dict() == fixture


def test_v1_project_migrates_to_v2_without_runtime_positions():
    v1 = {
        "clock": {"source": "EXT", "bpm": 137, "mul": 6},
        "tracks": [
            {
                "type": "CV",
                "v_lo": 1,
                "v_hi": 8,
                "EUC": {
                    "g1": {"steps": 13, "pulses": 5, "rot": 4, "prob": 91},
                    "g2": {"steps": 7, "pulses": 2, "rot": 7, "prob": 42},
                    "merge": 2,
                },
                "CVSEQ": {
                    "length": 5,
                    "values": list(range(16)),
                    "gate_len": 75,
                    "pos": 3,
                },
            }
        ],
    }
    sequencer = make_sequencer(1)
    transport = Transport(None)
    transport.running = True

    project = ProjectState.decode(v1)
    project.apply(transport, sequencer)
    migrated = ProjectState.capture(transport, sequencer.tracks).to_dict()

    assert project.source_version == 1
    assert not transport.running
    assert migrated["schema_version"] == seq2.STATE_SCHEMA_VERSION
    assert migrated["clock"] == v1["clock"]
    assert "pos" not in migrated["tracks"][0]["CVSEQ"]
    assert sequencer.tracks[0].runtimes["CVSEQ"].pos == 0
    assert migrated["tracks"][0]["EUC"] == v1["tracks"][0]["EUC"]
    expected_cv = dict(v1["tracks"][0]["CVSEQ"])
    expected_cv.pop("pos")
    assert migrated["tracks"][0]["CVSEQ"] == expected_cv


def test_v2_project_round_trip_is_stable():
    source_sequencer = make_sequencer(3)
    source_sequencer.tracks[0].set_type("CV")
    source_sequencer.tracks[1].set_v_lo(2)
    source_sequencer.tracks[1].set_v_hi(9)
    source_sequencer.tracks[2].engines["EUC"].g1.set_rot(3)
    source_transport = Transport(None)
    source_transport.set_bpm(173)
    encoded = ProjectState.capture(
        source_transport, source_sequencer.tracks
    ).to_dict()
    target_sequencer = make_sequencer(3)
    target_transport = Transport(None)

    ProjectState.decode(encoded).apply(target_transport, target_sequencer)
    round_tripped = ProjectState.capture(
        target_transport, target_sequencer.tracks
    ).to_dict()

    assert round_tripped == encoded


def test_project_load_resets_all_runtime_state_and_pending_outputs(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.engines["EUC"].g1.set_pulses(4)
    transport = Transport(None)
    project = ProjectState.capture(transport, sequencer.tracks)

    tick_sequencer(sequencer, transport, 0)
    transport.running = True
    transport.base_tick_index = 17
    transport.internal_phase = 11
    assert track.runtimes["EUC"].positions == [0, 0]
    assert sequencer.outputs._gate_active[0]

    project.apply(transport, sequencer)

    assert not transport.running
    assert transport.beat_index == 0
    assert transport.base_tick_index == 0
    assert transport.internal_phase == 0
    assert track.runtimes["EUC"].positions == [3, 3]
    assert not any(sequencer.outputs._gate_active)
    assert not hasattr(track.runtimes["EUC"], "to_dict")
    assert not hasattr(track.runtimes["CVSEQ"], "to_dict")
    assert "pos" not in project.to_dict()["tracks"][0]["CVSEQ"]


def test_corrupt_track_does_not_prevent_other_tracks_from_loading():
    sequencer = make_sequencer(3)
    transport = Transport(None)
    state = {
        "schema_version": 2,
        "clock": {"source": "INVALID", "bpm": 999, "mul": -4},
        "tracks": [
            {"type": "OFF", "v_lo": 0, "v_hi": 5},
            "corrupt track",
            {
                "type": "CV",
                "v_lo": 9,
                "v_hi": 2,
                "CVSEQ": {"length": 99, "values": [-1, 999], "gate_len": 0},
            },
        ],
    }

    project = ProjectState.decode(state)
    project.apply(transport, sequencer)

    assert transport.source == "INT"
    assert (transport.bpm, transport.mul) == (seq2.MAX_BPM, seq2.MIN_MUL)
    assert sequencer.tracks[0].type == "OFF"
    assert sequencer.tracks[1].type == "EUC"
    track = sequencer.tracks[2]
    assert track.type == "CV"
    assert (track.v_lo, track.v_hi) == (2, 9)
    assert track.engines["CVSEQ"].length == seq2.CV_LEN
    assert track.engines["CVSEQ"].values[:2] == [0, seq2.CV_VAL_MAX]
    assert track.engines["CVSEQ"].gate_len == seq2.CV_GATE_MIN
    assert project.load_errors == 1


def test_unknown_project_schema_is_rejected():
    assert ProjectState.decode({"schema_version": 999}) is None


def test_project_missing_fields_and_unknown_type_keep_safe_defaults():
    sequencer = make_sequencer(2)
    sequencer.tracks[1].set_type("CV")
    transport = Transport(None)
    state = {
        "schema_version": 2,
        "tracks": [
            {},
            {"type": "UNKNOWN", "EUC": {"g1": {"steps": "bad"}}},
        ],
    }

    project = ProjectState.decode(state)
    project.apply(transport, sequencer)

    assert transport.to_dict() == {"source": "INT", "bpm": 120, "mul": 4}
    assert sequencer.tracks[0].type == "EUC"
    assert sequencer.tracks[1].type == "EUC"
    assert sequencer.tracks[1].engines["EUC"].g1.steps == 4
    assert project.load_errors == 1


def test_parameter_schemas_are_created_once_and_reused():
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    transport = Transport(None)
    app = AppState()

    global_params = build_global_params(sequencer, transport, app)
    euclid_params = track.engines["EUC"].param_defs(track)
    track.set_type("CV")
    cv_params = track.engines["CVSEQ"].param_defs(track)
    cv_slots = track.engines["CVSEQ"].slots(track)

    for _ in range(1_000):
        assert build_global_params(sequencer, transport, app) is global_params
        assert track.engines["EUC"].param_defs(track) is euclid_params
        assert track.engines["CVSEQ"].param_defs(track) is cv_params
        assert track.engines["CVSEQ"].slots(track) is cv_slots

    assert not hasattr(seq2, "Command")


def test_switching_track_features_preserves_both_configurations():
    track = make_track()
    track.engines["EUC"].g1.set_steps(11)
    track.engines["EUC"].g1.set_pulses(7)

    track.set_type("CV")
    track.engines["CVSEQ"].set_length(5)
    track.engines["CVSEQ"].set_step(2, 99)

    track.set_type("EUC")
    assert track.engines["EUC"].g1.to_dict() == {
        "steps": 11,
        "pulses": 7,
        "rot": 0,
        "prob": 100,
    }

    track.set_type("CV")
    assert track.engines["CVSEQ"].length == 5
    assert track.engines["CVSEQ"].values[2] == 99


def test_pages_pickup_and_dynamic_parameter_ranges_use_editor_service():
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    transport = Transport(None)
    app = AppState()
    app._set_page(1)
    controller = Controller(None, app, transport, sequencer, None, None, None)
    pages = Pages(app, transport, sequencer, controller.editor.set)

    pages.on_knob2(1.0)
    assert track.engines["EUC"].g1.rot == 0
    assert not app.k2_picked

    pages.on_knob2(0.0)
    assert app.k2_picked
    pages.on_knob2(1.0)
    assert track.engines["EUC"].g1.rot == 4

    track.engines["EUC"].g1.set_steps(2)
    pages.on_knob2(0.5)
    assert track.engines["EUC"].g1.rot == 1

    track.set_type("CV")
    assert track.feature.slot_count(track) == 4 + track.engines["CVSEQ"].length
    track.engines["CVSEQ"].set_length(3)
    assert track.feature.slot_count(track) == 7

    track.set_type("OFF")
    assert track.feature.slot_count(track) == 0
    pages.on_knob1(0.5)


def test_registered_track_feature_runs_without_sequencer_changes(monkeypatch):
    use_linear_ticks(monkeypatch)

    class ConstantEngine:
        def __init__(self):
            self.voltage = 2.5
            self.reset_count = 0
            self.update_count = 0

        def reset(self):
            self.reset_count += 1

        def slots(self, track):
            return ()

    class ConstantCVFeature(TrackFeature):
        def __init__(self):
            super().__init__("CONST", "CONST", "CONST")

        def create_engine(self, track, g1, g2, merge):
            return ConstantEngine()

        def on_tick(self, track, outputs, context):
            outputs.set_cv(track.cv_index, track.engines["CONST"].voltage)
            outputs.cancel_channel(track.gate_index, 0)

        def update(self, track, outputs, now_us, context):
            track.engines["CONST"].update_count += 1

        def write_snapshot(self, track, app, transport, snapshot):
            pass

        def render(self, snapshot, hw):
            pass

        def encode_project(self, track):
            return {"voltage": track.engines["CONST"].voltage}

        def decode_project(self, track, data, project=False):
            track.engines["CONST"].voltage = data.get("voltage", 2.5)

    register_track_feature(ConstantCVFeature())
    try:
        sequencer = Sequencer()
        track = make_track()
        track.set_type("CONST")
        sequencer.add_track(track)
        hardware = FakeHardware()

        transport = Transport(None)
        tick_sequencer(sequencer, transport, 0)
        sequencer.update(1, transport.tick_context)
        sequencer.flush_outputs(hardware.set_cv)

        assert hardware.cv_writes == [(0, 2.5), (3, 0)]
        assert track.engines["CONST"].update_count == 1
        assert track.to_project_dict()["CONST"] == {"voltage": 2.5}
        reset_count = track.engines["CONST"].reset_count
        sequencer.reset()
        assert track.engines["CONST"].reset_count == reset_count + 1
    finally:
        seq2.TRACK_FEATURES.pop("CONST")


def test_renderer_reads_a_stable_view_snapshot():
    class DisplayHardware:
        def __init__(self):
            self.rects = []
            self.text = []

        def display_clear(self):
            pass

        def display_text(self, text, x, y):
            self.text.append((text, x, y))

        def display_fill_rect(self, x, y, width, height, color):
            self.rects.append((x, y, width, height, color))

        def display_show(self):
            pass

    sequencer = make_sequencer(1)
    transport = Transport(None)
    app = AppState()
    app._set_page(1)
    snapshot = ViewSnapshot()
    snapshot.write(app, sequencer, transport)
    frozen_bits = bytes(snapshot.sequence_a)

    sequencer.tracks[0].engines["EUC"].g1.set_rot(1)
    hardware = DisplayHardware()
    Renderer().render(snapshot, hardware)

    assert bytes(snapshot.sequence_a) == frozen_bits
    assert len(hardware.rects) == 33


def test_euclid_beat_redraw_only_updates_playhead_band():
    class DisplayHardware:
        def __init__(self):
            self.clears = 0
            self.rects = []
            self.text = []

        def display_clear(self):
            self.clears += 1

        def display_text(self, text, x, y):
            self.text.append((text, x, y))

        def display_fill_rect(self, x, y, width, height, color):
            self.rects.append((x, y, width, height, color))

    sequencer = make_sequencer(1)
    transport = Transport(None)
    app = AppState()
    app._set_page(1)
    snapshot = ViewSnapshot()
    renderer = Renderer()

    snapshot.write(app, sequencer, transport)
    renderer.draw(snapshot, DisplayHardware())
    app.redraw_all = False
    snapshot.write(app, sequencer, transport)
    hardware = DisplayHardware()
    renderer.draw(snapshot, hardware)

    assert hardware.clears == 0
    assert hardware.text == []
    assert hardware.rects[0] == (0, 26, seq2.OLED_WIDTH, 6, 0)
    assert len(hardware.rects) == 2


def test_render_and_pending_save_do_not_change_active_gate(monkeypatch):
    use_linear_ticks(monkeypatch)

    class DisplayHardware:
        def display_clear(self):
            pass

        def display_text(self, text, x, y):
            pass

        def display_fill_rect(self, x, y, width, height, color):
            pass

        def display_show(self):
            pass

    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.engines["EUC"].g1.set_pulses(4)
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = 100_000
    app = AppState()
    app._set_page(1)
    controller = Controller(
        None,
        app,
        transport,
        sequencer,
        None,
        None,
        None,
        on_save=lambda: "pending",
        on_save_flush=lambda: True,
    )

    tick_sequencer(sequencer, transport, 0)
    deadline = sequencer.outputs._gate_deadline[0]
    snapshot = ViewSnapshot()
    snapshot.write(app, sequencer, transport)
    Renderer().render(snapshot, DisplayHardware())
    controller.dispatch(ButtonEvent("B2", "long"))
    controller._flush_pending_save(0)

    assert deadline == seq2.GATE_MS * 1000
    assert sequencer.outputs._gate_deadline[0] == deadline
    assert sequencer.outputs._gate_active[0]
    assert app.notice == seq2.SAVE_NOTICE_TEXT


def test_output_scheduler_reports_nearest_gate_edge(monkeypatch):
    use_linear_ticks(monkeypatch)
    scheduler = OutputScheduler(3)
    scheduler.trigger_gate(0, 5, 0, 8_000, 1_000)
    scheduler.trigger_gate(1, 5, 0, 3_000, 1_000)

    assert scheduler.time_to_next_edge_us(2_000) == 2_000
    assert scheduler.time_to_next_edge_us(4_500) == 0

    scheduler.update(4_500)
    assert scheduler.time_to_next_edge_us(4_500) == 4_500


def test_display_is_committed_one_page_at_a_time_with_24ppqn_budget(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [0]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])

    class DisplayHardware(FakeHardware):
        def __init__(self):
            super().__init__()
            self.pages = []

    class PageRenderer:
        def __init__(self):
            self.draws = 0

        def draw(self, snapshot, hardware):
            self.draws += 1

        def show_page(self, page, hardware):
            hardware.pages.append(page)

    hardware = DisplayHardware()
    renderer = PageRenderer()
    sequencer = make_sequencer(1)
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = seq2.DISPLAY_TIMING_MIN_PERIOD_US
    app = AppState()
    controller = Controller(
        hardware,
        app,
        transport,
        sequencer,
        None,
        renderer,
        None,
    )

    assert seq2.DISPLAY_TIMING_MIN_PERIOD_US == 10_416
    assert controller._prepare_display()
    assert renderer.draws == 1
    assert not app.dirty

    assert controller._flush_display_page(0)
    assert hardware.pages == [0]
    assert controller._flush_display_page(0)
    assert hardware.pages == [0, 1]
    assert controller._flush_display_page(0)
    assert controller._flush_display_page(0)
    assert hardware.pages == [0, 1, 2, 3]
    assert controller._display_page == -1
    assert not controller._flush_display_page(0)


def test_view_snapshot_reuses_header_until_selected_value_changes():
    sequencer = make_sequencer(1)
    transport = Transport(None)
    app = AppState()
    snapshot = ViewSnapshot()

    snapshot.write(app, sequencer, transport)
    original_header = snapshot.header_value
    snapshot.write(app, sequencer, transport)
    assert snapshot.header_value is original_header

    app.sel = 1
    snapshot.write(app, sequencer, transport)
    bpm_header = snapshot.header_value
    snapshot.write(app, sequencer, transport)
    assert snapshot.header_value is bpm_header

    transport.bpm = 121
    snapshot.write(app, sequencer, transport)
    assert snapshot.header_value == "BPM:121"
    assert snapshot.header_value is not bpm_header


def test_display_page_waits_for_clock_and_gate_slack(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [0]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])

    class DisplayHardware(FakeHardware):
        def __init__(self):
            super().__init__()
            self.pages = []

    class PageRenderer:
        def draw(self, snapshot, hardware):
            pass

        def show_page(self, page, hardware):
            hardware.pages.append(page)

    hardware = DisplayHardware()
    sequencer = make_sequencer(1)
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = seq2.DISPLAY_TIMING_MIN_PERIOD_US
    app = AppState()
    controller = Controller(
        hardware,
        app,
        transport,
        sequencer,
        None,
        PageRenderer(),
        None,
    )
    controller._prepare_display()

    sequencer.outputs.trigger_gate(0, 5, 0, seq2.OLED_PAGE_GUARD_US, 0)
    assert not controller._flush_display_page(0)
    assert hardware.pages == []

    now_us[0] = seq2.OLED_PAGE_GUARD_US
    assert not controller._flush_display_page(now_us[0])
    assert hardware.pages == []
    assert hardware.cv_writes == [(0, 0)]

    now_us[0] = seq2.DISPLAY_TIMING_MIN_PERIOD_US
    transport.update(now_us[0])
    assert controller._flush_display_page(now_us[0])
    assert hardware.pages == [0]

    transport.next_clock_us = now_us[0] + seq2.OLED_PAGE_GUARD_US
    assert not controller._flush_display_page(now_us[0])
    assert hardware.pages == [0]


def test_display_render_waits_for_gate_slack_and_services_due_edge(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [0]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])

    class PageRenderer:
        def __init__(self):
            self.draws = 0

        def draw(self, snapshot, hardware):
            self.draws += 1

        def show_page(self, page, hardware):
            pass

    hardware = FakeHardware()
    renderer = PageRenderer()
    sequencer = make_sequencer(1)
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = seq2.DISPLAY_TIMING_MIN_PERIOD_US
    app = AppState()
    controller = Controller(
        hardware,
        app,
        transport,
        sequencer,
        None,
        renderer,
        None,
    )

    sequencer.outputs.trigger_gate(0, 5, 0, 3_000, 0)
    sequencer.flush_outputs(hardware.set_cv)
    assert not controller._prepare_display(0)
    assert renderer.draws == 0
    assert app.dirty

    now_us[0] = 3_000
    assert controller._prepare_display(now_us[0])
    assert renderer.draws == 1
    assert hardware.cv_writes == [(0, 5), (0, 0)]


def test_full_render_uses_measured_render_guard_for_active_gate(monkeypatch):
    use_linear_ticks(monkeypatch)
    monkeypatch.setattr(seq2, "ticks_us", lambda: 0)

    class PageRenderer:
        def __init__(self):
            self.draws = 0

        def draw(self, snapshot, hardware):
            self.draws += 1

        def show_page(self, page, hardware):
            pass

    hardware = FakeHardware()
    renderer = PageRenderer()
    sequencer = make_sequencer(1)
    transport = Transport(None)
    transport.running = True
    transport.next_clock_us = seq2.DISPLAY_TIMING_MIN_PERIOD_US
    app = AppState()
    controller = Controller(
        hardware,
        app,
        transport,
        sequencer,
        None,
        renderer,
        None,
    )

    sequencer.outputs.trigger_gate(
        0, 5, 0, seq2.OLED_FULL_RENDER_GUARD_US, 0
    )
    sequencer.flush_outputs(hardware.set_cv)
    assert not controller._prepare_display(0)
    assert renderer.draws == 0

    sequencer.outputs.trigger_gate(
        0, 5, 0, seq2.OLED_FULL_RENDER_GUARD_US + 1, 0
    )
    assert controller._prepare_display(0)
    assert renderer.draws == 1


def test_seq2_application_smoke_uses_v2_state_and_snapshot_renderer(monkeypatch):
    monkeypatch.setattr(seq2, "SAVE_STATES", False)
    monkeypatch.setattr(seq2.Hardware, "on_clock_rise", lambda self, callback: None)
    application = seq2.Seq2()

    application.controller._on_beat()
    application.controller.snapshot.write(
        application.app, application.seq, application.transport
    )
    application.renderer = application.controller.renderer
    application.renderer.render(application.controller.snapshot, application.hw)
    state = application.get_state()

    assert state["schema_version"] == seq2.STATE_SCHEMA_VERSION
    assert len(state["tracks"]) == seq2.NUM_TRACKS
    assert "pos" not in state["tracks"][0]["CVSEQ"]


def test_clock_service_late_tick_keeps_absolute_phase(monkeypatch):
    clock = FakeClock(1_100)
    ticks = []
    service = ClockService()
    service.running = True
    service.next_deadline_us = 1_000
    use_linear_ticks(monkeypatch)

    service.update(clock.now_us, 250, lambda *_: ticks.append(clock.now_us))

    assert ticks == [1_100]
    assert service.next_deadline_us == 1_250
    assert service.last_lateness_us == 100


def test_clock_service_catches_up_multiple_due_ticks(monkeypatch):
    use_linear_ticks(monkeypatch)
    ticks = []
    service = ClockService()
    service.running = True
    service.next_deadline_us = 1_000

    emitted = service.update(1_750, 250, lambda *_: ticks.append(True))

    assert emitted == 4
    assert len(ticks) == 4
    assert service.next_deadline_us == 2_000
    assert service.dropped_ticks == 0
    assert service.max_lateness_us == 750


def test_transport_reuses_tick_context_with_scheduled_and_actual_times(monkeypatch):
    use_linear_ticks(monkeypatch)
    captured = []

    def on_tick(context):
        captured.append(
            (
                id(context),
                context.tick_index,
                context.scheduled_us,
                context.actual_us,
                context.period_us,
            )
        )

    transport = Transport(on_tick)
    transport.mul = 8
    transport.running = True
    transport.next_clock_us = 1_000
    monkeypatch.setattr(transport, "base_tick_us", lambda: 250)

    transport.update(1_500)

    assert captured == [
        (id(transport.tick_context), 0, 1_500, 1_500, 750),
    ]
    assert transport.base_tick_index == 3


def test_internal_clock_emits_mul_steps_per_24ppqn_quarter(monkeypatch):
    use_linear_ticks(monkeypatch)
    base_period_us = 10_416

    for mul in range(seq2.MIN_MUL, seq2.MAX_MUL + 1):
        contexts = []
        transport = Transport(
            lambda context: contexts.append(
                (
                    context.tick_index,
                    context.scheduled_us,
                    context.period_us,
                )
            )
        )
        transport.bpm = 240
        transport.mul = mul
        transport.running = True
        transport.next_clock_us = base_period_us
        monkeypatch.setattr(
            transport, "base_tick_us", lambda: base_period_us
        )

        base_tick = 1
        while base_tick <= seq2.INTERNAL_PPQN:
            transport.update(base_tick * base_period_us)
            base_tick += 1

        assert len(contexts) == mul
        assert [context[0] for context in contexts] == list(range(mul))
        assert transport.base_tick_index == seq2.INTERNAL_PPQN
        assert transport.internal_phase == 0


def test_24ppqn_mul_eight_is_uniform_and_mul_five_is_evenly_distributed(monkeypatch):
    use_linear_ticks(monkeypatch)
    base_period_us = 10_416

    def scheduled_ticks(mul):
        scheduled = []
        transport = Transport(
            lambda context: scheduled.append(
                context.scheduled_us // base_period_us
            )
        )
        transport.mul = mul
        transport.running = True
        transport.next_clock_us = base_period_us
        monkeypatch.setattr(
            transport, "base_tick_us", lambda: base_period_us
        )
        base_tick = 1
        while base_tick <= seq2.INTERNAL_PPQN:
            transport.update(base_tick * base_period_us)
            base_tick += 1
        return scheduled

    assert scheduled_ticks(8) == [3, 6, 9, 12, 15, 18, 21, 24]
    assert scheduled_ticks(5) == [5, 10, 15, 20, 24]


def test_internal_step_period_tracks_next_24ppqn_grid_interval(monkeypatch):
    use_linear_ticks(monkeypatch)
    base_period_us = 10_416
    periods = []
    transport = Transport(lambda context: periods.append(context.period_us))
    transport.mul = 5
    transport.running = True
    transport.next_clock_us = base_period_us
    monkeypatch.setattr(transport, "base_tick_us", lambda: base_period_us)

    base_tick = 1
    while base_tick <= seq2.INTERNAL_PPQN:
        transport.update(base_tick * base_period_us)
        base_tick += 1

    assert periods == [
        5 * base_period_us,
        5 * base_period_us,
        5 * base_period_us,
        4 * base_period_us,
        5 * base_period_us,
    ]
    assert transport.metrics()["internal_ppqn"] == 24
    assert transport.metrics()["internal_base_ticks"] == 24


def test_24ppqn_phase_has_no_long_term_step_count_drift(monkeypatch):
    use_linear_ticks(monkeypatch)
    base_period_us = 10_416
    transport = Transport(lambda context: None)
    transport.mul = 7
    transport.running = True
    transport.next_clock_us = base_period_us
    monkeypatch.setattr(transport, "base_tick_us", lambda: base_period_us)

    base_tick = 1
    while base_tick <= 10_000:
        transport.update(base_tick * base_period_us)
        base_tick += 1

    assert transport.beat_index == 10_000 * 7 // seq2.INTERNAL_PPQN
    assert transport.internal_phase == 10_000 * 7 % seq2.INTERNAL_PPQN
    assert transport.base_tick_index == 10_000


def test_internal_base_period_uses_24ppqn():
    transport = Transport(None)

    transport.bpm = 240
    assert transport.base_tick_us() == 10_416
    transport.bpm = 120
    assert transport.base_tick_us() == 20_833


def test_time_to_next_step_skips_empty_24ppqn_base_ticks(monkeypatch):
    use_linear_ticks(monkeypatch)
    transport = Transport(None)
    transport.bpm = 240
    transport.mul = 8
    transport.running = True
    transport.next_clock_us = transport.base_tick_us()

    assert transport.time_to_next_clock_us(0) == 10_416
    assert transport.time_to_next_step_us(0) == 31_248

    transport.update(10_416)
    assert transport.internal_phase == 8
    assert transport.time_to_next_step_us(10_416) == 20_832


def test_clock_service_catch_up_is_bounded_and_reports_dropped_ticks(monkeypatch):
    use_linear_ticks(monkeypatch)
    ticks = []
    service = ClockService(max_catch_up=4)
    service.running = True
    service.next_deadline_us = 1_000

    emitted = service.update(1_650, 100, lambda *_: ticks.append(True))

    assert emitted == 4
    assert len(ticks) == 4
    assert service.next_deadline_us == 1_700
    assert service.overruns == 1
    assert service.dropped_ticks == 3
    assert service.metrics()["dropped_ticks"] == 3
    assert service.last_dropped_ticks == 3


def test_dropped_base_ticks_advance_24ppqn_phase_without_output(monkeypatch):
    use_linear_ticks(monkeypatch)
    scheduled = []
    transport = Transport(
        lambda context: scheduled.append(context.scheduled_us)
    )
    transport.mul = 8
    transport.running = True
    transport.next_clock_us = 1_000
    transport.max_catch_up = 4
    monkeypatch.setattr(transport, "base_tick_us", lambda: 100)

    assert transport.update(1_650) == 1
    assert scheduled == [1_200]
    assert transport.dropped_ticks == 3
    assert transport.dropped_music_steps == 1
    assert transport.internal_phase == 8

    transport.update(1_800)
    assert scheduled == [1_200, 1_800]
    assert transport.internal_phase == 0


def test_clock_service_deadline_math_survives_ticks_wrap(monkeypatch):
    modulus = 1_024

    def wrapped_add(value, delta):
        return (value + delta) % modulus

    def wrapped_diff(left, right):
        return ((left - right + modulus // 2) % modulus) - modulus // 2

    monkeypatch.setattr(seq2, "ticks_add", wrapped_add)
    monkeypatch.setattr(seq2, "ticks_diff", wrapped_diff)
    ticks = []
    service = ClockService()
    service.running = True
    service.next_deadline_us = 1_000

    emitted = service.update(20, 30, lambda *_: ticks.append(True))

    assert emitted == 2
    assert service.next_deadline_us == 36
    assert len(ticks) == 2


def test_clock_capture_is_bounded_and_preserves_timestamp_order():
    capture = ClockCapture(3)

    assert capture.push(10)
    assert capture.push(20)
    assert capture.push(30)
    assert not capture.push(40)
    assert capture.count == 3
    assert capture.overflows == 1
    assert [capture.pop(), capture.pop(), capture.pop(), capture.pop()] == [
        10,
        20,
        30,
        None,
    ]


def test_external_transport_records_clock_timestamps(monkeypatch):
    use_linear_ticks(monkeypatch)
    ticks = []
    transport = Transport(lambda context: ticks.append(True))
    transport.set_source("EXT")

    transport.ext_tick(10_000)
    transport.ext_tick(12_500)

    assert len(ticks) == 2
    assert transport.beat_index == 2
    assert transport.external_ticks == 2
    assert transport.last_external_interval_us == 2_500


def test_external_clock_reports_jitter_timeout_and_clean_recovery(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [1_000]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    contexts = []
    transport = Transport(
        lambda context: contexts.append(
            (context.scheduled_us, context.actual_us, context.period_us)
        )
    )
    transport.set_source("EXT")

    transport.ext_tick(1_000)
    now_us[0] = 2_000
    transport.ext_tick(2_000)
    now_us[0] = 3_100
    transport.ext_tick(3_100)

    assert transport.last_external_interval_us == 1_100
    assert transport.external_jitter_us == 100
    assert transport.max_external_jitter_us == 100
    assert contexts[-1] == (3_100, 3_100, 1_100)

    assert transport.update(7_500) == 0
    assert transport.external_timed_out
    assert transport.external_timeouts == 1

    now_us[0] = 8_000
    transport.ext_tick(8_000)

    assert not transport.external_timed_out
    assert transport.last_external_interval_us == 0
    assert transport.external_jitter_us == 0
    assert contexts[-1][0] == 8_000


def test_input_manager_drains_bounded_clock_capture_with_timestamps():
    class InputHardware:
        def __init__(self):
            self.capture = ClockCapture(3)

        def button1(self):
            return False

        def button2(self):
            return False

        def knob1(self):
            return 0.0

        def knob2(self):
            return 0.0

        def pop_clock_timestamp(self):
            return self.capture.pop()

    hardware = InputHardware()
    hardware.capture.push(101)
    hardware.capture.push(202)
    manager = InputManager(hardware)

    clock_events = [
        event for event in manager.poll() if isinstance(event, ClockEvent)
    ]

    assert [event.timestamp_us for event in clock_events] == [101, 202]
    assert hardware.capture.count == 0

    event_ids = [id(event) for event in clock_events]
    hardware.capture.push(303)
    hardware.capture.push(404)
    reused_events = [
        event for event in manager.poll() if isinstance(event, ClockEvent)
    ]
    assert [id(event) for event in reused_events] == event_ids


def test_input_manager_tracks_motion_then_suppresses_static_knob_jitter(monkeypatch):
    now_ms = [1_000]
    monkeypatch.setattr(seq2, "ticks_ms", lambda: now_ms[0])
    use_linear_ticks(monkeypatch)

    class InputHardware:
        k1_value = 0.5
        k2_value = 0.5

        def button1(self):
            return False

        def button2(self):
            return False

        def knob1(self):
            return self.k1_value

        def knob2(self):
            return self.k2_value

        def pop_clock_timestamp(self):
            return None

    hardware = InputHardware()
    manager = InputManager(hardware)

    assert len([event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]) == 2

    hardware.k1_value = 0.505
    hardware.k2_value = 0.505
    assert not [event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]

    hardware.k1_value = 0.515
    hardware.k2_value = 0.515
    assert len([event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]) == 2

    now_ms[0] += 50
    hardware.k1_value = 0.519
    hardware.k2_value = 0.519
    moving = [event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]
    assert [event.value for event in moving] == [0.519, 0.519]

    now_ms[0] += seq2.KNOB_ACTIVE_TIMEOUT_MS + 1
    hardware.k1_value = 0.523
    hardware.k2_value = 0.523
    assert not [event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]


def test_input_manager_reaches_both_knob_endpoints_while_active(monkeypatch):
    now_ms = [1_000]
    monkeypatch.setattr(seq2, "ticks_ms", lambda: now_ms[0])
    use_linear_ticks(monkeypatch)

    class InputHardware:
        k1_value = 0.5
        k2_value = 0.5

        def button1(self):
            return False

        def button2(self):
            return False

        def knob1(self):
            return self.k1_value

        def knob2(self):
            return self.k2_value

        def pop_clock_timestamp(self):
            return None

    hardware = InputHardware()
    manager = InputManager(hardware)
    manager.poll()

    hardware.k1_value = 0.99
    hardware.k2_value = 0.99
    assert len([event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]) == 2

    now_ms[0] += 50
    hardware.k1_value = 1.0
    hardware.k2_value = 1.0
    high_events = [event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]
    assert [event.value for event in high_events] == [1.0, 1.0]

    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.engines["EUC"].g1.set_prob(99)
    transport = Transport(None)
    app = AppState()
    app._set_page(1)
    app.sel = 6  # PRB1
    app.k2_picked = True
    controller = Controller(None, app, transport, sequencer, None, None, None)
    pages = Pages(app, transport, sequencer, controller.editor.set)
    controller.pages = pages
    controller.dispatch(high_events[1])
    assert track.engines["EUC"].g1.prob == 100

    hardware.k1_value = 0.01
    hardware.k2_value = 0.01
    assert len([event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]) == 2

    now_ms[0] += 50
    hardware.k1_value = 0.0
    hardware.k2_value = 0.0
    low_events = [event for event in manager.poll() if isinstance(event, seq2.KnobTurn)]
    assert [event.value for event in low_events] == [0.0, 0.0]


def test_din_schedule_failure_is_counted(monkeypatch):
    class Pin:
        IRQ_FALLING = 4

        def irq(self, trigger, handler):
            self.handler = handler

    class Din:
        pin = Pin()

    hardware = seq2.Hardware.__new__(seq2.Hardware)
    hardware.din = Din()
    hardware.clock_capture = ClockCapture(1)
    hardware.clock_schedule_failures = 0

    def fail_schedule(callback, argument):
        raise RuntimeError("schedule queue full")

    monkeypatch.setattr(seq2.micropython, "schedule", fail_schedule, raising=False)
    hardware.on_clock_rise(lambda timestamp: None)

    hardware.din.pin.handler()

    assert hardware.clock_schedule_failures == 1
    assert hardware.dropped_clock_events == 1


def test_transport_start_continue_and_reset_are_distinct(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [1_000]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    transport = Transport(None)
    monkeypatch.setattr(transport, "base_tick_us", lambda: 250)

    transport.start()
    assert transport.running
    assert transport.beat_index == 0
    assert transport.next_clock_us == 1_250

    transport.beat_index = 7
    transport.stop()
    now_us[0] = 2_000
    transport.continue_()
    assert transport.running
    assert transport.beat_index == 7
    assert transport.next_clock_us == 2_250

    now_us[0] = 3_000
    transport.reset()
    assert transport.running
    assert transport.beat_index == 0
    assert transport.next_clock_us == 3_250


def test_transport_runtime_callbacks_reset_players_and_cancel_deadlines(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [1_000]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.engines["EUC"].g1.set_pulses(4)
    transport = Transport(None)
    transport.set_runtime_callbacks(
        on_stop=sequencer.cancel_all,
        on_reset=sequencer.reset,
    )

    tick_sequencer(sequencer, transport, 0)
    assert track.runtimes["EUC"].positions == [0, 0]
    assert sequencer.outputs._gate_active[0]

    transport.running = True
    transport.stop()
    assert not sequencer.outputs._gate_active[0]
    assert track.runtimes["EUC"].positions == [0, 0]

    now_us[0] = 2_000
    transport.continue_()
    assert track.runtimes["EUC"].positions == [0, 0]

    transport.stop()
    now_us[0] = 3_000
    transport.start()
    assert track.runtimes["EUC"].positions == [3, 3]


def test_running_tempo_change_reschedules_without_resetting_position(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [1_000]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    transport = Transport(None)
    transport.start()
    transport.beat_index = 9

    now_us[0] = 2_000
    transport.set_bpm(150)

    assert transport.running
    assert transport.beat_index == 9
    assert transport.next_clock_us == 2_000 + transport.base_tick_us()

    transport.beat_index = 10
    now_us[0] = 3_000
    transport.set_mul(5)
    assert transport.beat_index == 10
    assert transport.next_clock_us == 3_000 + transport.base_tick_us()


def test_internal_external_source_transitions_have_explicit_start_stop_semantics(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [1_000]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    transport = Transport(None)
    transport.start()
    transport.beat_index = 7

    transport.set_source("EXT")
    assert transport.source == "EXT"
    assert not transport.running
    assert transport.beat_index == 7

    now_us[0] = 2_000
    transport.set_source("INT")
    assert transport.source == "INT"
    assert transport.running
    assert transport.beat_index == 0
    assert transport.next_clock_us == 2_000 + transport.base_tick_us()


def test_retrigger_replaces_old_gate_deadline(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.engines["EUC"].g1.set_pulses(4)
    sequencer.add_track(track)
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    tick_sequencer(sequencer, transport, 4_000)

    assert not sequencer.pump(5_000)
    assert not sequencer.flush_outputs(hardware.set_cv)
    assert sequencer.pump(9_000)
    sequencer.flush_outputs(hardware.set_cv)
    assert hardware.cv_writes == [(0, 5), (3, 5), (0, 0), (3, 0)]


def test_output_scheduler_skips_duplicate_voltage_writes(monkeypatch):
    use_linear_ticks(monkeypatch)
    outputs = OutputScheduler(2)
    hardware = FakeHardware()

    outputs.set_cv(0, 3.5)
    outputs.flush(hardware.set_cv)
    outputs.set_cv(0, 3.5)
    outputs.flush(hardware.set_cv)

    assert hardware.cv_writes == [(0, 3.5)]


def test_cv_max_gate_length_keeps_low_gap_between_consecutive_steps(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.set_type("CV")
    track.engines["CVSEQ"].set_gate_len(seq2.CV_GATE_MAX)
    track.engines["CVSEQ"].values[1] = 64
    track.engines["CVSEQ"].values[2] = 65
    sequencer.add_track(track)
    transport = Transport(None)
    monkeypatch.setattr(transport, "beat_us", lambda: 100_000)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    assert sequencer.pump(90_000)
    sequencer.flush_outputs(hardware.set_cv)
    tick_sequencer(sequencer, transport, 100_000)
    sequencer.pump(100_000)
    sequencer.flush_outputs(hardware.set_cv)

    gate_writes = [write for write in hardware.cv_writes if write[0] == 3]
    assert gate_writes == [(3, 5), (3, 0), (3, 5)]

    assert sequencer.pump(190_000)
    sequencer.flush_outputs(hardware.set_cv)
    gate_writes = [write for write in hardware.cv_writes if write[0] == 3]
    assert gate_writes == [(3, 5), (3, 0), (3, 5), (3, 0)]


def test_cv_rest_step_cancels_gate_and_keeps_low_pitch(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.set_type("CV")
    track.set_v_lo(1)
    track.engines["CVSEQ"].values[1] = 0
    sequencer.add_track(track)
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(0, 1.0), (3, 1)]
    assert not sequencer.outputs._gate_active[3]


def test_bpm_change_does_not_move_an_active_gate_deadline(monkeypatch):
    use_linear_ticks(monkeypatch)
    now_us = [0]
    monkeypatch.setattr(seq2, "ticks_us", lambda: now_us[0])
    sequencer = Sequencer()
    track = make_track()
    track.set_type("CV")
    track.engines["CVSEQ"].values[1] = 64
    track.engines["CVSEQ"].gate_len = 50
    sequencer.add_track(track)
    transport = Transport(None)
    monkeypatch.setattr(transport, "beat_us", lambda: 100_000)

    tick_sequencer(sequencer, transport, 0)
    deadline = sequencer.outputs._gate_deadline[3]
    transport.running = True
    now_us[0] = 10_000
    transport.set_bpm(180)

    assert deadline == 50_000
    assert sequencer.outputs._gate_deadline[3] == deadline


def test_three_tracks_keep_all_six_outputs_isolated(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = make_sequencer(3)
    for track in sequencer.tracks:
        track.engines["EUC"].g1.set_pulses(4)
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(channel, 5) for channel in range(6)]
    assert [
        (track.cv_index, track.gate_index) for track in sequencer.tracks
    ] == [(0, 3), (1, 4), (2, 5)]

    assert sequencer.pump(seq2.GATE_MS * 1000)
    sequencer.flush_outputs(hardware.set_cv)
    assert hardware.cv_writes[-6:] == [(channel, 0) for channel in range(6)]


def test_track_type_change_cancels_pending_gates(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.engines["EUC"].g1.set_pulses(4)
    sequencer.add_track(track)
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    track.set_type("OFF")
    sequencer.sync_track_types()
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(0, 5), (3, 5), (0, 0), (3, 0)]
    assert not sequencer.pump(5_000)


def test_null_feature_drives_its_fixed_pair_low_once(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.set_type("OFF")
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    tick_sequencer(sequencer, transport, 10_000)
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [(0, 0), (3, 0)]


def test_sequencer_reset_cancels_pending_gates_and_resets_players(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = make_sequencer(1)
    track = sequencer.tracks[0]
    track.engines["EUC"].g1.set_pulses(4)
    transport = Transport(None)
    hardware = FakeHardware()

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    sequencer.reset()
    sequencer.flush_outputs(hardware.set_cv)

    assert hardware.cv_writes == [
        (0, 5),
        (3, 5),
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
        (5, 0),
    ]
    assert track.runtimes["EUC"].positions == [3, 3]
    assert not sequencer.pump(5_000)


def test_output_scheduler_counts_late_deadlines(monkeypatch):
    use_linear_ticks(monkeypatch)
    outputs = OutputScheduler(1)

    outputs.trigger_gate(0, 5, 0, 5_000, 10_000)
    assert outputs.update(15_250)

    assert outputs.late_edges == 1
    assert outputs.last_late_edge_us == 250
    assert outputs.max_late_edge_us == 250
    assert outputs.metrics() == {
        "late_edges": 1,
        "last_late_edge_us": 250,
        "max_late_edge_us": 250,
    }


def test_stopping_internal_transport_cancels_outputs(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.engines["EUC"].g1.set_pulses(4)
    sequencer.add_track(track)
    transport = Transport(None)
    transport.running = True
    hardware = FakeHardware()
    controller = Controller(
        hardware, AppState(), transport, sequencer, None, None, None
    )

    tick_sequencer(sequencer, transport, 0)
    sequencer.flush_outputs(hardware.set_cv)
    controller.dispatch(ButtonEvent("B1", "long"))

    assert not transport.running
    assert hardware.cv_writes == [
        (0, 5),
        (3, 5),
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
        (5, 0),
    ]
    assert not sequencer.pump(5_000)


def test_output_scheduler_storage_is_fixed_across_ten_thousand_ticks(monkeypatch):
    use_linear_ticks(monkeypatch)
    sequencer = Sequencer()
    track = make_track()
    track.engines["EUC"].g1.set_pulses(4)
    sequencer.add_track(track)
    transport = Transport(None)
    outputs = sequencer.outputs
    storage_ids = (
        id(outputs._desired),
        id(outputs._applied),
        id(outputs._pending),
        id(outputs._gate_active),
        id(outputs._gate_low),
        id(outputs._gate_deadline),
    )

    def discard_cv(channel, voltage):
        pass

    now_us = 0
    for _ in range(10_000):
        tick_sequencer(sequencer, transport, now_us)
        sequencer.flush_outputs(discard_cv)
        sequencer.pump(now_us + seq2.GATE_MS * 1000)
        sequencer.flush_outputs(discard_cv)
        now_us += 10_000

    assert storage_ids == (
        id(outputs._desired),
        id(outputs._applied),
        id(outputs._pending),
        id(outputs._gate_active),
        id(outputs._gate_low),
        id(outputs._gate_deadline),
    )
