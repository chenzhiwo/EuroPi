# Euclid2 6ch — 6 通道双欧几里得音序器

EuroPi 上的 6 通道双欧几里得节奏/GATE 音序器，灵感来自 Elektron Digitakt 2 的
「双机器(Dual Machine)」：每路输出由两组独立欧几里得发生器 (Gen1 + Gen2) 组成，
二者通过合并逻辑实时得到该通道最终的门信号。

> 本模块是**节奏 / GATE 音序器**，不是音高 / CV 音序器：每通道只有一个插孔，
> 合并结果只决定 ON/OFF，「全局输出电压」即门的触发高电压（0~10V，默认 5V），于 P0 设置并作用于全部 6 路。

## 硬件资源

- 6 路 CV/GATE 输出：cv1..cv6（与 6 个通道一一对应）
- 2 旋钮：K1 参数选择，K2 参数数值
- 2 按键：B1、B2 页面切换
- 外部时钟：DIN（上升沿驱动）
- 屏幕：128×32 单色（每行最多 16 字符，共 4 行）

## 控制

| 操作 | 功能 |
| --- | --- |
| B1 短按 | 上一页 |
| B2 短按 | 下一页 |
| B1 长按 (>500ms) | 手动推进一拍（无外部时钟时试听） |
| B2 长按 (>500ms) | 回到全局时钟页 (P0) |
| 同时按住 B1+B2 0.5s | 返回菜单（系统行为） |
| K1 旋转 | 选择当前页的参数项（跳转策略，带 ±0.5 档迟滞） |
| K2 旋转 | 修改选中参数（拾取策略，需旋钮位置匹配当前值后才生效） |

## 页面结构

```
P0 全局时钟 → P1 CH1 → P2 CH2 → P3 CH3 → P4 CH4 → P5 CH5 → P6 CH6 → 循环
```

### 全局时钟页 (P0)
- 参数 1 时钟源：INT（内部时钟）/ EXT（外部时钟，DIN 驱动）
- 参数 2 BPM：20–240，仅 INT 模式可调；EXT 模式显示 `EXT`
- 参数 3 倍率 mul：1–16，实际时钟 = `BPM × mul`（默认 BPM=120, mul=4 → 480 BPM）
- 参数 4 输出电压：0–10V，作用于全部 6 路（默认 5V）

### 通道编辑页 (P1..P6)
每通道 9 项参数，由 K1 遍历（缩写显示在状态栏）：

| 缩写 | 参数 | 范围 |
| --- | --- | --- |
| R1 | Gen1 相位旋转 | 0 ~ Gen1 步数 |
| R2 | Gen2 相位旋转 | 0 ~ Gen2 步数 |
| S1 | Gen1 序列步数 | 4 ~ 128 |
| S2 | Gen2 序列步数 | 4 ~ 128 |
| P1 | Gen1 脉冲数 | 0 ~ Gen1 步数 |
| P2 | Gen2 脉冲数 | 0 ~ Gen2 步数 |
| B1 | Gen1 触发保留概率 | 0 ~ 100 |
| B2 | Gen2 触发保留概率 | 0 ~ 100 |
| MER | 合并模式 | OR / AND / XOR / G1 / G2 |

> 输出电压为**全局设置项**（见下方「全局时钟页 参数4」），作用于全部 6 路，不在通道页调节。

## 屏幕布局（128×32）

```
行0  状态栏            P3 C3 R1:5
行1  Gen1 序列         实心=触发，空心=空步，当前步固定最左(col0)
行2  Gen2 序列         同上，各自按自身播放头独立左滚
行3  合并模式          MER:OR
```

- **横向滚动**：当前步固定显示在该行最左一格，向右展示其后连续步，整窗最多 16 步；
  播放头推进时整窗向左滚动。序列长度 ≤16 时显示全序列。
- **当前步标记**：每行 col0 下方有下划线，指示该发生器当前播放头。
- 界面仅展示两路**原生生成序列**，不展示合并后的序列。

## 合并逻辑（触发时刻运算）

每一步在触发时实时计算：取 `Gen1.pattern[pos]` 与 `Gen2.pattern[pos]`（各自先按自身
概率做保留判定），再按合并模式得到该通道 ON/OFF：

- `OR`：任一路触发即触发
- `AND`：两路都触发才触发
- `XOR`：仅一路触发时触发
- `G1`：仅取 Gen1（忽略 Gen2）
- `G2`：仅取 Gen2（忽略 Gen2）

概率 `Prob%`：该步本应触发时，以 `Prob/100` 概率保持触发，否则视为空步（不影响存储序列）。
`G1`/`G2` 模式仅应用被选那一路的概率。

## 时钟

- **内部时钟 (INT)**：由内部 Timer 周期驱动，周期 = `60000 / (BPM × mul)` ms；门长按
  `min(周期/2, 30ms)` 自动关闭。
- **外部时钟 (EXT)**：DIN 上升沿推进全部通道，下降沿关闭所有门（跟随输入门宽）。
- 全局时钟统一驱动全部 6 路，通道间无独立分频/延迟（即「无 Shift 时序偏移」）；
  Gen1/Gen2 因步长不同产生的相对漂移属预期行为。

## 状态保存

状态持久化由脚本顶部常量 `SAVE_STATES` 控制：

- `SAVE_STATES = True`：所有参数（每通道 steps/pulses/rot/prob/merge，以及全局
  时钟源/BPM/倍率 mul/输出电压）自动保存到 `saved_state_Euclidean2.txt`，断电或返回菜单后下次启动自动恢复；
  状态变更约 1 秒落盘一次。
- `SAVE_STATES = False`（当前默认）：完全屏蔽 save states 功能——既不从 flash 加载历史
  状态（每次启动都用默认/初始参数），也不在运行中写盘，避免阻塞式写 flash 造成的卡顿。
  需要保留参数时改回 `True`。

## 内部架构（单文件分层，参照 ARCHITECTURE.md 与 Refactoring Plan）

代码保持为**单个文件**，但内部按 `europi-ws/ARCHITECTURE.md` 的原则严格分层；各层只通过
「语义事件 / 命令 / 模型读取 / 输出事件」交互，绝不直接互相改状态，且 Sequencer / Pattern /
Track / Transport 中**不出现任何 EuroPi API 调用**：

| 层 | 类 | 职责 | 单一事实来源 |
| --- | --- | --- | --- |
| Hardware Adapter | `Hardware` | 唯一访问 `oled/k1/k2/b1/b2/cv*/din` 的模块；将输出事件 apply 到物理 CV/Gate；持有 DIN ISR 与时钟事件队列 | — |
| Input Manager | `InputManager` | 稳定采样 → 语义事件（`KnobTurn` / `ButtonEvent` / `ClockEvent`） | — |
| Output Events | `OutputEvent` → `CVOutputEvent` / `GateOutputEvent` / `ClockOutputEvent` | Sequencer Core → Controller 的通信载体（硬件无关） | — |
| Commands | `Command` 子类 | Controller → Model 的通信载体（与输入事件对称） | — |
| Application | `Euclidean2`(EuroPiScript) | 装配 Hardware / 模型 / Controller；状态持久化 | — |
| Controller | `Controller` | 事件分发 + 命令派发（`_exec` 统一出口）+ 主循环（几乎无业务） | — |
| UI Pages | `Pages` | 把交互译为命令，只产出 `Command`，不直改模型 | — |
| ApplicationState | `AppState` | 仅 UI 状态（当前页 / 选中项 / K2 拾取 / dirty） | UI 状态 |
| Sequencer Core | `Pattern`(接口) / `EuclidPattern` / `TrackSettings` / `TrackPlayer` / `Track` / `Sequencer` | `Pattern`=音乐内容(what)；`TrackSettings`=播放配置(how)；`TrackPlayer`=播放头/相位(where)；`Sequencer`=分发 ClockTick 并产出**输出事件** | 音乐数据 |
| Transport | `Transport` | 全局时间：BPM / 倍率 mul / 输出电压 level / 时钟源 / 运行状态 | 时间 |
| ViewModel | `SequencerViewModel` | 渲染用视图模型：把模型读数翻译为渲染就绪视图（唱头分页 / 参数取值） | — |
| Renderer | `Renderer` | 无状态：读 ViewModel、经 Hardware 绘制，不修改、不持有持久数据 | — |

关键约束（来自重构计划 6 步）：
- **Sequencer 与硬件解耦**：`Sequencer` 不持有 Hardware，每个 ClockTick 产出 `GateOutputEvent` /
  `CVOutputEvent` 等语义事件；门控「GATE_MS 后拉低」以定时输出事件在内部调度（`Sequencer.pump` 取走）。
- **Pattern 接口**：`Pattern` 为抽象接口；`EuclidPattern` 是其实现。Sequencer 只依赖接口，新增算法只需新增子类。
- **Track 三层拆分**：`Track = Pattern ×2 + TrackSettings + TrackPlayer`，分别回答「演奏什么 / 怎么播 / 播到哪」。
- **Application / Controller / Pages 分离**：装配与持久化在 `Euclidean2`；事件分发/主循环在 `Controller`；交互→命令在 `Pages`。
- **ViewModel 渲染**：`Renderer` 经 `SequencerViewModel` 读模型，不做状态判断。

数据流：`硬件 → Hardware → InputManager(事件) → Controller → [UI Pages 命令 → Transport/Track] / [ClockTick → Sequencer → 输出事件 → Hardware]`；
渲染由 Controller 在「脏」时经 `Renderer.render(..., hw)` 调用，`Renderer` 经 `SequencerViewModel` 读 AppState + Sequencer/Transport。
换硬件平台只需重写 `Hardware` 适配器，其余层保持不变。

## 实现要点（对应 euclidean2_dev.md）

- 双序列分别独立维护，合并在触发时刻计算，不预生成 LCM 全数组。
- 参数编辑触发 `regenerate()` 时**保留播放头**，实时调参听感连续。
- 时钟与门控走事件驱动（Timer / DIN 回调），屏幕仅在「脏」时重绘，主循环不空转计算。
- K1 跳转带迟滞；K2 拾取策略需旋钮位置匹配当前值后才生效，避免误改。
