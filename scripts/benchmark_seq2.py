"""Run Seq2 timing and heap baselines on a connected EuroPi.

Usage:
    mpremote connect /dev/ttyACM0 run scripts/benchmark_seq2.py

The script intentionally does not write state or drive CV outputs. It measures
the hardware-independent sequencer core at 200 MHz, then restores the original
CPU frequency.
"""

import gc
import machine
from utime import ticks_add, ticks_diff, ticks_us

VALIDATION_FREQUENCY = 200_000_000
PPQN_BPM = 240
PPQN_STEP_MULTIPLIER = 4
PPQN_WARMUP_TICKS = 120
TICKS = 10_000
HISTOGRAM_BUCKET_US = 10
HISTOGRAM_BUCKETS = 501
seq2 = None


def _set_frequency(frequency):
    machine.freq(frequency)
    actual_frequency = machine.freq()
    if actual_frequency != frequency:
        raise RuntimeError(
            "Unable to set CPU frequency to %d Hz (actual %d Hz)"
            % (frequency, actual_frequency)
        )


def _discard_cv(channel, voltage):
    pass


def _make_sequencer():
    sequencer = seq2.Sequencer()
    for index in range(3):
        sequencer.add_track(
            seq2.Track(
                index,
                seq2.EuclidPattern(64, 23 + index, index, 100),
                seq2.EuclidPattern(63 - index, 17 + index, index * 2, 100),
                seq2.MERGE_OR,
            )
        )
    return sequencer


def _percentile_99(histogram, count):
    threshold = (count * 99 + 99) // 100
    seen = 0
    index = 0
    while index < len(histogram):
        seen += histogram[index]
        if seen >= threshold:
            return index * HISTOGRAM_BUCKET_US
        index += 1
    return (len(histogram) - 1) * HISTOGRAM_BUCKET_US


def _measure_oled_show():
    total_us = 0
    maximum_us = 0
    count = 20
    index = 0
    while index < count:
        started_us = ticks_us()
        seq2.oled.show()
        elapsed_us = ticks_diff(ticks_us(), started_us)
        total_us += elapsed_us
        if elapsed_us > maximum_us:
            maximum_us = elapsed_us
        index += 1
    full_average_us = total_us // count
    full_maximum_us = maximum_us

    total_us = 0
    maximum_us = 0
    index = 0
    while index < count:
        started_us = ticks_us()
        seq2.oled.show_page(index % seq2.OLED_PAGE_COUNT)
        elapsed_us = ticks_diff(ticks_us(), started_us)
        total_us += elapsed_us
        if elapsed_us > maximum_us:
            maximum_us = elapsed_us
        index += 1
    return full_average_us, full_maximum_us, total_us // count, maximum_us


def _tick(sequencer, transport, now_us, period_us):
    context = transport.tick_context
    context.write(transport.beat_index, now_us, now_us, period_us)
    sequencer.tick(context)
    transport.beat_index += 1


def run_one(frequency):
    _set_frequency(frequency)
    sequencer = _make_sequencer()
    transport = seq2.Transport(None)
    period_us = transport.beat_us()
    histogram = [0] * HISTOGRAM_BUCKETS

    # Warm caches and one-time allocations before taking the heap baseline.
    now_us = 0
    warmup = 0
    while warmup < 100:
        _tick(sequencer, transport, now_us, period_us)
        sequencer.flush_outputs(_discard_cv)
        sequencer.pump(ticks_add(now_us, period_us))
        sequencer.flush_outputs(_discard_cv)
        now_us = ticks_add(now_us, period_us)
        warmup += 1
    gc.collect()
    heap_before = gc.mem_free()

    total_us = 0
    maximum_us = 0
    tick_index = 0
    while tick_index < TICKS:
        started_us = ticks_us()
        _tick(sequencer, transport, now_us, period_us)
        sequencer.flush_outputs(_discard_cv)
        sequencer.pump(ticks_add(now_us, period_us))
        sequencer.flush_outputs(_discard_cv)
        elapsed_us = ticks_diff(ticks_us(), started_us)
        total_us += elapsed_us
        if elapsed_us > maximum_us:
            maximum_us = elapsed_us
        bucket = elapsed_us // HISTOGRAM_BUCKET_US
        if bucket >= HISTOGRAM_BUCKETS:
            bucket = HISTOGRAM_BUCKETS - 1
        histogram[bucket] += 1
        now_us = ticks_add(now_us, period_us)
        tick_index += 1

    gc.collect()
    heap_after = gc.mem_free()
    (
        oled_full_average_us,
        oled_full_maximum_us,
        oled_page_average_us,
        oled_page_maximum_us,
    ) = _measure_oled_show()
    print(
        "SEQ2_BENCH freq=%d ticks=%d avg_us=%d p99_us~=%d max_us=%d "
        "heap_before=%d heap_after=%d heap_delta=%d "
        "oled_full_avg_us=%d oled_full_max_us=%d "
        "oled_page_avg_us=%d oled_page_max_us=%d"
        % (
            frequency,
            TICKS,
            total_us // TICKS,
            _percentile_99(histogram, TICKS),
            maximum_us,
            heap_before,
            heap_after,
            heap_after - heap_before,
            oled_full_average_us,
            oled_full_maximum_us,
            oled_page_average_us,
            oled_page_maximum_us,
        )
    )


def run_ppqn_candidate(frequency, ppqn):
    """Measure the real internal 24 PPQN Transport path."""
    if ppqn != seq2.INTERNAL_PPQN:
        raise ValueError("Seq2 internal PPQN mismatch")
    _set_frequency(frequency)
    sequencer = _make_sequencer()
    transport = seq2.Transport(sequencer.tick)
    transport.bpm = PPQN_BPM
    transport.mul = PPQN_STEP_MULTIPLIER
    base_period_us = transport.base_tick_us()
    transport.running = True
    transport.next_clock_us = base_period_us
    histogram = [0] * HISTOGRAM_BUCKETS
    now_us = base_period_us

    warmup = 0
    while warmup < PPQN_WARMUP_TICKS:
        transport.update(now_us)
        sequencer.update(now_us, transport.tick_context)
        sequencer.pump(now_us)
        sequencer.flush_outputs(_discard_cv)
        now_us = ticks_add(now_us, base_period_us)
        warmup += 1
    gc.collect()
    heap_before = gc.mem_free()
    music_steps_before = transport.beat_index

    total_us = 0
    maximum_us = 0
    tick_index = 0
    while tick_index < TICKS:
        started_us = ticks_us()
        transport.update(now_us)
        sequencer.update(now_us, transport.tick_context)
        sequencer.pump(now_us)
        sequencer.flush_outputs(_discard_cv)
        elapsed_us = ticks_diff(ticks_us(), started_us)
        total_us += elapsed_us
        if elapsed_us > maximum_us:
            maximum_us = elapsed_us
        bucket = elapsed_us // HISTOGRAM_BUCKET_US
        if bucket >= HISTOGRAM_BUCKETS:
            bucket = HISTOGRAM_BUCKETS - 1
        histogram[bucket] += 1
        now_us = ticks_add(now_us, base_period_us)
        tick_index += 1

    gc.collect()
    heap_after = gc.mem_free()
    print(
        "SEQ2_PPQN freq=%d ppqn=%d bpm=%d mul=%d base_period_us=%d ticks=%d "
        "music_steps=%d avg_us=%d p99_us~=%d max_us=%d heap_delta=%d "
        "dropped_base_ticks=%d dropped_music_steps=%d"
        % (
            frequency,
            ppqn,
            PPQN_BPM,
            PPQN_STEP_MULTIPLIER,
            base_period_us,
            TICKS,
            transport.beat_index - music_steps_before,
            total_us // TICKS,
            _percentile_99(histogram, TICKS),
            maximum_us,
            heap_after - heap_before,
            transport.dropped_ticks,
            transport.dropped_music_steps,
        )
    )


def _measure_import():
    global seq2
    gc.collect()
    heap_before = gc.mem_free()
    started_us = ticks_us()
    import contrib.seq2 as seq2_module
    # EuroPi hardware initialization applies the configured board frequency.
    # Reassert the benchmark frequency before recording or running any workload.
    _set_frequency(VALIDATION_FREQUENCY)
    elapsed_us = ticks_diff(ticks_us(), started_us)
    seq2 = seq2_module
    gc.collect()
    heap_after = gc.mem_free()
    print(
        "SEQ2_IMPORT freq=%d import_us=%d heap_before=%d heap_after=%d heap_used=%d"
        % (
            machine.freq(),
            elapsed_us,
            heap_before,
            heap_after,
            heap_before - heap_after,
        )
    )


def _measure_startup():
    gc.collect()
    heap_before = gc.mem_free()
    started_us = ticks_us()
    application = seq2.Seq2()
    elapsed_us = ticks_diff(ticks_us(), started_us)
    gc.collect()
    heap_after = gc.mem_free()
    print(
        "SEQ2_STARTUP freq=%d startup_us=%d heap_before=%d heap_after=%d heap_used=%d"
        % (
            machine.freq(),
            elapsed_us,
            heap_before,
            heap_after,
            heap_before - heap_after,
        )
    )
    return application


def main():
    original_frequency = machine.freq()
    try:
        _set_frequency(VALIDATION_FREQUENCY)
        _measure_import()
        run_one(VALIDATION_FREQUENCY)
        run_ppqn_candidate(VALIDATION_FREQUENCY, seq2.INTERNAL_PPQN)
        _measure_startup()
    finally:
        machine.freq(original_frequency)


if __name__ == "__main__":
    main()
