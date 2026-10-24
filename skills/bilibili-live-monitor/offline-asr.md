# 离线音频转写子 Skill（`offline-asr`）

> **加载与注册约定**：本文件是面向 AI 操作者的内部子 skill，仅由唯一根 skill（`skills/bilibili-live-monitor/SKILL.md`）按需链接加载，**不单独注册为顶层 skill**。
> **隐私与公开仓库红线**：本文档及所有公开仓库文件仅使用合成示例（如 `recordings/123456/clip.mka`、`outputs/offline_asr`、`.venv-mlx/bin/python`），严禁写入本机主机名、真实直播间号、个人路径、真实盘点统计数字或真实转写内容。真实源路径、模型运行日志与转写产物仅保留在本机 overlay 与私有输出目录中。

## 1. 定位与设计边界

`offline-asr` 是 `bilibili_live_monitor` 的小型离线批处理子 CLI，用于对本地已落盘的录制音频素材执行只读盘点、分层抽样规划、可恢复转写与状态查询。

- **保持小型与复用现有能力**：首版直接复用包内已有的 ASR JSONL worker 通信协议与模型桥接逻辑，**不设计新的分布式或后台调度系统**。
- **职责聚焦**：本子 skill 仅负责离线素材盘点、抽样/有界执行与转写证据生成，**不自动生成内容分析报告**。
- **原素材绝对只读**：全流程严禁修改、截断或删除任何原始音频文件（即使遇到零字节、未封口或损坏文件，也仅记录跳过/失败原因并保留原文件）。

---

## 2. CLI 动作与参数规范

入口命令：`python -m bilibili_live_monitor offline-asr <scan|plan|run|status>`

| 动作 | 必选路径参数 | 核心职责与约束 |
| :--- | :--- | :--- |
| `scan` | `--input <dir> --output <dir>` | 只读扫描输入目录，提取文件元数据并冻结写入 `inventory.jsonl`。 |
| `plan` | `--input <dir> --output <dir>` | 基于冻结清单按房间累计容器时长分层取样，交替选取各房间的起/中/末位置文件。 |
| `run` | `--input <dir> --output <dir>` | 校验文件身份并执行切块转写（必须显式带 `--sample N`、`--max-files N` 或 `--full` 之一）。 |
| `status` | `--output <dir>` | 仅需 `--output`；只读返回任务计数、已处理对齐时长、推理时长与RTF。 |

### 标准操作序列（合成示例）

```bash
# 1. 只读盘点素材（例如包含 recordings/123456/clip.mka），冻结生成 outputs/offline_asr/inventory.jsonl
python -m bilibili_live_monitor offline-asr scan \
  --input recordings \
  --output outputs/offline_asr

# 2. 按房间累计容器时长分层抽样，交替选取起/中/末样本
python -m bilibili_live_monitor offline-asr plan \
  --input recordings \
  --output outputs/offline_asr \
  --sample 4

# 3. 演练执行（校验清单、锁与计划队列，不加载 ASR 模型）
python -m bilibili_live_monitor offline-asr run \
  --input recordings \
  --output outputs/offline_asr \
  --sample 4 \
  --dry-run

# 4. 指定本地 ASR 解释器执行有界转写（最多处理 10 个文件）
python -m bilibili_live_monitor offline-asr run \
  --input recordings \
  --output outputs/offline_asr \
  --max-files 10 \
  --asr-python .venv-mlx/bin/python

# 5. 查看当前输出目录的执行与恢复状态（仅传 --output）
python -m bilibili_live_monitor offline-asr status \
  --output outputs/offline_asr
```

---

## 3. 清单冻结、断点恢复与并发护栏

1. **`scan` 清单冻结与状态分类**：
   - 先确认输入素材已经停止写入；容器可读并不证明音频无缺口。
   - 记录每个素材文件的相对/规范路径、文件大小（size）、修改时间（`mtime`）、`sha256`、容器时长（container duration）以及封口可读状态（如 `sealed_readable` 等状态）。
   - 零字节文件、未正常封口或容器损坏的不完整文件会被跳过，并在清单/状态中保留具体失败或跳过原因，**绝不删除原文件**。
   - 盘点结果冻结于 `<output>/inventory.jsonl`。后续若源目录新增文件，不会自动混入，必须显式重新执行 `scan`。
   - **输入根目录绑定**：同一个 `--output` 目录绑定唯一的 `--input` 根目录，禁止在已有输出目录上更换另一个 `--input` 根目录。
2. **`plan` 分层交替抽样**：
   - 按房间（如示例房间 `123456`）累计容器时长进行分层取样（stratified sampling），并在同一房间样本中交替挑选起始、中间、末尾（起/中/末）文件，覆盖不同时间段。
3. **`run` 显式规模与恢复校验**：
   - **防误触全量护栏**：调用 `run` 时若未提供 `--sample N`、`--max-files N` 或 `--full` 之一，CLI 直接拒绝执行；`--dry-run` 仅做流程演练，不加载模型。
   - **四元组恢复身份**：文件恢复身份定义为 `路径 + 文件大小 + mtime + sha256`。在真正处理每个文件前，必须再次核对磁盘实际文件是否仍与 `inventory.jsonl` 一致；一旦发现文件被修改或替换，立即拒绝处理该项并要求重新 `scan`。
   - **单持有者互斥（Single-holder Lock）**：同一 `--output` 目录同一时刻仅允许一个 `run` 进程持有运行锁；当 `run` 正在执行时，`scan` 与 `plan` 禁止重写正在运行的队列。
   - **失败保留与非零退出**：执行中出现的任何文件或切块失败均须落盘保留错误原因，且进程以**非零退出码**结束，严禁将“部分失败（partial failure）”静默视为“全部通过”。

---

## 4. 模型环境、离线隔离与进程生命周期

- **解释器与模型参数**：
  - 通过 `--asr-python`（如 `.venv-mlx/bin/python`）或环境变量 `ASR_PYTHON` 指定已安装 ASR 依赖的本地 Python 解释器。
  - `--model` 默认值为 `Qwen/Qwen3-ASR-1.7B`。**拒绝任何 URL 形式的 `--model` 参数**。
- **严格离线运行**：
  - 模型权重缓存必须预先存在于本机缓存中。
  - 启动模型 worker 子进程时强制设置 `HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1`，禁止运行时联网拉取模型，也不进行任何网络上传。
- **单进程串行与有界重试**：
  - 正常运行时仅维护**一个**模型子进程串行处理切块批次，直接复用包内 JSONL worker 协议。
  - 设有明确的 worker 启动超时与单次推理超时；超时或崩溃时最多执行**一次有界重试**。
- **信号收尾与 GPU 作业隔离**：
  - 捕获中断（`SIGINT`）或 `SIGTERM` 时，仅定向终止并回收当前 `offline-asr` 进程自己拉起的模型子进程，**严禁操作、杀伤或干扰宿主机上的其他 GPU 作业**。

---

## 5. 音频对齐、切块与统计解读边界

### 音频预处理与切块策略
1. **时间戳空洞补齐**：针对录制容器时间戳存在空洞或跳变的情况，调用 `ffmpeg` 时使用 `aresample=async=1:first_pts=0` 以静音补齐对齐时间轴。
2. **约 30 秒切块与全量送检**：对齐后的音频按约 30 秒切块送入 ASR。**不启用 VAD 预过滤，也不因低 RMS 能量跳过切块**，避免漏掉轻声或气声片段。
3. **临时文件即用即删**：切块解码产生的临时 `.wav` 文件在当前切块识别完成后立即删除，不残留临时音频。
4. **转写输出记录**：每个切块记录相对当前文件的起止时间范围、识别文本 `text`、能量指标 `RMS`、`error` / `truncated` 状态以及模型元数据。注意：**切块时间范围仅代表相对文件的切块区间，不是词级（word-level）对齐时间戳**。

### 统计与语义判读铁律
- **不设置 `counted_as_speech` 真值**：输出与状态中不提供 `counted_as_speech` 布尔真值字段。
- **`RMS` 仅代表信号能量**：
  - **非空文本 + 高能量（高 RMS）** **不能**证明该段一定是真人说话或主播人声（游戏语音、歌声、强节奏 BGM 同样会产生高能量与识别文字）。
  - **低能量（安静）下的非空文本** **不能**直接断言为非语音或模型幻觉（主播气声、耳语、远麦轻声说话同样表现为低 RMS 文本）。
  - 任何后续分析必须将 **文本（`text`）**、**能量（`RMS`）** 与 **语音类别（人声/游戏/歌声/BGM）** 严格区分开。
- **时长口径不可混用**：
  - **容器时长（container duration）** 与 **经 `aresample` 补静音对齐后的时长** 均包含静音补洞或非语音区间，**既不等于连续音频包时长，也不等于实际有效语音时长**。
