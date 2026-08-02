# Seq2 — 3 轨多功能音序器

Seq2 使用 EuroPi 的 6 路 CV，提供三条固定配对轨道。实现以稳定时钟、固定内存和可扩展轨道类型为优先目标。

## 1. 输出与轨道类型

固定配对不会随轨道类型改变：

| 轨道 | 主输出 | gate / clock 输出 |
| --- | --- | --- |
| Track 1 | CV1 | CV4 |
| Track 2 | CV2 | CV5 |
| Track 3 | CV3 | CV6 |

每轨支持：

- `OFF`：两路输出归零，并取消未完成的 gate；
- `EUC`：主输出为两个 Euclidean pattern 合并后的 gate，配对输出为每步 clock；
- `CV`：主输出为保持型 CV，配对输出为 gate；步值为 0 时不触发 gate。

切换类型会重置新类型的播放头，但两套引擎的音乐参数都常驻，切回后仍然保留。

## 2. 操作

| 控件 | 行为 |
| --- | --- |
| K1 | 选择当前页参数；CV 页参数之后是有效步槽 |
| K2 | 编辑当前参数；普通参数使用 pickup，CV 步值使用 jump |
| B1 短按 | 上一页 |
| B2 短按 | 下一页 |
| B1 长按 | 仅在内部时钟模式下启动 / 停止 |
| B2 长按 | 请求保存项目 |
| DIN | `CLK=EXT` 时输入外部步时钟 |

页面为 P0 全局页和 P1–P3 轨道页。保存请求先显示 `SAVING`，实际写入在距离下一音乐步至少 20 ms 时执行；空的 24 PPQN 基础 tick 不会让保存永久饥饿，返回后由 ClockService 有界 catch-up。成功显示 `SAVED`，失败显示 `SAVE ERR`。

### 2.1 全局参数

| 参数 | 范围 | 说明 |
| --- | --- | --- |
| CLK | INT / EXT | 内部或外部时钟 |
| BPM | 20–240 | 内部时钟 BPM |
| MUL | 1–8 | 每拍步进倍率，保留旧存档语义 |
| T1–T3 | OFF / EUC / CV | 三条轨道类型 |

### 2.2 EUC 参数

每轨有两组 pattern：`ROT1/2`、`PLS1/2`、`STP1/2`、`PRB1/2`，以及合并模式 `MERG`。

合并模式为 `OR`、`AND`、`XOR`、`G1`、`G2`。`steps` 范围 1–64，`pulses` 与 `rotate` 范围 0–steps，概率范围 0–100。

### 2.3 CV 参数

| 参数 | 范围 | 说明 |
| --- | --- | --- |
| LEN | 1–16 | 播放长度；缩短不会删除隐藏步值 |
| VLO | 0–10 V | 轨道低电压 |
| VHI | 0–10 V | 轨道高电压，始终满足 `VLO <= VHI` |
| GLEN | 10–90% | gate 占当前步周期的比例 |
| S01–S16 | 0–127 | 有效长度内的步值 |

CV 电压为 `VLO + (VHI - VLO) * value / 127`。`GLEN` 限制为 10–90%：最短脉宽避开 OLED/GC 的主要抖动窗口，最长脉宽在连续有效步之间保留明确 LOW 间隙。旧状态中的 5%/100% 会在加载时夹到 10%/90%。

## 3. 当前架构

```text
Hardware / DIN ISR
  └─ ClockCapture（16 项固定时间戳环形缓冲）
       ↓
InputManager → Controller / Pages / EditorService / UiState
                    ├─ ClockService → TransportRuntime → TickContext
                    ├─ Sequencer → Track → TrackFeature
                    │                         ├─ NullTrackFeature
                    │                         ├─ EuclidTrackFeature
                    │                         └─ CVTrackFeature
                    ├─ OutputScheduler → Hardware.set_cv
                    ├─ ProjectState → PersistenceService
                    └─ ViewSnapshot → Renderer → OLED
```

职责边界：

- `Hardware` 是唯一访问 EuroPi API 的层；
- `ClockCapture` 在 ISR 与主循环之间保存有界时间戳，满队列丢弃新事件并增加 overflow 计数；
- `ClockService` 只负责绝对 deadline、有界 catch-up 和内部时序指标；
- `TransportRuntime` 负责 source、tempo、running、音乐步索引，并复用同一个 `TickContext`；
- `Sequencer` 不按类型分支，只调用当前 `TrackFeature`；
- `OutputScheduler` 为每通道保存唯一电压状态和唯一 gate deadline；
- `Renderer` 不读取 Track 或 Engine，只读取一次性写好的 `ViewSnapshot`；
- `EditorService` 直接应用静态 descriptor，并统一处理类型切换、停止和输出取消副作用；
- `ProjectState` 只包含项目配置，播放头、pending gate 和运行状态不会保存。

## 4. Euclidean pattern

Seq2 不再依赖共享的 `experimental.euclid.generate_euclidean_pattern()`。内置生成器具有以下约束：

- 每个 pattern 在构造时分配两个 `bytearray(MAX_STEPS)`；
- 生成使用模块级固定 workspace，无递归、append 或切片；
- 新 pattern 先写入 inactive buffer，写完 metadata 后切换 active index；
- 只有规范化后的 `(steps, pulses)` 改变时才重新生成；
- rotate 在 `is_on()` 的读取索引上应用，不改写 canonical buffer；
- `rotate == steps` 与 rotate 0 等效，同时保持旧存档显示值兼容。

主机测试逐项对照旧生成器的完整空间：steps 1–64、pulses 0–steps、rotate 0–steps。

## 5. 输出调度语义

`OutputScheduler` 在初始化时为 6 路通道预分配状态数组。引擎调用：

```python
outputs.set_cv(channel, voltage)
outputs.trigger_gate(channel, high, low, length_us, now_us)
outputs.cancel_channel(channel, voltage)
```

规则：

- retrigger 覆盖同一通道的旧 deadline，因此旧 LOW 不会截短新 gate；
- 相同电压不会重复写硬件；
- OFF、类型切换、停止和 reset 会取消相关 deadline；
- 到期晚于 deadline 时增加 `late_edges`；
- 当前每通道只需一个未来 LOW；未来 ratchet 若需要多个边沿，应增加固定容量 edge ring，而不是动态列表。

## 6. 时钟语义

内部时钟固定为 24 PPQN，基础周期为 `60_000_000 // (BPM × 24)`。基础 deadline 从上一个 deadline 累加，不从实际执行时间重建。一次主循环最多 catch up 4 个基础 tick；更大的延迟会推进到下一个未来 deadline，并记录 `overruns` 和 `dropped_ticks`，其中 dropped 指丢失的基础 tick。被丢弃的基础 tick 仍推进整数相位但不产生输出，跨过的音乐步计入 `dropped_music_steps`，因此恢复后不会永久偏离 PPQN 网格。

Transport 使用固定整数相位累加器把 `MUL=1..8` 映射为音乐步：每个基础 tick 执行 `phase += MUL`，达到 24 时减去 24 并推进一音乐步。因此一个四分音符始终推进 MUL 步；MUL=1/2/3/4/6/8 有固定间隔，MUL=5/7 在 24 tick 网格上均匀交替。传给 gate 的 `period_us` 是到下一实际网格步的时间，而不是平均值，使 10–90% 的 `GLEN` 对非均匀 MUL 网格仍按下一实际步长计算。

`start()` 从第 0 步重新开始，`continue_()` 保留步索引，`reset()` 清零索引。运行中修改 BPM/MUL 会从当前时刻重排下一步，但不会重置播放位置，也不会更改已经开始的 gate 截止时间。

DIN ISR 捕获 `ticks_us()`，主循环按原顺序交给外部 Transport；可查询外部 tick 数、最近间隔、jitter、最大 jitter 和 timeout 次数。超时后的第一拍作为新的测量起点，不把断线时长当成正常周期。外部时钟继续保持“每个 DIN 边沿推进一步”的既有语义，不套用内部 24 PPQN 相位累加器。

### 6.1 OLED 分页提交

Renderer 先把完整画面写入 128×32 framebuffer，但不立即执行全屏 `show()`。Controller 随后按 SSD1306 的四个 8px page 分四次提交，每轮主循环最多传一页；Display 在初始化时为四页建立固定 `memoryview`，提交时不切片或重新分配 buffer。

单页 guard 经 200 MHz 真机测量从暂定 5 ms 调整为 6 ms：单页最坏传输 3,979 µs，额外保留约 2 ms 给主循环调度。渲染和每页发送前都会重新读取 `ticks_us()` 并处理已经到期的 gate。beat 更新只重画 EUC/CV 底部的 playhead 带；页面、参数或 notice 改变才做全量 framebuffer 重绘。全量重绘实测最坏约 25 ms，因此仅在已有 gate 没有 25 ms 内 deadline 时启动；动态重绘与单页传输继续使用 6 ms 的内部 clock/gate guard。发送返回后立即再次处理 clock 和 gate deadline。240 BPM、24 PPQN 的最短基础周期是 10,416 µs。外部时钟暂不参与 OLED clock-slack 预测，但已有 gate deadline 仍会阻止渲染和页面传输。

## 7. TrackFeature 扩展

一个 feature 注册 type id、config/runtime factory、codec、参数槽、离散 `on_tick()`、连续 `update()`、snapshot writer 和 renderer。Track 分别持有 `engines`（项目配置）与 `runtimes`（播放头和瞬时状态）；通用 Sequencer、Controller 和 Renderer 不含 OFF/EUC/CV 分支。当前三个内置 feature 的连续 update 均为空操作，为后续 slide/LFO 保留接口。

新增轨道类型的最小流程：

1. 实现 `TrackFeature`；
2. 若需要音乐配置/运行态，创建常驻 engine；
3. 提供静态参数 descriptors；
4. 实现 `on_tick()`；需要连续行为时实现 `update()`；再实现 `write_snapshot()` 和 `render()`；
5. 调用 `register_track_feature()`；
6. 若要出现在用户菜单与存档白名单，再显式加入 `TYPE_NAMES` 并补 codec 测试。

参数表在 engine 或 Sequencer 初始化后只创建一次。普通 K2 编辑直接调用 descriptor setter，不创建 Command 对象。

## 8. 项目状态

新保存格式使用 `schema_version: 2`：

```json
{
  "schema_version": 2,
  "clock": {"source": "INT", "bpm": 120, "mul": 4},
  "tracks": []
}
```

loader 可读取没有版本字段的 v1 状态并迁移。所有数值字段按实现范围校验；未知 schema 被拒绝；某一轨损坏时保留该轨默认值，不影响其他轨。v2 不保存 Transport running、beat index、EUC player positions、CV position 或 OutputScheduler deadline。

## 9. 性能与验证

主机回归入口：

```bash
pytest -q software/tests/contrib/test_seq2.py
pytest -q
```

真机基准：

```bash
mpremote connect /dev/ttyACM0 run scripts/benchmark_seq2.py
```

脚本先将 Pico 2 切换到 200 MHz，再测量 import、冷启动、10,000 tick 的 avg/p99/max、heap delta，以及 OLED 全屏和单页传输时间；结束后恢复原 CPU 频率。单页最大值必须不高于 6 ms guard。结果填写到 `seq2_benchmark_results.md`。本轮真机验收不采集 150 MHz 数据。

物理文件继续保持单一 `seq2.py`。200 MHz 真机测得 import 使用 88,848 B、Seq2 构造额外使用 18,096 B，启动后约余 381 KB heap；当前没有模块拆分带来实时性或维护收益的证据，因此不增加多模块 import 风险。重构方案与验收记录见 `seq2_refactor_plan.md` 和 `seq2_benchmark_results.md`。
