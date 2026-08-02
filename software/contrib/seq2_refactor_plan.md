# Seq2 架构重构方案

> 状态：主机侧阶段 0–5 与 200 MHz 内部时钟自动真机验收已完成；外部时钟按本轮决定不验收，物理旋钮/波形手测待补<br>
> 适用代码：`software/contrib/seq2.py`<br>
> 行为基线：`software/contrib/seq2.md`<br>
> 核心目标：降低实时路径抖动和堆分配，明确状态边界，使新增轨道类型不再修改时钟、调度和通用 UI 核心。

## 1. 结论

Seq2 当前已经具备 `Hardware → InputManager → Controller → Sequencer / Transport → Renderer`
的分层骨架，不需要推倒重写。重构应集中在以下三条边界：

1. 把 `Transport → Sequencer → OutputBus` 改造成可测量、少分配、不会被旧延时事件干扰的实时路径。
2. 把需要持久化的音乐数据与播放头、门截止时间、页面状态等运行态彻底分开。
3. 用完整的 `TrackFeature` 注册项统一轨道配置、运行引擎、编辑参数、渲染和编解码，消除核心层中的轨道类型分支。

采用分阶段迁移。前四个阶段先保留单文件，待真实 Pico 2 上完成 RAM 和导入成本测量后，才决定是否拆成多个模块。重构期间不改变现有 CV 配对、按钮操作、存档入口和 EUC/CV 的音乐行为。

## 2. 范围

### 2.1 本次重构包含

- 内部时钟和外部时钟事件捕获；
- start、stop、continue、reset 的明确语义；
- 每轨时钟分频所需的基础时钟能力；
- CV、gate、延时关门和未来 ratchet 所需的输出调度；
- EUC、CV、OFF 三种轨道的统一扩展接口；
- 参数编辑描述、渲染快照和 UI 分发；
- 状态 schema、校验、迁移和保存策略；
- 主机单元测试、确定性虚拟时钟和真机性能测量。

### 2.2 暂不包含

- song mode、pattern chain、MIDI、录制、undo/redo；
- 在重构同时改变现有音序或电压映射行为；
- 照搬 PER|FORMER 的 FreeRTOS、多任务、C++ 虚函数或 192 PPQN；
- 依靠提高 Pico 2 CPU 频率掩盖调度和 GC 问题；
- 未经真机数据验证就拆分 MicroPython 模块。

### 2.3 必须保持的用户行为

- 三轨固定配对保持不变：T1=`cv1+cv4`、T2=`cv2+cv5`、T3=`cv3+cv6`；
- B1 短按上一页，长按切换内部时钟启动/停止；
- B2 短按下一页，长按请求保存，成功后顶部左侧显示 `SAVED` 一秒；
- EUC 和 CV 引擎切换后再切回时，原参数和序列仍保留；
- CV 步值为 0 时不触发 gate，主 CV 仍输出对应电压；
- `VLO <= VHI`，轨道类型和当前项目数据可以从旧存档迁移。

## 3. 当前架构评估

### 3.1 可保留部分

- `Hardware` 是 EuroPi API 的主要边界；
- `InputManager` 已将旋钮、按钮和 DIN 转换为语义输入；
- Pattern 和 Euclid 算法基本独立于硬件；
- `Controller` 是输入分发和硬件输出的集中入口；
- `Renderer` 没有持久化自己的页面状态；
- 每轨固定输出配对简单、清晰，不需要引入通用路由表。

### 3.2 必须优先处理的问题

| 优先级 | 现状 | 影响 | 目标处理 |
| --- | --- | --- | --- |
| P0 | `Transport.update()` 迟到时只发一个 tick，并以当前时间重建 deadline | 主循环迟到会形成永久相位漂移 | 绝对 deadline + 有界 catch-up + overrun 计数 |
| P0 | `emit_after()` 把 LOW 作为普通旧事件保存 | 旧 LOW 可能截短后续 gate；改速、停机、切轨后仍可能生效 | 每通道 deadline/token 或覆盖式 gate 状态 |
| P0 | tick 中创建 `OutputBus`、事件、列表、元组和新队列 | MicroPython GC 时机不可控，直接形成时钟抖动 | 预分配通道状态和固定容量队列 |
| P0 | DIN 使用无界 Python list 和 `ClockEvent()` | burst 会增长内存；调度队列满时静默丢钟 | 固定环形缓冲区、时间戳、overflow 计数 |
| P1 | `generate_euclidean_pattern()` 每次返回新 list，并创建 counts/remainders、递归栈和旋转中间操作 | 编辑 steps/pulses/rotate 会产生突发分配，后续 GC 可能打断播放 | Seq2 内置原地生成器 + 最大长度预分配 buffer；rotate 外置 |
| P1 | `param_defs()`、`slots()`、`build_global_params()` 重复创建对象和 lambda | UI 刷新/旋钮操作产生大量短命对象 | 初始化时创建并缓存静态参数 schema |
| P1 | `Sequencer.tick()`、`Renderer`、`Track.engine` 都知道轨道类型 | 新增类型需要跨层修改，OFF 还会隐式返回 CV 引擎 | 完整 `TrackFeature` 注册表和显式 NullFeature |
| P1 | CV 的 `pos` 被保存，EUC 播放位置却不保存 | 存档语义不一致，恢复结果不可预测 | 运行态一律不进入项目存档 |
| P1 | 存档没有 schema 版本、迁移和完整校验 | 字段演进或损坏数据难以安全恢复 | schema v2 + v1 迁移 + clamp/default |
| P1 | 保存同步发生在主控制循环 | flash 写入可能阻塞实时输出 | 保存请求、快照和实际写入分离 |
| P2 | Renderer 直接读取可变 Engine 并重新计算参数表 | UI 与播放状态耦合，未来并发时难以保证一致 | 固定大小 `ViewSnapshot` |

### 3.3 当前文档与实现的偏差

现有 `seq2.md` 表述“新增 Engine 并注册即可，其他层零改动”，但当前新增类型至少还要修改：

- `TYPE_NAMES`；
- `Track.__init__` 和 `Track.engine`；
- `Sequencer.tick()`；
- `Renderer._draw_channel()`；
- 参数槽生成；
- 状态字段和编解码。

重构完成的扩展性验收标准不是“有一个基类”，而是新增一个测试用轨道类型时，通用
`ClockService`、`SequencerRuntime`、`OutputScheduler`、`Controller` 均无需修改。

## 4. 设计原则

1. **实时路径不做非必要分配。** 播放启动后的每个基础 tick 不创建事件对象、列表、lambda、格式化字符串或 Command。
2. **绝对时间优先。** 下一个 deadline 从上一个 deadline 累加，不能从“当前执行时间”重新起算。
3. **配置与运行态分开。** Project 描述“要播放什么”，Runtime 描述“现在播到哪里”。
4. **Engine 输出语义，不输出临时对象。** 轨道通过 `set_cv()`、`trigger_gate()` 表达意图，Scheduler 负责电平和截止时间。
5. **ISR 只捕获。** 中断中只记录最少的时间戳/计数，不渲染、不保存、不构造复杂对象。
6. **单文件优先是迁移策略，不是架构限制。** 先在一个文件内建立清晰边界，再用 RAM 数据决定物理拆分。
7. **每阶段保持可运行。** 不进行跨多个阶段才能恢复功能的大爆炸式改写。
8. **本轮只验收 200 MHz。** 按用户决定，Pico 2 超频后的 200 MHz 是唯一真机验收频率；不采集 150 MHz 数据。
9. **EUC buffer 只保存规范化节奏。** rotate 是读取坐标变换，不是重新生成 pattern 的理由。

## 5. 目标架构

```text
DIN IRQ / Internal Timer
          │
          ▼
    ClockCapture ────────────────┐
          │ timestamp / count   │ metrics
          ▼                     ▼
    ClockService ─────────► TransportRuntime
          │ base tick + context
          ▼
   SequencerRuntime
          │
          ├── TrackRuntime(EUC)
          ├── TrackRuntime(CV)
          └── TrackRuntime(OFF / future feature)
          │ semantic output calls
          ▼
   OutputScheduler ─────────► HardwareOutputPort ─────────► cv1..cv6

   ProjectState ◄──── EditorService ◄──── InputManager
        │                    │
        │                    └────► UiState
        ├── PersistenceService
        └── SnapshotWriter ───────► ViewSnapshot ───────► Renderer
```

### 5.1 组件职责

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `ClockCapture` | 捕获 DIN/Timer 时间戳、维护固定队列、统计 overflow | 推进轨道、写 CV、刷新屏幕 |
| `ClockService` | 时钟源、PPQN、绝对 deadline、catch-up、start/stop/continue/reset | 轨道类型、OLED、状态文件 |
| `TransportRuntime` | running、tick index、source、tempo、复用 TickContext | 项目序列内容 |
| `SequencerRuntime` | 遍历启用轨道并分发 TickContext | 根据类型写 if/else 分支 |
| `TrackRuntime` | 消费 tick/update，推进播放头，产生语义输出 | UI 对象、JSON、硬件 API |
| `OutputScheduler` | CV 去重、gate deadline、取消、retrigger/tie、未来边沿 | 音乐模式和轨道参数 |
| `EditorService` | 参数查找、clamp、变更通知、未来量化操作 | 实时 tick |
| `PersistenceService` | schema、校验、迁移、保存请求和成功/失败结果 | 播放头和 UI notice |
| `SnapshotWriter` | 从 model/runtime 写入固定视图快照 | 直接绘制 OLED |
| `Renderer` | 只读取快照并绘制 | 修改模型、创建参数定义 |

## 6. 实时核心设计

### 6.1 ClockService

统一接口建议如下：

```python
class ClockService:
    def start(self, reset=False): ...
    def stop(self): ...
    def continue_(self): ...
    def reset(self): ...
    def set_source(self, source): ...       # INT / EXT，未来可扩 AUTO
    def set_tempo(self, bpm): ...
    def service(self, now_us): ...          # 主循环实现时使用
    def drain_captured_ticks(self): ...     # DIN/Timer 捕获实现时使用
```

`start/stop/continue/reset` 必须具有独立语义：

- `start(reset=True)`：位置归零并开始；
- `stop()`：停止推进，取消所有计划 gate，按策略关闭输出；
- `continue_()`：保留播放位置继续；
- `reset()`：位置归零但不隐式改变 running；
- 切换时钟源时明确采用 start 还是 stop，不在 setter 中隐藏复杂副作用。

内部主循环调度使用：

```text
while now >= next_deadline and catch_up < MAX_CATCH_UP:
    dispatch(next_deadline)
    next_deadline += period
    catch_up += 1
```

超过 `MAX_CATCH_UP` 后不无限补拍，记录 `clock_overrun` 和 `dropped_ticks`，并按明确策略把
deadline 推进到未来。这样既不永久漂移，也不会一次迟到让 UI 永久饿死。

### 6.2 PPQN 迁移策略

内部时钟固定为 24 PPQN，Transport 通过整数相位累加器决定何时推进音乐步。每个基础 tick
执行 `phase += MUL`，达到 24 时减去 24 并发出一步，因此每个四分音符严格推进 MUL 步。
MUL=1/2/3/4/6/8 使用固定间隔；MUL=5/7 在 24 tick 网格上均匀交替，并保持长期速度和相位。

24 PPQN 是三连音与 `MUL=8` 都能整齐表达的最低公共网格；在 240 BPM 下基础周期为
10,416 µs、每秒 96 个基础 tick。48 PPQN 不再是本轮候选，避免把 MicroPython 回调和 OLED
预算加倍。外部 DIN 保持每个边沿推进一个音乐步，不套用内部相位累加器。

PER|FORMER 的 192 PPQN 和硬件定时器证明了高分辨率时钟的功能价值，但其 STM32/FreeRTOS
执行模型不能直接等同于 MicroPython。

### 6.3 外部时钟捕获

用固定容量环形缓冲区代替 `_clock_q`：

```text
timestamps_us[N]
read_index
write_index
overflow_count
```

- IRQ 仅写入时间戳或递增待处理计数；
- 主循环批量 drain，不为每个时钟创建 `ClockEvent`；
- 队列满时保留最新还是最旧必须固定为一种策略，初始建议丢弃最新输入并递增 `overflow_count`，避免改写尚未消费的事件；
- 外部时钟统计最近周期、jitter、超时和 burst；
- 任何 `micropython.schedule()` 失败都进入可观测计数，不再静默忽略。

### 6.4 OutputScheduler

现有功能只有六个固定输出，第一版无需通用动态事件系统。使用预分配数组：

```text
current_voltage[6]
dirty_mask
gate_deadline_us[6]
gate_generation[6]
gate_active_mask
```

轨道只调用：

```python
outputs.set_cv(channel, voltage)
outputs.trigger_gate(channel, high, low, length_us, mode)
outputs.cancel_channel(channel, drive_low=True)
```

Scheduler 必须统一定义：

- 新 gate 覆盖旧 deadline，旧 LOW 不再可能截短新 gate；
- `GLEN` 限制为 10–90%，连续有效步之间始终保留明确 LOW 间隙；
- retrigger 如需低电平间隙，由 Scheduler 生成，不由 Engine 拼接事件；
- 停止、reset、切换类型和 OFF 会取消该轨所有 pending edge；
- 改 BPM 不改变已开始 gate 的绝对截止时间，除非产品行为明确要求重算；
- 相同电压不重复写硬件；
- 输出队列满、deadline 迟到都有指标。

未来 ratchet 需要多个边沿时，再增加固定容量 `ScheduledEdgeQueue`，而不是恢复为无界 Python list。

### 6.5 TickContext

引擎接收一个只读、可复用的上下文，避免逐步扩展函数参数：

```python
TickContext(
    tick_index,
    scheduled_us,
    actual_us,
    period_us,
    transport_state,
)
```

实现时使用 `__slots__` 或可复用轻量对象；不得每 tick 新建。

## 7. TrackFeature 扩展模型

### 7.1 注册项

每种轨道类型注册完整 bundle，而不是只注册 Engine：

```python
TrackFeature(
    type_id,             # OFF / EUC / CV
    config_factory,
    runtime_factory,
    editor_schema,
    snapshot_writer,
    renderer,
    codec,
)
```

MicroPython 实现可以使用带 `__slots__` 的小对象或静态 tuple，避免引入重量级反射。

### 7.2 运行接口

```python
class TrackRuntime:
    def reset(self, reason): ...
    def on_tick(self, context, config, outputs): ...
    def update(self, now_us, context, config, outputs): ...
    def write_snapshot(self, target): ...
```

- `on_tick()` 处理离散步进；
- `update()` 为未来 slide、curve、LFO 等连续输出保留，EUC/CV 可为空操作；
- `reset(reason)` 的 reason 区分 start、manual reset、type change、load；
- Engine 不返回事件列表，不知道 OLED、Command 或 JSON。

### 7.3 OFF 必须是显式 feature

当前 `Track.engine` 在非 EUC 情况下默认返回 CV 引擎，导致 OFF 也隐式拥有 CV 引擎。目标架构中
OFF 使用 `NullTrackRuntime`：进入 OFF 时取消该轨调度、将两路输出归零，之后 tick 为零成本空操作。

### 7.4 静态参数 schema

参数定义在 feature 初始化时创建一次：

```text
id / label / min_provider / max_provider / getter / setter / formatter
```

动态变化的只有范围和值，不重复创建描述对象。当前设置操作是同步、即时且没有 undo 的，因此不需要
为每次旋钮变化创建 Command 子类；统一使用：

```python
editor.set(param_id, value)
```

未来“小节边界切换 pattern”这类量化操作进入独立的固定容量 `PendingActionQueue`，不与普通参数设置混用。

### 7.5 EUC 原地生成器与外置 rotate

Seq2 不再导入共享的 `experimental.euclid.generate_euclidean_pattern()`。在 `seq2.py` 内实现专用的
`_generate_euclidean_into()`，以便针对 `MAX_STEPS=64`、固定生命周期和实时约束优化；共享实现暂不修改，
避免影响 `euclid.py`、`euclidean2.py` 和 `pams.py`。

建议接口：

```python
def _generate_euclidean_into(target, steps, pulses, workspace):
    """把未旋转的规范化 Euclidean pattern 写入 target，不返回新容器。"""
```

内存布局：

- 每个 `EuclidPattern` 在构造时按 `MAX_STEPS` 预分配 pattern buffer；
- 若时钟回调可能在编辑期间抢占读取，则一次性预分配两个 `bytearray(MAX_STEPS)`，在 inactive buffer
  完整生成后原子切换 `active_index`；六个 EUC pattern 的双 buffer 纯数据载荷为 768 bytes；
- Bjorklund 所需 `counts`、`remainders` 和迭代展开栈使用模块级固定 workspace，容量按
  `MAX_STEPS + 1` 分配；生成过程不递归、不 append、不切片；
- workspace 只由控制/编辑路径串行使用，实时读取路径只访问 active pattern；
- buffer 未使用尾部不参与读取，必要时只在 steps 缩短时原地清零，不创建切片。

每个 buffer slot 同时有预分配的 `slot_steps/slot_pulses` 元数据。读取路径先把 `active_index` 读到
局部变量，再从同一 slot 读取 buffer 和 steps；生成完成后最后一步只切换 `active_index`。这样 timer
在任意时刻抢占，都不会看到“新 steps + 旧 buffer”或半生成内容。

生成与参数更新规则：

```text
steps 变化  ─┐
             ├─ clamp/normalize → 若 (steps, pulses) 与已生成 key 不同 → 原地生成一次
pulses 变化 ─┘

rotate 变化 ─── clamp/normalize → 只更新 rotate，不生成
prob 变化   ─── clamp           → 只更新 probability，不生成
load state  ─── 一次性校验全部字段 → 最多生成一次
```

读取时应用 rotate：

```python
def is_on(self, logical_pos):
    source_pos = logical_pos - self.rotate
    if source_pos < 0:
        source_pos += self.steps
    return self._active_pattern[source_pos] != 0
```

该索引方向必须与旧实现“把末尾元素移到开头”的旋转方向完全一致。为保持 UI 和存档兼容，
`rotate` 仍允许保存和显示为 `steps`，读取时把它当作等效的 0 旋转处理。Renderer 和 TrackRuntime
都必须调用同一个 `is_on()`/`value_at()`，不能再直接读取 `.pattern[idx]`，否则显示和实际输出会
出现不同旋转结果。

兼容性测试覆盖整个参数空间：

- `steps=1..64`；
- `pulses=0..steps`；
- `rotate=0..steps`；
- 新实现结果逐项等于冻结的旧实现 golden 结果；
- 重复设置相同 steps/pulses 不调用生成器；
- 连续修改 rotate 不改变 active buffer 身份和内容；
- 反复生成只在两个预分配 buffer 间切换，heap 不随次数下降；
- 生成期间发生 tick 时只能看到完整的旧 pattern 或完整的新 pattern，不能看到半成品。

旧实现只允许作为主机端兼容性 oracle，设备运行时代码不得为此保留 import。

## 8. 状态模型与持久化

### 8.1 四类状态

| 状态 | 示例 | 是否保存 |
| --- | --- | --- |
| `ProjectState` | BPM、时钟源、轨道类型、EUC 参数、CV steps、VLO/VHI | 是 |
| `PerformanceState` | mute、当前 pattern、fill（未来） | 明确逐项决定 |
| `RuntimeState` | 播放头、tick、deadline、gate 电平、overflow 统计 | 否 |
| `UiState` | page、sel、pickup、notice、dirty | 否 |

CV `pos` 从 schema v2 起不再保存；EUC 和 CV 在 load 后都按统一 reset 策略启动。

### 8.2 schema v2 最终格式

```json
{
  "schema_version": 2,
  "clock": {
    "source": "INT",
    "bpm": 120,
    "mul": 4
  },
  "tracks": [
    {
      "type": "EUC",
      "v_lo": 0,
      "v_hi": 5,
      "EUC": {"g1": {}, "g2": {}, "merge": 0},
      "CVSEQ": {"length": 8, "values": [], "gate_len": 50}
    }
  ]
}
```

最终字段名在阶段三实现前冻结。兼容要求：

- 无 `schema_version` 的现有状态视为 v1；
- v1 的 `CVSEQ` 字段在 v2 中保持原名，降低迁移风险；
- 忽略旧 `pos`；
- 所有数值在加载时 clamp；
- 缺失字段使用默认值，未知字段忽略；
- 无效轨道类型回退为 EUC，并记录诊断；
- 单轨损坏不应导致其他轨道全部丢失。

### 8.3 保存策略

B2 长按产生 `SaveRequested`，PersistenceService 负责：

1. 从 ProjectState 生成一致快照；
2. 在非 tick 路径序列化；
3. 执行写入并返回成功或失败；
4. 只有成功才显示 `SAVED`。

阶段三必须先测量真实 flash 写入的最坏耗时，再选择运行中保存策略：

- 若不会破坏时序，保持即时保存；
- 若会阻塞，则运行时只标记 pending，在 transport 停止后写入，并显示 `SAVE PENDING`；
- 不引入 `_thread`，除非有独立的稳定性验证和明确的共享状态协议。

## 9. UI 与渲染

Renderer 改为读取固定大小快照，不直接遍历运行 Engine：

```text
ViewSnapshot
  page / selected_slot
  header_label / header_value
  track_type
  sequence_bits_or_values[16]
  playhead
  last_output
  notice
```

快照由预分配结构复用。字符串格式化只在选中 slot 或其值确实改变时执行。OLED 采用已确定的
分页策略：完整画面只渲染一次到 framebuffer，随后通过四个预分配 `memoryview`，每轮主循环最多
传输一个 8px page。beat 只重画 EUC/CV 的底部 playhead 带；页面、参数和 notice 变化才全量重绘。
动态渲染与单页发送使用 6 ms clock/gate guard；全量渲染实测最坏约 25 ms，另用 25 ms gate guard，
避免已有短 gate 在渲染中变 stale。返回后立即再次服务 clock/gate。显示预算按最高 240 BPM、24 PPQN
（10,416 µs 周期）设计，外部时钟暂不参与显示 slack 预测。200 MHz 真机测得单页最大 3,979 µs。

## 10. 物理文件组织

### 10.1 第一阶段：仍为单文件

先在 `seq2.py` 内按以下顺序整理区域，减少迁移变量：

```text
constants / diagnostics
ports and capture
project model
runtime model
clock
output scheduler
track features
editor and pages
view snapshot and renderer
persistence
controller and app
```

### 10.2 拆分决策门

阶段四完成后比较单文件和多模块版本的：

- 启动后 `gc.mem_free()`；
- import 峰值内存；
- UF2/设备部署复杂度；
- 主机测试可维护性。

只有多模块版本在真机上有足够余量，才拆成：

```text
seq2/
  model.py
  clock.py
  scheduler.py
  runtime.py
  features/euclid.py
  features/cvseq.py
  editor.py
  ui.py
  persistence.py
  app.py
```

如果多模块增加明显常驻 RAM，则开发目录保持模块化，通过构建脚本生成单一部署版 `seq2.py`；生成文件不手工编辑。

## 11. 分阶段迁移计划

### 阶段 0：建立行为和性能基线

交付：

- `FakeHardware`、`FakeClock`、可控 `ticks_us/ticks_ms`；
- 当前 EUC/CV 输出的 golden tests；
- 冻结旧 Euclidean 生成器在全部 `steps/pulses/rotate` 参数空间的兼容性 oracle；
- gate、按钮、保存、渲染、状态 round-trip 测试；
- 真机 benchmark 脚本和结果模板；
- 记录 200 MHz 下的 tick 延迟、OLED 耗时和 heap。

退出条件：

- 现有行为全部被测试描述；
- 已能复现迟到漂移、stale LOW、外部队列 overflow 风险中的至少两个；
- 后续阶段可以用同一组测试做差异比较。

### 阶段 1A：替换 Euclidean pattern 生成器

交付：

- 在 Seq2 内实现非递归 `_generate_euclidean_into()`；
- 为每个 EUC pattern 预分配固定 buffer，并预分配共享 workspace；
- `steps/pulses` 采用 change detection，只有规范化后的生成 key 变化才重建；
- rotate 移到 `is_on()` 索引层，Renderer 和 Runtime 复用同一读取接口；
- 移除 Seq2 对共享 `generate_euclidean_pattern()` 的设备端依赖。

退出条件：

- 全参数空间结果与旧实现一致；
- 连续 rotate 不触发生成或改写 pattern buffer；
- load state 最多生成一次；
- 重复编辑和生成没有随操作次数增长的 heap 消耗；
- tick 不会观察到部分生成的 pattern。

回退边界：只恢复 `EuclidPattern` 的旧生成 adapter，不影响 Scheduler、状态 schema 和 UI。

### 阶段 1B：替换 OutputBus 和延时队列

交付：

- 引入预分配 `OutputScheduler`；
- Engine 改用语义输出调用；
- 删除每拍 `CVOutputEvent` 和 `_scheduled` 动态列表；
- stop/reset/type change/OFF 的统一取消行为；
- 输出只在电压变化时落硬件。

退出条件：

- 连续 gate、90% gate、retrigger、BPM 修改和切轨测试通过；
- 播放 10,000 tick 后，热路径没有持续 heap 下降；
- 不再存在旧 LOW 截短新 gate 的路径。

回退边界：仅恢复旧 OutputBus，不影响 ProjectState 和 UI。

### 阶段 2：重构 ClockService 和 TransportRuntime

交付：

- 绝对 deadline 和有界 catch-up；
- start/stop/continue/reset 状态机；
- 外部时钟固定环形缓冲区和时间戳；
- jitter、overrun、dropped tick、queue overflow 指标；
- 固定 24 PPQN 基础时钟和 MUL 1–8 整数相位累加器；
- 将旧 `mul` 行为映射到明确 divisor，保持已有存档节奏。

退出条件：

- 主循环故意迟到后不会永久相位漂移；
- `ticks_us()` 回绕测试通过；
- 外部 burst 不增长队列，overflow 可见；
- OLED 刷新不会导致错误 gate，极限场景的 UI 降级行为有文档。
- 240 BPM、24 PPQN 预算下 OLED 四页可持续完成，不发生 display starvation。

回退边界：ClockService 保留兼容 adapter，可切回旧步频，不回退 Scheduler。

### 阶段 3：分离配置、运行态与持久化

交付：

- `ProjectState` / `RuntimeState` / `UiState` 明确分离；
- schema v2 和 v1→v2 迁移；
- 完整范围校验、缺省和局部损坏恢复；
- 保存请求与实际写入分离；
- load 后统一 reset，不恢复播放头和 pending gate。

退出条件：

- v1 fixture 迁移后音乐数据等价；
- v2 round-trip 稳定；
- 损坏单轨不会破坏其他轨；
- 保存成功/失败/pending 的 UI 行为有测试。

回退边界：保留只读 v1 loader；任何 v2 写入问题都可停止新格式写入。

### 阶段 4：TrackFeature、Editor 和 ViewSnapshot

交付：

- OFF/EUC/CV 全部通过注册项接入；
- 删除 `Sequencer` 和通用 Renderer 中的类型分支；
- 参数 schema 初始化一次；
- 普通编辑不再创建 Command；
- Renderer 只读 ViewSnapshot。

退出条件：

- 运行期间反复切页面、选参数不会持续制造参数描述对象；
- 加入只用于测试的 `ConstantCVFeature` 时，不修改通用实时核心；
- 原 UI 和存档行为测试全部通过。

回退边界：每个 feature adapter 可逐个替换，EUC 和 CV 不必同一提交迁移。

### 阶段 5：决定是否拆文件

交付：

- 单文件/多模块 RAM 与启动时间报告；
- 若拆分，完成模块化和部署验证；
- 更新 `seq2.md`，删除已过时的架构描述；
- 保留本文件作为决策与迁移记录。

退出条件：

- `scripts/linux/deploy_all.sh` 部署内容正确；
- 真机冷启动、状态加载、菜单进入和完整播放通过；
- 文档、代码和测试对组件职责的表述一致。

## 12. 测试矩阵

### 12.1 主机测试

| 领域 | 必测场景 |
| --- | --- |
| Clock | 准点、迟到一拍、迟到多拍、超过 catch-up 上限、BPM 改变、ticks 回绕 |
| Transport | start、stop、continue、reset、INT↔EXT、停止状态改 BPM |
| Gate | 10/50/90%、连续有效步、休止步、retrigger、旧 deadline、停止/切轨取消 |
| Output | 相同电压不重复写、六通道隔离、OFF 归零、固定配对 |
| External | 正常 DIN、burst、队列满、schedule 失败、超时恢复 |
| Euclidean | steps/pulses/rotate 全空间兼容、相同值不重建、rotate 零重建、buffer 原子切换 |
| Feature | EUC/CV 等价、NullFeature、注册测试 feature、连续 update 空操作 |
| State | v1 迁移、v2 round-trip、字段缺失、范围越界、未知类型、单轨损坏 |
| UI | pickup、页面切换、保存 notice、快照一致性、参数范围动态变化 |

### 12.2 真机测试

在 Pico 2 超频后的 200 MHz 运行：

- 20、120、240 BPM；
- 内部和外部时钟；
- EUC×3、CV×3、混合三轨；
- `GLEN=10/50/90%`；
- 持续旋钮操作、页面切换和 OLED 刷新；
- 运行中请求保存；
- 外部时钟 burst、拔插和恢复；
- 至少 30 分钟压力运行，另做一次 10,000 tick 可重复基准。

## 13. 验收指标

以下指标以阶段 0 的测量工具为准，结果必须同时记录频率、BPM、PPQN、轨道组合和 OLED 状态：

- 10,000 个基础 tick 后，热路径没有随 tick 数线性增长的 heap 占用；
- EUC 连续修改 rotate 不调用生成器；连续修改 steps/pulses 只复用预分配 buffer/workspace；
- 新 EUC 生成器在 `steps=1..64`、`pulses=0..steps`、`rotate=0..steps` 范围内与旧节奏逐项一致；
- 240 BPM、三轨同时工作时无未解释的 dropped tick；
- 200 MHz 下 p99 tick 调度延迟不高于 1 ms；若外设写入导致无法达到，需给出分项数据并修订阈值，不能静默放宽；
- 最坏延迟不会累积成持续相位漂移；
- OLED 和保存期间不产生错误 gate 或 stale LOW；
- 外部队列始终有界，overflow 和 dropped tick 均可查询；
- `GLEN=90%` 的连续步保留 LOW 间隙且行为稳定；
- 新增一个 TrackFeature 不修改 ClockService、TransportRuntime、SequencerRuntime、OutputScheduler 和 Controller；
- 旧 v1 项目可迁移，EUC/CV 音乐参数不丢失；
- 所有已有 `software/tests/contrib/test_seq2.py` 测试继续通过。

## 14. 风险与控制

| 风险 | 控制措施 |
| --- | --- |
| 24 PPQN 增加基础回调负载 | 热路径固定内存；OLED 分页；200 MHz 测量 10,000 基础 tick |
| Timer 回调与 UI 编辑共享状态 | 配置在安全点提交；回调只读稳定快照；避免复杂锁和分配 |
| 模块拆分增加 MicroPython RAM | 拆分设为阶段五决策门，保留单文件生成方案 |
| schema v2 破坏已有项目 | v1 fixture、只读迁移、版本字段、写入前验证 |
| 保存仍阻塞实时路径 | pending-save 避让下一音乐步而非空基础 tick；测量 flash 最坏时延和 catch-up |
| 优化后行为不易察觉地变化 | 阶段 0 golden tests；每阶段小提交和独立回退边界 |
| 指标本身产生抖动 | profiler 可完全关闭；预分配计数器；基准区分带/不带诊断 |

## 15. 已确定的设计决策

- 保留三轨固定输出配对，不引入通用路由层；
- 优先重写实时数据通路；Euclid 保持节奏语义，但按固定内存约束替换实现；
- 第一阶段保留单文件；
- Seq2 使用内置、原地、预分配的 Euclidean 生成器，共享生成器暂不修改；
- Euclidean canonical buffer 不包含 rotate，旋转只在读取索引层应用；
- 输出调度以每通道状态/deadline 为主，不继续使用对象事件列表；
- 运行态不保存；
- 新存档使用显式 schema 版本；
- 内部时钟固定使用 24 PPQN；外部 DIN 仍是一边沿一步；
- OLED 调度固定按 240 BPM、24 PPQN 的内部时钟最坏预算设计；外部时钟本轮不参与显示预测；
- 本轮按用户决定以 200 MHz 作为唯一真机验收频率；
- 不在首轮引入线程或完整 undo Command 系统。

## 16. 实施前检查表

- [x] 确认本文的行为兼容项与 `seq2.md` 一致；
- [x] 为当前实现生成 v1 状态 fixture；
- [x] 冻结旧 Euclidean 生成结果并加入全参数空间测试；
- [x] 建立虚拟时钟和 FakeHardware；
- [ ] 记录 200 MHz 基线；
- [x] 先完成阶段 0 的主机基线，再开始 Scheduler（本次工作按要求不创建 git commit）；
- [x] 每阶段单独运行主机测试；
- [x] 最终实现的 200 MHz 真机 smoke、10,000 tick 基准和 30 分钟压力测试；
- [ ] 每阶段记录 heap、延迟和行为差异；
- [x] 阶段四后再决定是否物理拆文件（缺少真机 import/RAM 数据，当前保留单文件）；
- [x] 最终同步更新 `seq2.md` 和用户操作说明。

## 17. 实施进度

### 2026-08-02：阶段 0（主机部分）

- 新增 FakeClock、FakeHardware、EUC/CV 输出与 gate 时序 golden tests、v1 轨道状态 fixture；
- 测试明确复现旧实现的相位漂移和 stale LOW 两条风险路径；
- 新增 `scripts/benchmark_seq2.py`、`scripts/stress_seq2.py` 与 `seq2_benchmark_results.md`，已填写 200 MHz 数据；
- 主机仓库回归：`235 passed, 1 skipped`（阶段 1A 前的完整运行结果）。

### 2026-08-02：阶段 1A

- Seq2 已移除共享 Euclidean 生成器依赖，改用非递归 `_generate_euclidean_into()`；
- 每个 pattern 使用两个 `MAX_STEPS` 固定 bytearray，共享固定 workspace，完成后切换 active slot；
- rotate 改为读取时索引转换；Renderer 与播放路径统一使用 `is_on()`；
- 相同生成 key、rotate、prob 不重建；load state 最多重建一次；
- 全参数空间 oracle、固定 buffer、inactive publish 等测试通过；真机 10,000 tick heap delta 为 0。

### 2026-08-02：阶段 1B

- 动态 `CVOutputEvent`、`OutputBus` 和 `_scheduled` 列表已由六通道固定 `OutputScheduler` 取代；
- 每通道只有一个可替换 gate deadline，retrigger 不再留下 stale LOW；
- 相同电压写入被抑制，OFF、类型切换、停止和 reset 统一取消输出；
- 初版曾允许 `GLEN=100%` legato；真机调度验证后范围收窄为 10–90%，连续步保留 LOW 间隙。

### 2026-08-02：阶段 2（主机部分）

- `ClockCapture` 使用 16 项固定时间戳 ring，记录 overflow 和 schedule failure；
- `ClockService` 与 `TransportRuntime` 分离，采用绝对 deadline、4 tick 有界 catch-up 和 dropped/overrun/jitter 指标；
- 内部与外部时钟复用同一个 `TickContext`，同时携带 scheduled/actual/period；外部时钟支持 jitter、timeout 和恢复统计；
- start/stop/continue/reset、BPM 修改、迟到多拍和 `ticks_us()` 回绕测试通过；
- 内部时钟已切换为 24 PPQN，MUL 1–8 经整数相位累加器推进；外部步进语义不变；
- 10,000 基础 tick 的 MUL=7 长期步数/余相位无漂移；音乐 step context 使用下一实际网格间隔；
- dropped base tick 只推进相位、不产生输出，并单独记录跨过的音乐步，恢复后重新对齐 PPQN 网格；
- 保存避让下一音乐步而不是空基础 tick，避免 24 PPQN 下永久停留在 `SAVING`；
- OLED 已拆为四个 8px page，每轮最多提交一页；beat 只更新 playhead 带；动态渲染/发送使用 6 ms guard，全量渲染使用 25 ms gate guard；

### 2026-08-02：阶段 3

- 新增 `ProjectState` schema v2、`RuntimeState`、`UiState` 和 `PersistenceService`；
- Track 分离常驻 config 与 runtime；EUC/CV 播放头只存在于 `EuclidRuntime` / `CVSeqRuntime`，且没有序列化入口；
- v1 状态可迁移；v2 不保存播放头、running 或 pending gate；未知 schema 被拒绝；
- 字段范围、缺省和单轨损坏隔离有测试；load 后统一 reset；
- 保存改为 request/flush 两步，UI 区分 `SAVING`、`SAVED` 和 `SAVE ERR`。

### 2026-08-02：阶段 4

- OFF/EUC/CV 经 `TrackFeature` 注册，Sequencer 与 Renderer 删除轨道类型分支；
- feature bundle 包含 config/runtime factory、codec、tick、snapshot writer 与 renderer；
- 参数 descriptor 和 16 个 CV step slot 只创建一次，普通编辑直接调用 setter，不创建 Command；
- `EditorService` 统一处理参数变更、类型切换与输出取消副作用；
- Controller 复用固定 `ViewSnapshot`，Renderer 不再读取 Track/Engine；
- 测试 feature 无需改通用核心即可输出。

### 2026-08-02：阶段 5（主机部分）

- 当前保留单一 `seq2.py`；真机已取得 import/空闲 RAM 数据，但没有证据表明拆分能改善实时性或维护成本；
- `seq2.md` 已按实际 Clock、Scheduler、Feature、Snapshot 和 schema v2 行为重写；
- 真机脚本已输出 import/冷启动 RAM 与时间、10,000 step tick 延迟/heap、OLED 全屏/单页耗时，以及 240 BPM 的真实 24 PPQN Transport 负载；
- `deploy_all.sh` 会经 `deploy_contrib.sh` 部署 `seq2.py`，脚本语法检查通过；
- 最终主机验证：Seq2 `73 passed`，Display 分页测试 `2 passed`；全仓库 `297 passed, 1 skipped`；Python 编译、部署 shell 语法与 `git diff --check` 通过；
- 基准脚本的 import、10,000 tick、OLED 和 startup 路径已在 Pico 2 / 200 MHz 执行；最终数值见 `seq2_benchmark_results.md`；
- 用户将真机验收范围收窄为仅超频后的 200 MHz；脚本会在 import 前切频，全部测量完成后恢复原频率；
- 真机完成最小部署、20/120/240 BPM 内部时钟、CV/gate、OLED 与 30 分钟压力测试；外部时钟按本轮决定忽略，保存和物理旋钮/波形仍需手测。

## 18. 主机完成度审计

| 领域 | 当前证据 | 状态 |
| --- | --- | --- |
| Clock | 准点/迟到一拍/多拍/catch-up 上限/BPM/ticks 回绕；复用 TickContext | 已验证 |
| Transport | start/stop/continue/reset、INT↔EXT、运行/停止改速、runtime callbacks | 已验证 |
| Gate | EUC 5 ms、CV 10/50/90%、rest、retrigger、旧 deadline、停止/重置/切轨取消 | 已验证 |
| Output | 相同电压去重、六通道隔离、OFF 归零、固定三轨配对、late edge 指标 | 已验证 |
| External | 时间戳顺序、固定 ring、overflow、预分配 ClockEvent、schedule failure、jitter/timeout 恢复 | 已验证 |
| Euclidean | 全参数空间 oracle、change detection、rotate 零生成、双 buffer 发布 | 已验证 |
| Feature | config/runtime 分离、静态 schema、EditorService、NullFeature、测试 feature factory/codec/reset/tick/update | 已验证 |
| State | v1 音乐数据迁移、v2 round-trip、缺省/越界/未知类型/单轨损坏、runtime 全清 | 已验证 |
| UI | pickup、动态范围、页面按钮、保存三状态、snapshot 稳定性 | 已验证 |
| 主机整体 | Seq2 `73 passed`，Display `2 passed`；全仓库 `297 passed, 1 skipped`；Python 编译、shell 语法、diff whitespace、benchmark mock smoke | 已验证 |
| 真机 | 200 MHz、24 PPQN、20/120/240 BPM、CV/gate/OLED、10,000 tick、30 分钟压力 | 自动验证通过；外部/save/物理手测不在本轮自动结果内 |

## 19. 参考实现

- [PER|FORMER 源码](https://github.com/westlicht/performer)：参考其 engine、model、UI、平台和测试边界；不复制其运行时模型。
- [PER|FORMER TrackEngine](https://github.com/westlicht/performer/blob/master/src/apps/sequencer/engine/TrackEngine.h)：参考离散 tick 与连续 update 的职责划分。
- [PER|FORMER 用户手册：Clock](https://westlicht.github.io/performer/manual/#clock)：其内部使用 192 PPQN 和硬件定时器，并将轨道 divisor 与主时钟分开。
- `software/contrib/pams.py`：EuroPi 项目内已有 48 PPQN、`machine.Timer` 和每通道时钟分频的真机先例。
- `software/tests/contrib/test_seq2.py`：当前交互行为的最小回归基线。
