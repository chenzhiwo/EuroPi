"""Real-time Seq2 stress test for a Pico 2 running at 200 MHz.

Run a short smoke test from the repository without installing this file:

    mpremote connect /dev/ttyACM0 mount . exec \
        'import sys; sys.path.append("/remote/scripts"); import stress_seq2; stress_seq2.main(60)'

The default duration is 30 minutes. The test drives the real OLED and CV/gate
outputs between 0 V and 5 V, but never saves Seq2 state. All CVs are turned off
in ``finally``.
"""

import gc
import time

import machine
from time import ticks_add, ticks_diff, ticks_ms, ticks_us


VALIDATION_FREQUENCY = 200_000_000
DEFAULT_DURATION_SECONDS = 30 * 60
REPORT_INTERVAL_MS = 60_000
PAGE_SWITCH_INTERVAL_MS = 1_000
HISTOGRAM_BUCKET_US = 50
HISTOGRAM_BUCKETS = 401


def _percentile_99(histogram, samples):
    target = (samples * 99 + 99) // 100
    seen = 0
    index = 0
    while index < len(histogram):
        seen += histogram[index]
        if seen >= target:
            return index * HISTOGRAM_BUCKET_US
        index += 1
    return (len(histogram) - 1) * HISTOGRAM_BUCKET_US


def _phase_settings(phase):
    if phase == 0:
        types = ("EUC", "EUC", "EUC")
        gate_len = 50
    elif phase == 1:
        types = ("CV", "CV", "CV")
        gate_len = 10
    elif phase == 2:
        types = ("CV", "CV", "CV")
        gate_len = 50
    elif phase == 3:
        types = ("CV", "CV", "CV")
        gate_len = 90
    elif phase == 4:
        types = ("EUC", "CV", "EUC")
        gate_len = 50
    else:
        types = ("CV", "EUC", "CV")
        gate_len = 90
    return types, gate_len


def _configure_track(app, phase, index):
    """Apply one UI-sized track edit and service deadlines around its flush."""
    types, gate_len = _phase_settings(phase)
    track = app.seq.tracks[index]
    now_us = ticks_us()
    app.transport.update(now_us)
    if app.seq.pump(now_us):
        app.controller._flush_outputs()
    if track.type != types[index]:
        track.set_type(types[index])
        app.seq.sync_track_types()
        app.seq.flush_outputs(app.hw.set_cv)
    track.engines["CVSEQ"].set_gate_len(gate_len)
    app.app.dirty = True
    app.app.redraw_all = True


def _configure_phase(app, phase):
    """Configure the initial phase before playback has active gates."""
    types, gate_len = _phase_settings(phase)

    index = 0
    while index < len(app.seq.tracks):
        _configure_track(app, phase, index)
        index += 1
    return "%s/%s/%s:%d" % (
        types[0], types[1], types[2], gate_len
    )


def main(duration_seconds=DEFAULT_DURATION_SECONDS, fixed_phase=None, bpm=240):
    duration_seconds = max(1, int(duration_seconds))
    bpm = min(max(int(bpm), 20), 240)
    if fixed_phase is not None:
        fixed_phase = min(max(int(fixed_phase), 0), 5)
    original_frequency = machine.freq()
    app = None
    try:
        machine.freq(VALIDATION_FREQUENCY)
        from contrib.seq2 import Seq2

        # EuroPi configuration is imported with Seq2, so enforce the requested
        # validation frequency once more after import and construction.
        machine.freq(VALIDATION_FREQUENCY)
        app = Seq2()
        machine.freq(VALIDATION_FREQUENCY)

        app.transport.stop()
        app.transport.source = "INT"
        app.transport.bpm = bpm
        app.transport.mul = 4

        clock_histogram = [0] * HISTOGRAM_BUCKETS
        clock_samples = 0
        clock_total_lateness_us = 0
        original_clock_tick = app.transport._clock_tick

        def observed_clock_tick(scheduled_us, actual_us, period_us):
            nonlocal clock_samples, clock_total_lateness_us
            lateness_us = ticks_diff(actual_us, scheduled_us)
            if lateness_us < 0:
                lateness_us = 0
            clock_total_lateness_us += lateness_us
            bucket = lateness_us // HISTOGRAM_BUCKET_US
            if bucket >= HISTOGRAM_BUCKETS:
                bucket = HISTOGRAM_BUCKETS - 1
            clock_histogram[bucket] += 1
            clock_samples += 1
            original_clock_tick(scheduled_us, actual_us, period_us)

        app.transport._clock_tick = observed_clock_tick
        app.transport.start()

        phase_count = 1 if fixed_phase is not None else 6
        phase_ms = max(1_000, duration_seconds * 1_000 // phase_count)
        phase = fixed_phase if fixed_phase is not None else 0
        phase_label = _configure_phase(app, phase)

        loop_histogram = [0] * HISTOGRAM_BUCKETS
        render_histogram = [0] * HISTOGRAM_BUCKETS
        display_histogram = [0] * HISTOGRAM_BUCKETS
        render_samples = 0
        display_samples = 0
        loop_count = 0
        page_attempts = 0
        page_count = 0
        frame_count = 0
        maximum_loop_us = 0
        maximum_phase_change_us = 0
        maximum_ui_change_us = 0
        maximum_render_us = 0
        maximum_display_us = 0
        maximum_page_gap_us = 0
        consecutive_page_skips = 0
        maximum_page_skips = 0
        last_page_us = ticks_us()

        gc.collect()
        heap_before = gc.mem_free()
        started_ms = ticks_ms()
        finish_ms = ticks_add(started_ms, duration_seconds * 1_000)
        next_report_ms = ticks_add(started_ms, REPORT_INTERVAL_MS)
        next_page_switch_ms = ticks_add(started_ms, PAGE_SWITCH_INTERVAL_MS)
        next_phase_ms = ticks_add(started_ms, phase_ms)
        phase_track_index = -1
        next_phase_track_ms = started_ms

        print(
            "SEQ2_STRESS_START freq=%d duration_s=%d bpm=%d ppqn=24 mul=4 "
            "mode=%s" % (machine.freq(), duration_seconds, bpm, phase_label)
        )

        while ticks_diff(finish_ms, ticks_ms()) > 0:
            loop_started_us = ticks_us()
            now_us = loop_started_us
            now_ms = ticks_ms()

            if (
                fixed_phase is None
                and ticks_diff(now_ms, next_phase_ms) >= 0
                and phase < phase_count - 1
            ):
                phase += 1
                phase_track_index = 0
                next_phase_track_ms = now_ms
                next_phase_ms = ticks_add(next_phase_ms, phase_ms)

            if (
                phase_track_index >= 0
                and ticks_diff(now_ms, next_phase_track_ms) >= 0
            ):
                phase_started_us = ticks_us()
                _configure_track(app, phase, phase_track_index)
                phase_elapsed_us = ticks_diff(ticks_us(), phase_started_us)
                if phase_elapsed_us > maximum_phase_change_us:
                    maximum_phase_change_us = phase_elapsed_us
                phase_track_index += 1
                if phase_track_index == len(app.seq.tracks):
                    phase_track_index = -1
                else:
                    next_phase_track_ms = ticks_add(
                        next_phase_track_ms, PAGE_SWITCH_INTERVAL_MS
                    )

            if ticks_diff(now_ms, next_page_switch_ms) >= 0:
                ui_started_us = ticks_us()
                app.app.next_page()
                # Rotate edits exercise the allocation-free outer rotation path.
                track_index = 0
                while track_index < len(app.seq.tracks):
                    pattern = app.seq.tracks[track_index].engines["EUC"].g1
                    pattern.set_rot((pattern.rot + 1) % (pattern.steps + 1))
                    track_index += 1
                next_page_switch_ms = ticks_add(
                    next_page_switch_ms, PAGE_SWITCH_INTERVAL_MS
                )
                ui_elapsed_us = ticks_diff(ticks_us(), ui_started_us)
                if ui_elapsed_us > maximum_ui_change_us:
                    maximum_ui_change_us = ui_elapsed_us

            app.app.expire_notice(now_ms)
            app.transport.update(now_us)
            app.seq.update(now_us, app.transport.tick_context)
            if app.seq.pump(now_us):
                app.controller._flush_outputs()

            if app.controller._display_page < 0 and app.app.dirty:
                render_started_us = ticks_us()
                rendered = app.controller._prepare_display()
                render_elapsed_us = ticks_diff(ticks_us(), render_started_us)
                if rendered:
                    if render_elapsed_us > maximum_render_us:
                        maximum_render_us = render_elapsed_us
                    bucket = render_elapsed_us // HISTOGRAM_BUCKET_US
                    if bucket >= HISTOGRAM_BUCKETS:
                        bucket = HISTOGRAM_BUCKETS - 1
                    render_histogram[bucket] += 1
                    render_samples += 1

            pending_page = app.controller._display_page >= 0
            if pending_page:
                page_attempts += 1
            display_started_us = ticks_us()
            displayed = app.controller._flush_display_page()
            display_elapsed_us = ticks_diff(ticks_us(), display_started_us)
            if displayed:
                if display_elapsed_us > maximum_display_us:
                    maximum_display_us = display_elapsed_us
                bucket = display_elapsed_us // HISTOGRAM_BUCKET_US
                if bucket >= HISTOGRAM_BUCKETS:
                    bucket = HISTOGRAM_BUCKETS - 1
                display_histogram[bucket] += 1
                display_samples += 1
                completed_us = ticks_us()
                page_gap_us = ticks_diff(completed_us, last_page_us)
                if page_gap_us > maximum_page_gap_us:
                    maximum_page_gap_us = page_gap_us
                last_page_us = completed_us
                page_count += 1
                consecutive_page_skips = 0
                if app.controller._display_page < 0:
                    frame_count += 1
            elif pending_page:
                consecutive_page_skips += 1
                if consecutive_page_skips > maximum_page_skips:
                    maximum_page_skips = consecutive_page_skips

            elapsed_us = ticks_diff(ticks_us(), loop_started_us)
            if elapsed_us > maximum_loop_us:
                maximum_loop_us = elapsed_us
            bucket = elapsed_us // HISTOGRAM_BUCKET_US
            if bucket >= HISTOGRAM_BUCKETS:
                bucket = HISTOGRAM_BUCKETS - 1
            loop_histogram[bucket] += 1
            loop_count += 1

            if ticks_diff(now_ms, next_report_ms) >= 0:
                print(
                    "SEQ2_STRESS_PROGRESS elapsed_s=%d base_ticks=%d "
                    "music_steps=%d dropped=%d late_edges=%d pages=%d frames=%d"
                    % (
                        ticks_diff(now_ms, started_ms) // 1_000,
                        app.transport.base_tick_index,
                        app.transport.beat_index,
                        app.transport.dropped_ticks,
                        app.seq.outputs.late_edges,
                        page_count,
                        frame_count,
                    )
                )
                next_report_ms = ticks_add(next_report_ms, REPORT_INTERVAL_MS)

            time.sleep_ms(0)

        gc.collect()
        heap_after = gc.mem_free()
        print(
            "SEQ2_STRESS_RESULT freq=%d duration_s=%d loops=%d base_ticks=%d "
            "music_steps=%d loop_p99_us~=%d loop_max_us=%d "
            "clock_avg_lateness_us=%d clock_p99_lateness_us~=%d "
            "clock_max_lateness_us=%d overruns=%d dropped_base_ticks=%d "
            "dropped_music_steps=%d late_gate_edges=%d "
            "max_gate_lateness_us=%d heap_delta=%d "
            "phase_change_max_us=%d ui_change_max_us=%d "
            "render_p99_us~=%d render_max_us=%d "
            "display_p99_us~=%d display_max_us=%d "
            "page_attempts=%d pages=%d frames=%d max_page_gap_us=%d "
            "max_page_skips=%d"
            % (
                machine.freq(),
                duration_seconds,
                loop_count,
                app.transport.base_tick_index,
                app.transport.beat_index,
                _percentile_99(loop_histogram, loop_count),
                maximum_loop_us,
                clock_total_lateness_us // max(1, clock_samples),
                _percentile_99(clock_histogram, clock_samples),
                app.transport.max_lateness_us,
                app.transport.overruns,
                app.transport.dropped_ticks,
                app.transport.dropped_music_steps,
                app.seq.outputs.late_edges,
                app.seq.outputs.max_late_edge_us,
                heap_after - heap_before,
                maximum_phase_change_us,
                maximum_ui_change_us,
                _percentile_99(render_histogram, render_samples),
                maximum_render_us,
                _percentile_99(display_histogram, display_samples),
                maximum_display_us,
                page_attempts,
                page_count,
                frame_count,
                maximum_page_gap_us,
                maximum_page_skips,
            )
        )
    finally:
        if app is not None:
            try:
                app.transport.stop()
                app.seq.cancel_all()
                app.controller._flush_outputs()
                app.hw.off_all_cvs()
            except Exception as error:
                print("SEQ2_STRESS_CLEANUP_ERROR", error)
        machine.freq(original_frequency)


if __name__ == "__main__":
    main()
