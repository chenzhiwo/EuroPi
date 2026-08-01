# Seq2 — 3 轨多功能音序器（架构需求文档）

> 本文件是 **架构 / 需求说明**，是 `software/contrib/seq2.py` 的实现依据。
> seq2 在 `euclidean2.py` 的分层架构基础上重构而来，保留其事件流、命令、渲染分层与
> 性能约束，新增「**轨道类型（Track Type）**」抽象，使每条轨道可在多种音序器引擎之间切换。

---

## 1. 目标与范围

### 1.1 目标
1. **3 条轨道**，每条轨道占用一对**固定的** CV 输出（共 6 路），可独立切换轨道类型。
   **固定配对**（无需路由表，jack 归属稳定）：
   - track1 → `cv1`(主/音高) + `cv4`(门/钟)
   - track2 → `cv2`(主/音高) + `cv5`(门/钟)
   - track3 → `cv3`(主/音高) + `cv6`(门/钟)
2. 轨道类型（首版三种，架构上可扩展）：
   - **OFF**：该轨关闭，两端输出 0V。
   - **EUC**：欧几里得节奏引擎（沿用 euclidean2 的双发生器 + 合并逻辑）；
     第一个 CV（cv1~3）出门（欧几里得节奏），第二个 CV（cv4~6）出时钟（每个步稳定脉冲）。
  - **CV**：最长 16 步的 CV 步进音序器，输出保持型电压；
    **主通道出音高，门通道（cv4~6）出门脉冲**（长度 `GLEN`）；
    **步值 = 0 时该步不出门**（主通道仍出 `v_lo`）。
3. **沿用现有 page 系统**（P0 全局 + P1..P3 轨道页，B1/B2 翻页）。
   P0 新增 3 个参数 `T1..T3`，用于切换 track1–3 的类型。
4. **移除全局输出电压设置**，改为**每轨独立的输出电压范围**。

### 1.2 非目标（首版不做）
- 不做多文件拆分：与 euclidean2 一致，**保持单文件** `seq2.py`（MicroPython 导入成本 / 部署简单）。
- 不做 pattern 链（song mode）、不做每轨独立分频/摇摆、不做量化到音阶（架构预留）。
- 不做与 euclidean2 存档的兼容迁移（seq2 使用自己的 state 文件）。

---

## 2. 硬件资源与约束

| 资源 | 用途 |
| --- | --- |
| cv1..cv6 | 3 对固定配对输出（cv1/4, cv2/5, cv3/6）；cv1~3 出主信号/音高，cv4~6 出门/钟 |
| K1 | **槽位选择**（参数槽 + 步槽，见 §5.2） |
| K2 | **数值编辑**（拾取策略；编辑步槽时改用 jump 策略） |
| B1 | 短按上一页；长按 (>500ms) 切换内部时钟启动/停止（仅 `CLK=INT`） |
| B2 | 短按下一页；长按 (>500ms) 保存状态，成功后在顶部一行左对齐显示 `SAVED` 1 秒 |
| B1+B2 | 返回菜单（系统行为，脚本不拦截） |
| DIN | 外部时钟（沿用 Hardware 的 ISR + 事件队列） |
| OLED | 128×32，1bpp；16 列 × 8px 恰好铺满屏宽 |

**性能约束（继承自 euclidean2，不得回退）**
- 时钟精度优先：屏幕刷新前检查 `time_to_next_clock_us() >= T_SHOW_US(20ms)`，否则跳过。
- `tick()` 热路径避免堆分配与 `isinstance` 链过长；输出事件对象**按轨复用单例**（见 §4.3）。
- flash 写入只在长按 B2 时发生，绝不在主循环自动落盘。

---

## 3. 分层架构

沿用 euclidean2 的分层，**新增 TrackEngine 抽象层**（粗体为新增/变更）：

```
EuroPi 硬件
    │
    ▼
Hardware              唯一调用 EuroPi API；去抖采样、DIN 队列、输出事件落盘
    │                 **移除 set_gate_level（电平不再是全局概念）**
    ▼
InputManager          稳定采样 → KnobTurn / ButtonEvent / ClockEvent
    │
    ▼
Controller            事件分发 + _exec 命令出口 + 主循环 + _on_beat
    ├─ Pages          交互 → Command（**按轨道类型取参数表 / 槽位表**）
    ├─ AppState       UI 状态：page / sel(槽位) / k2_picked / dirty
    ├─ Sequencer      分发 ClockTick → 各 Track；**持有 OutputBus 与延时调度**
    │    └─ Track     轨道容器：cv_index + TrackSettings + **engines{type: TrackEngine}**
    │         ├─ **TrackEngine (接口)**
    │         │     ├─ **EuclidEngine**  Pattern×2 + merge + TrackPlayer
    │         │     └─ **CVSeqEngine**   StepPattern(≤16) + TrackPlayer
    │         └─ TrackSettings  **v_lo / v_hi（每轨输出电压范围）**
    └─ Transport      全局时间：BPM / mul / 源 / running（**不再持有 level**）
    │
    ▼
ViewModel → Renderer  **按轨道类型分派 TrackView**，经 Hardware 绘制（无状态）
```

### 3.1 保持不变的约束
- Sequencer / Engine / Pattern / Track / Transport **不出现任何 EuroPi API 调用**。
- Renderer 无状态；所有模型读取经 ViewModel。
- 单一事实来源：Pattern=音乐数据，Transport=时间，AppState=UI 状态，TrackSettings=输出电压范围。
- 换平台只重写 `Hardware`。

---

## 4. 模型层设计

### 4.1 TrackEngine 接口（新增，核心抽象）

一个 Engine 封装「某种轨道类型的全部内容」：音乐数据 + 播放头 + 参数表 + 输出生成 + 视图 + 序列化。
新增轨道类型 = 新增一个 Engine 子类 + 注册进类型表，**其余各层零改动**。

```python
class TrackEngine:
    TYPE = ""            # "EUC" / "CV"
    PARAMS = []          # 参数槽表（见 §5.1），供 Pages / ViewModel 表驱动
    def step_slots(self): return 0        # 步槽数量（EUC 返回 0，CV 返回 length）
    def tick(self, ch, settings, out):    # 推进一拍并向 OutputBus 发输出事件
        raise NotImplementedError
    def reset(self): ...                  # 播放头归零（切换类型 / 停止时）
    def view(self):                       # 返回渲染就绪的 TrackView
        raise NotImplementedError
    def to_dict(self) / from_dict(d): ...
```

要点：
- `tick()` 的入参 `out` 是 **OutputBus**（§4.3），Engine 只 `emit` 语义事件，绝不碰硬件。
- `settings` 由 Track 传入（`v_lo` / `v_hi`），Engine 据此把「逻辑值」翻译成电压。
- `PARAMS` 是表驱动的参数描述（缩写 / 范围 / 读值 / 造命令 / 显示格式），与 euclidean2 的
  `GLOBAL_PARAMS` 同构，消除 Pages 里的 if-elif 链。

### 4.2 两种 Engine

#### EuclidEngine（EUC）
- 组成：`EuclidPattern × 2` + `merge` + `TrackPlayer`（与 euclidean2 完全一致的算法与行为）。
- 参数表（K1 参数槽，10 项）：

  | 缩写 | 含义 | 范围 |
  | --- | --- | --- |
  | ROT1/ROT2 | 相位旋转 | 0 ~ steps |
  | PLS1/PLS2 | 脉冲数 | 0 ~ steps |
  | STP1/STP2 | 步数 | 1 ~ 64 |
  | PRB1/PRB2 | 触发保留概率 | 0 ~ 100 |
  | MERG | 合并模式 | OR/AND/XOR/G1/G2 |
  | LVL | **本轨触发电压**（= `settings.v_hi`） | 0 ~ 10 V |

- 步槽数量：0（K1 全程用于选参数，行为与 euclidean2 相同）。
- 输出（固定配对双通道）：第一个 CV 出「门」——本拍触发 → `emit(CV(cv_index, v_hi))` 并
  `emit_after(GATE_MS, CV(cv_index, v_lo))`，不触发 → `emit(CV(cv_index, v_lo))`；
  第二个 CV 出「时钟」——每个步稳定 `emit(CV(gate_index, v_hi))` 并
  `emit_after(GATE_MS, CV(gate_index, v_lo))`。

#### CVSeqEngine（CV 使用）
- 组成：`StepPattern`（`values[16]`，每步 **0~127**，与 MIDI note 可能性一致）+ `TrackPlayer`（`pos`）。
- **步值 = MIDI note 编号（0~127）**，输出时线性映射到 `[v_lo, v_hi]`：
  `voltage = v_lo + (v_hi - v_lo) * value / 127`。
  → 改电压范围时**不破坏已编辑的音型**；同时可直接当作「按音序排列的 note 数」使用，
  UI 顶栏同时显示音名（如 `C3`）与换算电压。
- 参数表（K1 参数槽，CV 4 项）：

  | 缩写 | 含义 | 范围 |
  | --- | --- | --- |
  | LEN | 序列长度（最长步数） | 1 ~ 16 |
  | VLO | 本轨输出电压下限（= `settings.v_lo`） | 0 ~ 10 V |
  | VHI | 本轨输出电压上限（= `settings.v_hi`） | 0 ~ 10 V |
  | GLEN | 门长度（占每步时长的百分比，恒定输出在配对门通道） | 5 ~ 100 % |

- 步槽数量：`LEN`（1..16），K1 后段逐步选中。
- 输出（CV，固定配对双通道）：
  每拍 `pos = (pos+1) % LEN`，主通道 `emit(CV(cv_index, voltage(values[pos])))` 保持电平直到下一拍；
  门通道（始终配对）**仅当步值 > 0 时**步开始时 `emit(CV(gate_index, v_hi))`，
  `emit_after(GLEN% × step_us, CV(gate_index, v_lo))` 拉低
  （`step_us` 由全局时钟周期决定，`GLEN%` 即 `GLEN/100`）。
  **步值 = 0 时门通道不出脉冲**（该步即静音步）。

> 约束：`VLO <= VHI`，编辑任一侧时自动 clamp 另一侧。

### 4.3 输出事件与 OutputBus（变更）

因电压范围下沉到每轨，Hardware 不再知道「门高电平」，故：
- **移除** `GateOutputEvent(channel, state)` 与 `Hardware.set_gate_level()`。
- **输出原语统一为** `CVOutputEvent(channel, voltage)`；Gate 语义由 Engine 翻译为
  「on=v_hi，GATE_MS 后 v_lo」两次电压事件。
- `ClockOutputEvent` 保留为框架词汇（当前无独立时钟输出引脚）。

```python
class OutputBus:
    def emit(self, ev): ...                  # 本拍立即输出
    def emit_after(self, delay_us, ev): ...  # 延时输出（门控拉低）
```
`Sequencer` 持有 OutputBus 与延时队列，主循环经 `Sequencer.pump(now_us)` 取出到期事件；
`Controller._apply_outputs()` 仍是唯一落硬件的出口。

**热路径分配**：每轨预分配 2 个 `CVOutputEvent` 单例（on / off），`tick()` 中只改字段，
避免每拍 6~12 次堆分配（MicroPython GC 抖动会直接体现为时钟抖动）。

### 4.4 Track（容器，变更）

```python
class Track:
    settings: TrackSettings      # v_lo(默认0) / v_hi(默认5)，每轨独立
    type: str                    # "OFF" / "EUC" / "CV"
    engines: dict                # {"EUC": EuclidEngine(), "CVSEQ": CVSeqEngine()}  # 两种引擎常驻
    cv_index: int                # 固定 = track_index（主/音高通道；cv1..cv3）
    gate_index: int              # 固定 = track_index + 3（门/钟通道；cv4..cv6）
    @property
    def engine(self):
        return self.engines["EUC"] if self.type == "EUC" else self.engines["CVSEQ"]
```
- **两种引擎实例常驻**：切换类型后再切回，参数与音型**原样保留**（用户可 A/B 对比）。
  代价是 RAM（每轨多一个引擎），估算见 §9。
- **固定配对**：`cv_index = idx`，`gate_index = idx + 3`，不随类型变化（jack 归属稳定）。
  EUC 时第一个 CV 出门（节奏）、第二个 CV 出时钟；CV 时主通道出音高、门通道出门脉冲。
- 切换类型时：`new_engine.reset()`（播放头归零，与其他轨对齐）、`AppState.sel = 0`、
  `k2_picked = False`、`dirty = True`；**通道配对不变**。

### 4.5 Transport（变更）
- **删除** `level` 字段、`set_level()`、`SetLevel` 命令、`GLOBAL_PARAMS` 中的 `LVL` 项。
- 其余（BPM / mul / source / running / beat_index / update / ext_tick）保持不变。

### 4.6 固定输出配对（Fixed Pairing，硬件约束）

EuroPi 仅有 **6 个 CV 输出**（0~10V，可兼作门），无独立 gate 引脚。输出采用**固定配对**而非路由表，
每轨占用一对通道，jack 归属天然稳定：

| 轨道 | 主通道（音高 / 触发） | 门 / 钟通道 |
| --- | --- | --- |
| track1 | `cv1`（index 0） | `cv4`（index 3） |
| track2 | `cv2`（index 1） | `cv5`（index 4） |
| track3 | `cv3`（index 2） | `cv6`（index 5） |

- `Track.cv_index = idx`，`Track.gate_index = idx + 3`（构造时确定，不随类型变化）。
- 通道分配实现为常量，无 `ROUTE` 表、无溢出、无冲突（结构上不可能两轨争用同一输出）。
- **类型决定门通道语义**：
  - EUC：第一个 CV（cv1~3）出门（欧几里得节奏），第二个 CV（cv4~6）出时钟（每个步稳定脉冲）。
  - CV：主通道出保持型音高，门通道出门脉冲（`GLEN` 控制长度）；**步值 = 0 时不出门**。
  - OFF：两端均 0V。
- UI 顶栏标注每轨配对 jack（如 `cv1+cv4`）便于接线核对。

> 与 §4.4 的关系：`cv_index / gate_index` 为构造期常量，不单独持久化（见 §8）。

---

## 5. UI / 交互设计

### 5.1 页面结构

```
P0 全局 → P1 轨道1 → P2 轨道2 → P3 轨道3 → 循环
```

**P0 全局页参数（6 项，K1 遍历）**

| # | 缩写 | 含义 | 范围 / 显示 |
| --- | --- | --- | --- |
| 0 | CLK | 时钟源 | INT / EXT |
| 1 | BPM | 内部时钟速度 | 20 ~ 240 |
| 2 | MUL | 时钟倍率（实际 = BPM×MUL） | 1 ~ 8 |
| 3..5 | **T1..T3** | **轨道 1..3 的类型** | **OFF / EUC / CV（离散参数）** |

> 全局输出电压 `LVL` 已删除；改由 P1..P3 的 `LVL`（EUC）或 `VLO/VHI`（CV）设置。
> 输出配对为固定常量（见 §4.6），无路由参数。

**P1..P3 轨道页**：参数集**由该轨当前 Engine 的 `PARAMS` 决定**（Pages 不写死）。

### 5.2 K1：统一「槽位（Slot）」模型（新增）

K1 选择的对象从「参数」推广为「**槽位**」：

```
slots = [参数槽 × len(PARAMS)]  ++  [步槽 × engine.step_slots()]
```

- **EUC 轨**：`step_slots()==0` → 槽位 = 10 个参数，行为与 euclidean2 完全一致。
- **CV 轨**：槽位 = `LEN/VLO/VHI` 3 个参数 + `LEN` 个步槽（最多 19 槽）。
  即「K1 最开始的一段切换当前参数，后段在 steps 之间切换」。
- 映射沿用 `idx = int(p * n)` 截断（自带 ±0.5 档迟滞）；n=19 时每槽约 5.3% 旋钮行程，
  远大于 `KNOB_DEADBAND(1%)`，可稳定选中。
- `LEN` 变小导致 `sel` 越界时，自动 clamp 到最后一个有效槽。

### 5.3 K2：数值编辑

- 沿用**拾取（pickup）策略**：旋钮位置匹配当前值（离散参数 tol=0，连续参数
  `tol = max(1,(max-min)//32)`）后才开始生效；**编辑步槽时例外**。
- 编辑**步槽**时 K2 改用 **jump 策略**：旋钮位置直接映射步值（0~127），无拾取死区，转动即生效。
- 选中**参数槽** → 编辑该参数；选中**步槽** → 编辑该步的**值 0~127（MIDI note 编号）**，
  顶栏**只显示 cv 数值**（如 `S05:64`），不显示换算电压。
- 换槽 / 换页 / 换轨道类型都会 `k2_picked = False`（编辑步槽不依赖此标志）。

### 5.4 顶栏与屏幕布局（128×32）

```
行0 (y=0)    左：槽位缩写:值        右：P{n}
EUC 轨： 行1(y=8)  Gen1 序列 16 列    行2(y=16) Gen2 序列 16 列    行3(y=26) 唱头
CV  轨： y=9..25   16 列竖条（条高 ∝ 步值百分位）                  y=26..31 唱头/游标
P0：     仅行0
```

**CV 轨道页绘制细则**
- 16 列 × 8px；列 c 对应步 c（`c >= LEN` 的列画一条 1px 基线表示「不在序列内」）。
- 条形：`h = round(value/127 * 16)`，自 y=25 向上填充，宽 6px。
- **编辑头**：当 K1 选中某一步槽时，底部播放头行（y=29）在该步列画一条**横线（宽 6，与步条一致）**
  作为编辑指示。
- **唱头**：底行（y=27）在 `pos` 列画 6×3 实心块，表示实时播放位置；
  **编辑头与唱头可同时显示**（二者均宽 6，与步条宽度一致）。

### 5.5 命令（Controller → Model）

沿用命令模式，新增/调整：

| 命令 | 说明 |
| --- | --- |
| `SetTrackType(track, type)` | **新增**：切换轨道类型（OFF/EUC/CV）；含 reset / sel 归零（轨道页），全局页保持当前 T 槽位。通道配对不变（见 §4.6） |
| `SetTrackVLo/SetTrackVHi(track, v)` | **新增**：每轨电压范围（EUC 的 `LVL` 即 `SetTrackVHi`） |
| `SetCVLength(engine, n)` | **新增**：CV 序列长度 1~16 |
| `SetCVStep(engine, idx, value)` | **新增**：第 idx 步值 0~127（MIDI note 编号） |
| `SetCVGateLen(engine, pct)` | **新增**：CV 门长度 5~100 %（恒定输出在配对门通道） |
| `SetTrackParam` / `SetMerge` | 沿用（作用于 EuclidEngine） |
| `SetTransportSource/SetBpm/SetMul` | 沿用 |
| ~~`SetLevel`~~ | **删除**（全局电压已移除） |

所有变更仍经 `Controller._exec(cmd)` 单一出口执行（改模型 + 置 dirty）。

---

## 6. ViewModel / Renderer（变更）

- `SequencerViewModel` 负责：顶栏文本（含步槽显示格式）、当前页轨道、槽位语义。
- **按类型分派视图**：`engine.view()` 返回 `EuclidTrackView` 或 `CVTrackView`
  （纯数据：要画的行、列值、游标位置、唱头位置）。
- `Renderer` 只有 `_draw_global / _draw_euclid / _draw_cv` 三个绘制分支，**无状态判断逻辑**。
- 新增轨道类型时：新增一个 View + 一个绘制分支，Controller / Pages / Sequencer 不改。

---

## 7. 事件流（与 euclidean2 同构）

**输入流**
```
硬件 → Hardware → InputManager → Controller.dispatch
   ├ KnobTurn(K1) → Pages.on_knob1 → 槽位选择（读 engine.PARAMS / step_slots）
   ├ KnobTurn(K2) → Pages.on_knob2 → 拾取 → Command → _exec → 改模型 + dirty
   ├ ButtonEvent  → 翻页 / 内部时钟启停 / 保存
   └ ClockEvent   → Transport.ext_tick（仅 EXT）
```

**时钟 / 输出流**
```
INT: Transport.update(now) ─┐
EXT: ClockEvent ────────────┴→ Controller._on_beat
   → Sequencer.tick(now): for track in tracks: track.engine.tick(ch, settings, bus)
   → bus 中的 CVOutputEvent → Controller._apply_outputs → Hardware.set_cv
   → （EUC）GATE_MS 后 Sequencer.pump() 取出延时事件 → 拉回 v_lo
```

**渲染流**
```
dirty 且 距下次时钟 >= T_SHOW_US → Renderer.render → ViewModel → engine.view() → Hardware 绘制
```

---

## 8. 状态持久化

- 存档文件：`saved_state_Seq2.txt`（由 EuroPiScript 按类名生成，与 euclidean2 互不干扰）。
- 触发：**仅长按 B2**；保存成功后在屏幕左上角显示 `SAVED` 1 秒，仅覆盖状态栏，时钟、
  音序输出与其余画面不会暂停。
- Schema：

```json
{
  "v": 1,
  "clock": {"source": "INT", "bpm": 120, "mul": 4},
  "tracks": [
    {
      "type": "EUC",                 # OFF / EUC / CV
      "v_lo": 0, "v_hi": 5,
      "EUC": {"g1": {"steps":16,"pulses":5,"rot":0,"prob":100},
              "g2": {"steps":16,"pulses":3,"rot":0,"prob":100},
              "merge": 0},
      "CVSEQ": {"length": 8, "values": [0,20,40,...],   # 0~127
                "gate_len": 50}
    }
  ]
}
```
- **两种引擎的配置都持久化**（对应 §4.4 的常驻策略）；`cv_index`/`gate_index` 为构造期常量，不落盘。
- `type=="OFF"` 的轨道也保存其（空闲的）引擎配置，切回时即恢复。
- 加载容错：字段缺失取默认值；`type` 非法回退 `"EUC"`；`values` 长度不足补 0、超长截断；
  异常整体捕获后使用默认状态（不阻止启动）。

---

## 9. 资源估算与风险

| 项 | 估算 / 对策 |
| --- | --- |
| RAM：每轨两个引擎常驻 | EUC ≈ 2×64 步列表；CV = 16 个小整数。3 轨合计仍在数 KB 量级，可接受 |
| 热路径分配 | 输出事件按轨复用单例；`tick()` 内不建列表（直接 `bus.emit`） |
| 存档体积 | 3 轨 × 两引擎 JSON ≈ 1KB，一次性写入可接受 |
| K1 槽位增多导致误选 | 槽宽 ≥5% ≫ 死区 1%；截断映射自带迟滞 |
| CV 轨无 GATE_MS 拉低 | 明确为设计（CV 保持型输出）；如需触发型请用 EUC 轨 |
| **固定配对通道** | 仅 6 个 CV 输出，3 轨各占一对（cv1/4, cv2/5, cv3/6）；配对为常量，无路由表 / 无溢出 / 无冲突 |

---

## 10. 实现步骤（建议顺序）

1. 复制 `euclidean2.py` → `seq2.py`，重命名类 `Seq2(EuroPiScript)`，`display_name() = "Seq2"`。
2. **输出层改造**：删 `GateOutputEvent` / `set_gate_level`，引入 `OutputBus` +
   `CVOutputEvent` 单例复用；`Sequencer` 持有延时队列。
3. **电压下沉**：`TrackSettings` 加 `v_lo/v_hi`；删 `Transport.level` / `SetLevel` /
   `GLOBAL_PARAMS.LVL`。
4. **抽出 `TrackEngine` 接口**，把现有 Pattern×2 + merge + player 收进 `EuclidEngine`，
   补 `PARAMS` 表（含 LVL），确保**行为与 euclidean2 完全一致**（回归基线）。
5. **新增 `CVSeqEngine`** + `StepPattern` + 参数表 + 步槽。
6. **Pages 槽位模型**：`on_knob1` 改为槽位选择；`on_knob2` 分参数槽 / 步槽两路，
   全部经 `PARAMS` 表驱动，消除 if-elif 链。
7. **P0 新增 T1..T3** 类型切换参数 + `SetTrackType` 命令（固定配对，无路由参数）。
8. **Renderer/ViewModel**：`engine.view()` + `_draw_cv` 分支。
9. **持久化**：新 schema + 容错。
10. 编写 `seq2.md` 用户说明（本文件后续补「用户手册」小节）并回归验证。

---

## 11. 验收标准

- [ ] EUC 轨在默认参数下的发声、UI、时钟行为与 `euclidean2.py` **逐项一致**。
- [ ] P0 可为每轨独立切换 EUC/CV，切换后立即生效且播放头对齐；在 P0 切换类型不会跳回 CLK 槽位。
- [ ] 类型来回切换后，两侧引擎的参数与音型均**无损保留**。
- [ ] CV 轨：K1 前段选参数、后段选步；K2 可编辑步电压，输出电压落在 `[VLO,VHI]` 内并保持到下一拍。
- [ ] 修改 `VLO/VHI` 时已编辑音型形态不变（只整体缩放）。
- [ ] 全局页无 `LVL`；每轨电压互不影响。
- [ ] 长按 B1 可切换内部时钟启动/停止；`CLK=EXT` 时不影响外部时钟。
- [ ] 长按 B2 保存 → 重启后（含类型、两套引擎配置、电压范围）完整恢复。
- [ ] INT 480BPM（120×4）下时钟抖动与 euclidean2 同级；`PROFILE=True` 时 `on_beat` 均值不高于基线。

---

## 12. 待确认项（Open Questions）

> 以下 4 项已在初版设计阶段与用户确认落定，保留记录供回溯。

1. **CV 步值分辨率**：✅ 定为 **0~127**，与 MIDI note 可能性一致（每个步值即一个 note 编号）。
2. **CV 轨是否需要伴随 gate**：✅ CV 轨在配对门通道（cv4~6）输出门脉冲（参数 `GLEN`）；
   **步值 = 0 时该步不出门**（静音步）。
   因 EuroPi 仅 6 个 CV 输出、无独立 gate 引脚，采用**固定配对**模型（见 §4.6）：
   每轨占用一对 `cv(i)` + `cv(i+3)`，jack 归属稳定；轨道类型枚举为 `OFF/EUC/CV`
   （原 CVG 概念并入 CV，门通道恒为配对通道）。
3. **步槽是否支持「跳过/静音步」**：❌ 首版不支持，每步都输出。
4. **CV 轨是否需要独立方向 / 分频**：❌ 首版不做，架构已在 `TrackSettings` 预留。

### 12.1 次级决策（已实现确认）
- **CV 电压映射**：✅ 采用**线性映射** `v_lo + (v_hi - v_lo) * note / 127`，与 euclidean2 的电压
  风格一致；`v_lo/v_hi` 即每轨输出区间。1V/oct 量化首版不做。
- **路由方式**：✅ 采用**固定配对**（cv1/4, cv2/5, cv3/6），无全局路由表 / 无 R1..R6 参数。
  类型切换不改动通道配对，EUC 门通道出触发、CV 门通道出门脉冲。
