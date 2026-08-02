# Seq2 真机基准记录

运行命令：

```bash
mpremote connect /dev/ttyACM0 run scripts/benchmark_seq2.py
```

脚本不会保存状态或写 CV，但会暂时切换到 200 MHz 运行全部测量，结束后恢复原 CPU 频率。

## 环境

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-08-02 |
| EuroPi 硬件版本 | 未记录（需目视 PCB） |
| Pico / Pico 2 | Raspberry Pi Pico 2 / RP2350 |
| MicroPython / 固件版本 | MicroPython v1.28.0（2026-04-06） |
| Git revision / worktree | `e594765` / 含本轮未提交修改 |

## 结果

| 实现检查点 | CPU | BPM×MUL | 时钟分辨率 | 轨道组合 | OLED | ticks | avg µs | p99 µs（约） | max µs | heap delta | OLED full avg/max µs | OLED page avg/max µs |
| --- | ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 阶段 0 / 旧 OutputBus | 200 MHz | 120×4 | 旧 step clock | EUC × 3 | 单独测量 | 10,000 | 未采集 | 未采集 | 未采集 | 未采集 | 重构前未连接真机 | 未采集 |
| 阶段 1B+ / OutputScheduler | 200 MHz | 120×4 | 24 PPQN / 相位 MUL | EUC × 3 | 单独测量 | 10,000 | 2,300 | 约 2,470 | 2,493 | 0 B | 13,295 / 13,377 | 3,967 / 3,979，max ≤ 6,000：通过 |

### 24 PPQN 内部时钟负载（240 BPM）

| CPU | PPQN | base period µs | MUL | base ticks | music steps | avg µs | p99 µs（约） | max µs | heap delta | dropped base ticks | dropped music steps | 是否满足 1 ms p99 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 200 MHz | 24 | 10,416 | 4 | 10,000 | 1,666 | 815 | 约 2,180 | 2,212 | 0 B | 0 | 0 | 合成执行耗时不适用；真实 deadline p99 通过 |

这里的 `SEQ2_PPQN p99` 是在合成 deadline 上测得的**热路径执行时间**，不是实际 deadline 的调度迟到量。每 6 个基础 tick 有 1 个 MUL4 音乐步，因此 p99 会落入包含三轨推进的重 tick。真实时间压力脚本直接在每个基础 tick 的 `actual_us - scheduled_us` 上建直方图；30 分钟结果 p99 约 850 µs，满足不高于 1 ms 的指标。

## Import / 冷启动与单文件决策

| 形态 | CPU | import µs | import heap used | Seq2 startup µs | startup heap used | 结论 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 单文件 `seq2.py` | 200 MHz | 1,933,017 | 88,848 B | 79,077 | 18,096 B | 当前部署基线；保留单文件 |
| 多模块候选 | 200 MHz | 待测 | 待测 | 待测 | 待测 | 仅当真机余量明确后实现并比较 |

## 真机行为检查

- [x] 20 / 120 / 240 BPM
- [x] 内部时钟；外部时钟按本轮决定不验收
- [x] EUC × 3、CV × 3、混合三轨
- [x] `GLEN=10/50/90%`
- [x] 自动页面切换、rotate 编辑和 OLED 刷新期间无 dropped tick；物理旋钮手测待补
- [x] 240 BPM / 24 PPQN 预算下分页持续完成，无 display starvation
- [ ] 外部时钟 burst、拔插和恢复（本轮明确忽略）
- [x] 30 分钟压力运行

## 2026-08-02 最终基准原始输出

```text
SEQ2_IMPORT freq=200000000 import_us=1933017 heap_before=487968 heap_after=399120 heap_used=88848
SEQ2_BENCH freq=200000000 ticks=10000 avg_us=2300 p99_us~=2470 max_us=2493 heap_before=382736 heap_after=382736 heap_delta=0 oled_full_avg_us=13295 oled_full_max_us=13377 oled_page_avg_us=3967 oled_page_max_us=3979
SEQ2_PPQN freq=200000000 ppqn=24 bpm=240 mul=4 base_period_us=10416 ticks=10000 music_steps=1666 avg_us=815 p99_us~=2180 max_us=2212 heap_delta=0 dropped_base_ticks=0 dropped_music_steps=0
SEQ2_STARTUP freq=200000000 startup_us=79077 heap_before=399120 heap_after=381024 heap_used=18096
```

脚本结束后再次读取 `machine.freq()`，结果仍为 `200000000`。

## 30 分钟真实时间压力结果

测试固定为 200 MHz、240 BPM、24 PPQN、MUL4。六个 5 分钟阶段依次覆盖 EUC×3、CV×3/GLEN 10%、CV×3/50%、CV×3/90%、EUC/CV/EUC 和 CV/EUC/CV；每秒切换页面并修改三轨 rotate，不保存状态。

```text
SEQ2_STRESS_RESULT freq=200000000 duration_s=1800 loops=1973780 base_ticks=172812 music_steps=28802 loop_p99_us~=6600 loop_max_us=27755 clock_avg_lateness_us=329 clock_p99_lateness_us~=850 clock_max_lateness_us=17945 overruns=0 dropped_base_ticks=0 dropped_music_steps=0 late_gate_edges=90600 max_gate_lateness_us=11855 heap_delta=-80 phase_change_max_us=12204 ui_change_max_us=704 render_p99_us~=8550 render_max_us=25746 display_p99_us~=5250 display_max_us=6148 page_attempts=670210 pages=88968 frames=22242 max_page_gap_us=979846 max_page_skips=33
```

- 172,812 个真实基础 tick 后无 overrun、无 dropped base/music tick，clock p99 约 850 µs。
- `heap_delta=-80 B` 没有随 tick 数线性增长。
- 22,242 个完整 OLED frame 持续完成；接近 1 秒的 `max_page_gap` 来自静态 P0 不随 beat 重画，不是 pending frame starvation。
- 该长测版本在阶段边界把三轨连续改完，`phase_change_max_us=12,204` 与 `max_gate_lateness_us=11,855` 同时出现。随后把 harness 改成真实 UI 风格的逐轨修改、轨间隔 1 秒，60 秒全场景复核如下：

```text
SEQ2_STRESS_RESULT freq=200000000 duration_s=60 loops=71548 base_ticks=5762 music_steps=960 loop_p99_us~=5600 loop_max_us=8969 clock_avg_lateness_us=320 clock_p99_lateness_us~=900 clock_max_lateness_us=10565 overruns=0 dropped_base_ticks=0 dropped_music_steps=0 late_gate_edges=3047 max_gate_lateness_us=878 heap_delta=-64 phase_change_max_us=1691 ui_change_max_us=1213 render_p99_us~=6250 render_max_us=7129 display_p99_us~=5100 display_max_us=5433 page_attempts=22097 pages=2990 frames=747 max_page_gap_us=978850 max_page_skips=33
```

逐轨切换下 gate 最大迟到 878 µs、clock p99 约 900 µs，且仍为零 dropped tick。`late_gate_edges` 统计任何大于 0 µs 的轮询迟到，因此必须与最大迟到量一起解读。

低速补测同样使用 200 MHz、24 PPQN、MUL4、EUC×3：20 BPM 运行 10 秒得到 80 base ticks、clock p99 约 450 µs；120 BPM 运行 10 秒得到 480 base ticks、clock p99 约 700 µs。两者的 overrun、dropped base tick 和 dropped music step 均为 0。

每次保留原始的 `SEQ2_IMPORT ...`、一行 `SEQ2_BENCH ...`、一行 `SEQ2_PPQN ...` 和 `SEQ2_STARTUP ...` 输出，并注明任何 dropped tick、异常 gate 或 UI 卡顿。所有输出的 `freq` 都应为 `200000000`；`oled_page_max_us` 必须不高于 `6000`。
