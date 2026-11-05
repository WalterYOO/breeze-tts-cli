# breeze-tts-cli（btts）PRD v0.3

> 状态：P0 已实现（`fb48022`）；v0.3 把模型下载纳入 P0，支持 Hugging Face 与 ModelScope。
> 模型：Breeze-TTS-2，本地权重 `../breeze-tts/ms-models/Breeze-TTS-2`（7.2GB）。
> 上游代码基线：`breezeblue-ai/breeze-tts` @ `daa6cb2efe5471f09369229f322d20bb956cb3c3`。
> 参考同类工具：`../qwen-tts-cli`（借鉴模式，不照搬功能）。

---

## 0. 已锁定的决策

| 决策 | 结论 | 限定条件 |
|---|---|---|
| 运行环境 | 以 macOS MPS 为主 | 定位为**离线 / 近实时制作**，不承诺流式、不承诺实时对话 |
| 推理代码 | 复制进本仓库，不依赖 `../breeze-tts` 路径 | 只 vendor MPS 必需源码；权重与 `qwen-tts` 不进仓库 |
| P0 范围 | 设计工作台 + freeze + say 三路由 + 模型下载/校验 | 砍掉远程后端、批处理、项目锁、自动评分、后台任务系统 |
| 模型获取 | `model download` 支持 Hugging Face 与 ModelScope；`model verify` 离线校验 | 不自动下载、不后台更新；必须显式确认并接受非商业许可 |

**实测依据**

- MPS 加载模型：**8.6 秒**，`torch.mps.driver_allocated_memory` 约 **7.98GB**。
- 最小 vendored 集合独立加载：**7.2 秒**，设备 `mps:0`。
- 生成速度：约 **RTF 9**（5.52 秒音频 / 49.52 秒计算，来自现有 `manifest.json`）。
- 权重目录 7.2GB，其中 `audio_tokenizer/` 651MB。

结论：单次冷加载不到 10 秒，**P0 不需要常驻 worker**；`design explore` 一次加载可摊薄到多个候选。worker 推迟到 P1。

---

## 1. 核心洞察

### 1.1 Voice Design 是无状态的

Breeze 的请求里没有 `voice_id`，也没有可保存的 voice prompt tensor。一次生成只由以下因素决定：

```text
instruction + text + seed + cfg_scale + model digest
```

因此：

- 纯 design 适合一次性生成和探索；
- 要让同一音色跨文本稳定复用，可靠锚点是**参考音频 + 精确转写**（`ref_clone_tata`）；
- 情绪、语速、表演指导是**单次 direction**（`ref_edit_tata`），不应写进音色身份。

产品必须把 **Identity（音色身份）** 与 **Direction（单次表演）** 分开。

### 1.2 用户现有工作流就是产品原型

`../breeze-tts/outputs/voice_design/` 显示了真实方法：

1. 手写中文 instruction，固定试听句；
2. 按 seed / cfg 生成候选 A–I；
3. 拼 `all_candidates.wav`（每段间隔 1 秒）；
4. 用 F0 分布、HNR、频谱重心、LTAS 斜率与频段能量比做客观对比；
5. 人工挑“低 + 干净”的平衡点；
6. 用选中的 `boy.wav` 作参考音频 + 精确转写做克隆。

`btts` 应把这套动作变成一条命令链。

---

## 2. 产品定位与边界

### 2.1 定位

- 音色设计实验台；
- instruction 调试器；
- 候选客观分析与盲听工具；
- 音色卡（voice profile）管理；
- 可复现的 Breeze 请求执行器。

### 2.2 目标用户

- 做角色音色的 Agent / 游戏 / 动画原型团队；
- 没有参考音频、想靠描述找音色的创作者；
- 需要固定 seed / cfg / model digest 做可复现实验的研究者；
- 已有授权参考音频、想做克隆和声音指导的用户。

### 2.3 明确不做

- 长文本分段、字幕、ASR、强制对齐；
- 多说话人对话、内置音色库；
- 模型训练 / 微调 / LoRA；
- 浏览器 Web Studio、桌面 GUI；
- 没有参考依据的“音色审美自动评分”；
- 把自托管输出宣称为可商业使用。

### 2.4 许可

- 代码：Apache-2.0。
- 模型权重与自托管输出：BreezeBlue Research and Non-Commercial License。
- `doctor`、`voice add`、`voice show`、freeze、导出物和 README 都要显式提示。

---

## 3. P0 范围

### 3.1 核心闭环

```text
model download（首次）→ model verify → doctor
→ design init → design lint → design explore
→ design report（指标 + 试听卷 + HTML）
→ design freeze（--as design 或 --as clone）
→ voice add / list / show
→ say（design / clone / direction 三路由）
```

### 3.2 P0 命令

```text
btts model download [--source huggingface|modelscope] [--repo ID]
                        [--revision REV] [--to DIR] [--force] [--yes] [--json]
btts model verify [--model DIR] [--deep] [--json]

btts doctor [--bench] [--json]

btts design init FILE
btts design lint FILE [--show-rendered]
btts design explore FILE --out RUN_DIR [--resume] [--wide]
btts design report RUN_DIR [--metrics basic|full] [--csv] [--html] [--reel]
btts design freeze RUN_DIR --take ID --name NAME [--as design|clone] [--confirm-transcript]

btts voice add NAME --ref WAV --text FILE --confirm-transcript
btts voice list
btts voice show NAME

btts say TEXT --design FILE [--seed N] [--cfg N] [--out FILE] [--play]
btts say TEXT --voice @NAME [--out FILE] [--play]
btts say TEXT --voice @NAME --direction TEXT [--cfg N] [--out FILE] [--play]
```

### 3.3 P0 明确砍掉

- 远程 HTTP 后端、鉴权、远程任务提交；
- 模型自动更新、后台下载、多镜像测速切换、模型删除/缓存 GC；
- `breeze_infer.api`、batch API、CUDA fast path；
- 常驻 worker、后台 job 系统（P1）；
- 项目锁、`.bvoice` 导入导出、`voice publish`；
- 交互式终端 A/B（P0 用离线 HTML）；
- 自动 shortlist、ASR/CER、说话人相似度、LLM 评分；
- `direction lint/explore` 独立工作台；
- 16 个以上候选的默认矩阵；
- 长文本分段、字幕、BGM、后处理、训练/微调；
- MP3/FLAC、`--out -` 流式 PCM；
- 通用全局音频缓存。

### 3.4 默认候选矩阵

MPS RTF≈9，默认必须克制：

| 模式 | 矩阵 | 说明 |
|---|---|---|
| 默认 | 2 seed × 2 cfg = **4 个候选** | `seeds=42,43`、`cfg=2.5,4.0` |
| `--wide` | **8 个候选** | 2 seed × 2 cfg × 2 试听脚本 |
| 显式 | 16+ | 必须手动指定，并在开始前列出预计耗时、写盘大小，必要时要求 `--yes` |

试听文本默认 **2–3 秒**，`max_new_tokens≈300`。生成前打印：

```text
预计 4 个候选、每个约 2.5 秒音频
预计生成时间约 1.5 分钟（模型已加载）
预计 4 个 WAV、约 0.4MB
```

---

## 4. 功能设计

### 4.1 结构化音色描述

`design init` 生成可编辑 YAML：

```yaml
schema_version: 1
name: dusk
language: zh
template: zh-late-night-radio/v1

identity:
  age: 30s
  gender_presentation: female
  timbre: [warm, clean, slightly_husky]
  pitch: low_mid
  articulation: clear, relaxed
  persona: late_night_host

delivery_baseline:
  pace: medium_slow
  energy: medium_low
  tone: calm, intimate

scene:
  proximity: close_mic
  room: dry_studio

avoid: [childlike, shouting]
```

编译器渲染成自然语言 instruction，并同时保存：

- `structured`：用户意图，便于 diff；
- `rendered_instruction`：真正发给模型的内容；
- `compiler_version`：模板升级后可追溯。

首批模板 4–6 个，中英各半。

### 4.2 `design lint`

本地执行，不联网、不加载模型。输出 **Instruction Readiness Score**（不是音色质量分）：

- 具体性：音高、音色、节奏、人物气质等可感知锚点；
- 可执行性：避免“高级”“自然”“有质感”空词；
- 一致性：是否要求互斥特征；
- 语言匹配：instruction 语言是否匹配目标文本；
- 可测试性：是否有足够信息设计试听脚本。

硬错误：空 instruction、instruction 中混入 `[笑]` / `(laugh)`、中英错配、把完整台词误填进 instruction。

### 4.3 候选生成

`design explore` 维度：

```text
instruction variant × seed × cfg_scale × cover script
```

默认 cfg：

- design：4.0；
- direction：4.0；
- clone：1.0。

每个候选完成即原子写入 WAV + sidecar；中断后 `--resume` 只补未完成项。

### 4.4 客观分析 + 试听卷（P0 差异化核心）

`design report` 输出：

- 每个 take 的指标：时长、F0 中位数/p10/p90/标准差、HNR、频谱重心、LTAS 斜率、2–6 kHz vs 100–500 Hz 能量比、削波比例、静音比例、异常短/长；
- Markdown / CSV 对比表；
- `all_candidates.wav`（默认间隔 1 秒）；
- 离线 `audition.html`，可盲听、可看指标、本地相对路径播放；
- 明确声明：指标用于**淘汰和排序参考**，不是审美裁判。

指标分层：

- `basic`：只依赖 numpy + soundfile（时长、削波、静音、异常值）；
- `full`：可选 `[metrics]` 依赖（F0 分布、HNR、频谱重心、LTAS 斜率、HF/LF 频段能量比）。

不做自动选优。最终选择由人决定。

### 4.5 冻结

```bash
btts design freeze RUN --take c007 --name dusk --as design
btts design freeze RUN --take c007 --name dusk --as clone --confirm-transcript
```

两种 profile：

| 类型 | 保存内容 | 复用方式 | 稳定性 |
|---|---|---|---|
| `design` | instruction + seed + cfg + preview | `tts_instruction` | 跨文本可能漂移，不承诺 |
| `clone` | ref.wav + ref.txt + hashes | `ref_clone_tata` | 跨文本稳定，推荐用于正式角色 |

规则：

- 默认 `--as design`，不会静默把生成音频变成 clone reference；
- `--as clone` 必须 `--confirm-transcript`，并展示原始生成文本供人工确认漏字/补字；
- take 必须完整、未截断、无内联事件；
- 参考音频建议 6–20 秒，低于 6 秒警告，超过 30 秒要求显式覆盖；
- 保存来源 run、take、WAV 哈希、instruction、seed、cfg、model digest。

### 4.6 `say` 三路由

| 入口 | 模型模板 | 默认 cfg |
|---|---|---:|
| `say --design FILE` | `tts_instruction` | 4.0 |
| `say --voice @clone` | `ref_clone_tata` | 1.0 |
| `say --voice @clone --direction TEXT` | `ref_edit_tata` | 4.0 |

`--play` 语义：生成完整 WAV → 原子落盘 → 成功后调用 macOS `afplay`。不做边生成边播放。播放失败保留文件并返回非零退出码。

### 4.7 模型下载与校验

首次使用必须先获得本地 checkpoint。下载只由显式命令触发：`doctor`、`design explore`、`say` 等命令永远不隐式联网，也不自动更新模型。

#### 4.7.1 命令与默认值

```text
btts model download [--source huggingface|modelscope] [--repo ID]
                        [--revision REV] [--to DIR] [--force] [--yes] [--json]
btts model verify [--model DIR] [--deep] [--json]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--source` | `huggingface` | 可选 `modelscope` |
| `--repo` | `BreezeBlue/breeze-tts-2` | 两个源使用同一 repo id |
| `--revision` | HF `main` / ModelScope `master` | 显式传入时两源都使用该 revision |
| `--to` | `$BTTS_MODEL_DIR`，否则 `./ms-models/Breeze-TTS-2` | 下载目标目录 |
| `--force` | 关闭 | 允许写入非空目录；只覆盖同名模型文件，不删除其他文件 |
| `--yes` | 关闭 | 跳过交互确认；非 TTY 环境必须提供 |
| `--json` | 关闭 | stdout 只输出一个 JSON 文档，进度与诊断写 stderr |

`model verify` 的模型目录解析与其他命令一致：`--model` > `$BTTS_MODEL_DIR` > `./ms-models/Breeze-TTS-2`。

#### 4.7.2 下载源

- **Hugging Face**：通过 `huggingface_hub.snapshot_download()` 下载；自动使用标准缓存逻辑、断点续传和 `HF_TOKEN`（只从环境读取，不写入任何文件或日志）。
- **ModelScope**：直接使用公开 HTTP API（文件列表 + 单文件下载），只依赖标准库 `urllib`，不要求安装 `modelscope` 包。逐文件使用 API 返回的 `Size` 与 `Sha256` 校验，支持 `Range` 断点续传。
- 两个源都是公开仓库，无需申请权限；下载即表示接受 BreezeBlue Research and Non-Commercial License 1.1。
- 同一 repo id 在两个源的目录结构一致，下载后都满足 `REQUIRED_MODEL_FILES` 与 `audio_tokenizer/` 布局。

#### 4.7.3 流程

1. 解析目标目录；若目录非空且不是可识别的未完成下载，要求 `--force`。目标目录及其任何候选子路径不得包含 symlink；解析后的最终路径必须仍位于 `target.resolve()` 内。
2. 确认前置条件：非 TTY 且没有 `--yes` 时立即返回 exit 2，**不发起网络请求**。
3. 从远端获取文件清单（路径、大小、哈希），打印许可证、来源、repo、revision、文件数、总大小（约 7.68GB）和目标目录。
4. TTY 下若未给 `--yes`，在打印清单后交互确认。
5. 检查磁盘剩余空间 ≥ 总大小的 1.05 倍；不足时 exit 2 并给出所需空间。
6. 逐文件下载到 `<name>.part`；存在同源 `.part` 时尝试 `Range` 续传；校验大小与 SHA-256 后 `os.replace` 到最终路径。
7. 全部完成后调用 `missing_model_files()`；缺文件或哈希不符返回 exit 5，不写完成标记。
8. 写入 `.btts-model.json` 完成标记，删除 incomplete 状态，清理 `.part`，打印摘要。

可识别的未完成下载：存在 `.btts-model.json` 或 `.btts-model.incomplete.json` 且 `source`/`repo`/`revision` 与本次一致，或目录内只有已知模型文件和状态/`.part` 文件。其他非空目录一律视为冲突。

#### 4.7.4 完成标记

`.btts-model.json` 放在模型目录根部，只记录可复现元数据，不含 token、私有 URL 或绝对路径：

```json
{
  "schema_version": 1,
  "source": "modelscope",
  "repo": "BreezeBlue/breeze-tts-2",
  "revision": "master",
  "downloaded_at": "2026-11-03T12:34:56Z",
  "files": {
    "config.json": {"size": 10161, "sha256": "57849eb7..."}
  }
}
```

开始写入或覆盖任何模型文件前，必须先写入 `.btts-model.incomplete.json`（`source`、`repo`、`revision`、`started_at`、可选 `error`），再原子删除旧完成标记。只有全部文件校验通过后才写入新完成标记，最后删除 incomplete。任何失败都保留 incomplete 状态：`doctor` 必须给出警告，`model verify`（浅层和 `--deep`）必须报告 `complete=false`，不能因为文件数量/大小齐全而误判。若有效完成标记与残留 incomplete 并存，以完成标记为准，`doctor` 给出 stale 状态提示。失败清理时，远端没有 SHA-256 且旧完成标记也无法证明其哈希的同尺寸文件必须删除，避免续传时把同尺寸错误文件当成已完成。incomplete 标记不含绝对路径、token 或私有 URL。

`doctor` 读取完成标记或 incomplete 状态并显示来源/revision/下载时间；若标记存在但 `missing_model_files()` 非空，或存在 incomplete 状态，给出“未完成下载，请重跑 model download”的警告。`doctor` 不联网。

#### 4.7.5 `model verify`

- 默认：检查必需文件、分片是否齐全且非空；若存在 incomplete 状态（且无有效完成标记），报告 `complete=false`；若有完成标记，再核对标记中的文件大小。
- `--deep`：重新计算所有模型文件的 SHA-256 并与完成标记比对；无标记且无 incomplete 状态时只做完整性检查。
- exit 0 完整；exit 5 缺失/哈希不符；exit 2 用法错误。
- `--json` 输出字段：`model_dir`、`complete`、`missing`、`files_checked`、`deep`、`digest`、`source`、`repo`、`revision`、`downloaded_at`、`warnings`。

#### 4.7.6 错误与退出码

| 情况 | 退出码 |
|---|---|
| 参数错误、非 TTY 缺 `--yes`、磁盘不足、目录冲突 | 2 |
| 网络、provider API、写入失败（统一包装为可读错误，无 traceback） | 3 |
| 下载后校验失败、`model verify` 不通过 | 5 |

#### 4.7.7 测试要求

- 单元测试不下载 7.7GB：HF 用 monkeypatch 的 `snapshot_download`，ModelScope 用本地 `http.server` 提供小型假仓库。
- 覆盖：成功下载并写标记、断点续传、SHA-256 不符、缺文件、非空目录拒绝/`--force`、非 TTY 缺 `--yes`、磁盘不足、`--json` stdout 严格可解析、`model` 命令不导入 torch。
- 安全：目标目录内目录 symlink、最终文件 symlink、目标本身为 symlink、目标路径中的 symlink loop；直接调用 `download_model()` 传入 `../`/绝对路径；`--force` 刷新失败后旧 marker 必须失效；incomplete 状态必须让 `doctor` 告警且浅层/深层 verify 均 `complete=false`；远端无 SHA-256 时失败留下的同尺寸文件必须删除或标记不可信；`mkdir`/写入/`os.replace` 的 `OSError` 统一映射到 exit 3。
- 至少一条针对真实 ModelScope/HF 文件列表 API 的在线测试，显式开关（如 `BTTS_RUN_DOWNLOAD=1`），默认 skip。

---

## 5. 数据模型

### 5.1 Voice profile

```text
<data-dir>/voices/dusk/
  profile.json
  preview.wav
  design.yaml          # design profile
  ref.wav              # clone profile
  ref.txt              # clone profile
  LICENSE.txt
```

`profile.json`：

```json
{
  "schema_version": 1,
  "voice_id": "v_01J6R3M8Q9K2",
  "name": "dusk",
  "kind": "design",
  "language": "zh",
  "identity": {
    "structured": {},
    "rendered_instruction": "一位三十多岁的女性……",
    "template": "zh-late-night-radio/v1",
    "compiler_version": "1"
  },
  "design": {
    "seed": 42,
    "cfg_scale": 4.0,
    "instruction": "一位三十多岁的女性……"
  },
  "reference": null,
  "defaults": { "cfg_scale": 4.0, "seed": 42, "direction": null },
  "provenance": {
    "source": "designed",
    "source_take_id": "c007",
    "source_session_id": "dusk-01",
    "model_digest": "sha256:...",
    "model_id": "BreezeBlue/breeze-tts-2",
    "api_protocol": "local-mps/v1",
    "cli_version": "0.1.0"
  },
  "license": { "model": "breezeblue-research-non-commercial" },
  "consent": null,
  "tags": ["narration", "intimate"]
}
```

`kind=clone` 时：

```json
{
  "kind": "clone",
  "design": null,
  "reference": {
    "audio": "ref.wav",
    "audio_sha256": "sha256:...",
    "transcript": "ref.txt",
    "transcript_sha256": "sha256:...",
    "transcript_confirmed": true,
    "duration_s": 12.4,
    "sample_rate": 24000
  },
  "defaults": { "cfg_scale": 1.0, "seed": 42, "direction": null }
}
```

设计理由：

- Breeze 没有可保存的 voice prompt，**clone 的 reference WAV + exact transcript 就是身份**；
- design profile 保存可重放配方，但不承诺跨文本稳定；
- `voice_id` 稳定、`name` 可变；
- `provenance` 不保存 token / endpoint / 绝对路径。

### 5.2 Recipe sidecar

每个输出音频旁写 `*.recipe.json`：

| 字段组 | 内容 |
|---|---|
| `schema_version`, `take_id`, `run_id` | 与 run / 候选关联 |
| `command` | `say` / `design.explore` |
| `input` | 原文、哈希、语言 |
| `operation` | `design` / `clone` / `direction`、instruction、cfg、seed |
| `voice` | profile ID、profile 哈希、ref WAV 哈希、ref text 哈希 |
| `model` | model ID、digest、api protocol、sample rate |
| `output` | 文件、格式、字节数、时长、SHA-256 |
| `timing` | 总耗时、RTF（如可获得） |
| `warnings` | 未知事件、过长文本、参数警告 |

不写 token、私有 URL、绝对路径。原子写入。

---

## 6. 推理代码 vendoring

### 6.1 已验证的最小集合

```text
src/btts_cli/_vendor/breeze_tts/
  LICENSE
  __init__.py
  breeze_infer/
    __init__.py
    runtime.py
    audio.py
    templates.py
  models/
    __init__.py
    breeze.py
    breeze_config.py
    breeze_base_config.py
    breeze_backbone_factory.py
    generation_breeze.py
    logits_process.py
    t5gemma2_compat.py
```

已实测：该集合可独立导入，并从 vendored 包在 MPS 上加载模型，耗时 7.2 秒。

### 6.2 不复制

| 路径 | 原因 |
|---|---|
| `breeze_infer/api.py` | FastAPI / CUDA 流式服务 |
| `breeze_infer/batch.py` | CUDA batch graph |
| `models/fast_streaming.py` | 构造时拒绝非 CUDA |
| `models/cudagraph/` | CUDA Graph |
| `models/stream_runtime/` | CUDA 流式底座 |
| `models/text_encoder_graph.py` | MPS 不启用 fast text encoder |
| `models/warmup_profile.py` | 只服务 CUDA fast path |
| `configs/fast.json` | MPS P0 不使用 |

`breeze.py` 内有条件导入 `t5_adapter` / `text_encoder_graph` 的分支。当前 checkpoint 配置为 `text_encoder_proj_type=linear`，不会进入。适配层必须在加载前检查配置，遇到 `breeze_text_encoder_adapter` 或 fast graph 开关时**直接报错**，不能等到运行中途 `ImportError`。

### 6.3 权重与 `qwen-tts`

- `audio_tokenizer/` 是 checkpoint 内的 651MB 模型数据，不是源码，不复制。
- 模型目录需要完整结构：

```text
Breeze-TTS-2/
  config.json
  generation_config.json
  tokenizer.json
  tokenizer_config.json
  model.safetensors.index.json
  model-00001-of-00002.safetensors
  model-00002-of-00002.safetensors
  audio_tokenizer/
    config.json
    model.safetensors
```

- 模型通过 `--model` / `BTTS_MODEL_DIR` 指定；`doctor` 可提示现有路径，但代码不依赖 `../breeze-tts`。
- 不 vendor `qwen_tts`，固定 `qwen-tts==0.1.1`，只通过薄适配层调用 `Qwen3TTSTokenizer.from_pretrained(checkpoint/audio_tokenizer)`。
- 仓库和 wheel 中不得出现 `*.safetensors`、`ms-models/`、`audio_tokenizer/`。

### 6.4 导入改写

不要把 `models/` 作为顶层包安装（会与通用名冲突）。所有上游绝对导入确定性改写：

```python
from models.breeze import ...          # 原
from btts_cli._vendor.breeze_tts.models.breeze import ...  # 改写后

from breeze_infer.runtime import ...   # 原
from btts_cli._vendor.breeze_tts.breeze_infer.runtime import ...  # 改写后
```

### 6.5 Apache-2.0 合规

- 根目录 `LICENSE`；
- `src/btts_cli/_vendor/breeze_tts/LICENSE`；
- `THIRD_PARTY_NOTICES.md`：上游 URL、commit SHA、复制文件清单、import 改写、其他行为修改、许可证位置；
- 每个 vendor 文件保留原版权头，并加一行：

```text
Vendored from breezeblue-ai/breeze-tts at <sha>.
Modified: package-qualified imports for btts_cli.
```

- 若上游存在 `NOTICE`，一并复制；当前未见。
- wheel 的 `license-files` 包含根 LICENSE、THIRD_PARTY_NOTICES、vendor LICENSE。

### 6.6 同步机制

```text
vendor.lock.json
scripts/sync_breeze_vendor.py
```

`vendor.lock.json` 记录：

```json
{
  "upstream": "https://github.com/breezeblue-ai/breeze-tts",
  "ref": "daa6cb2efe5471f09369229f322d20bb956cb3c3",
  "files": {
    "breeze_infer/runtime.py": {
      "source_sha256": "...",
      "vendored_sha256": "..."
    }
  }
}
```

流程：

```bash
python scripts/sync_breeze_vendor.py --ref <commit>
pytest
# 真实 MPS smoke
git diff
```

脚本只复制白名单、执行同一套 import 改写、更新 hash 和 notices。不自动追 `main`。

---

## 7. MPS 运行模型

### 7.1 设备

- 自动设备：MPS 可用 → `mps`；否则明确报错，**不静默回退 CPU**。
- `--device cpu` 仅作显式调试选项。
- import torch 前设置 `PYTORCH_ENABLE_MPS_FALLBACK=1`。
- `doctor` 报告：MPS 可用性、模型路径、模型文件完整、可用内存、许可证。
- 16GB 内存机器要警告：模型占用约 8GB，系统和其他应用可能吃紧。

### 7.2 不常驻 worker

实测冷加载 8.6 秒，P0 采用：

- 每个命令进程加载一次模型；
- `design explore` 一次加载，生成所有候选；
- `say` 单次加载约 8–10 秒，可接受；
- P1 再加 `worker start/status/stop`，用 Unix socket + JSONL，worker 直接原子写 WAV，避免大块音频走 RPC。

### 7.3 进度与中断

```text
generating 1/4 ... 32%
phase: decode audio
ETA: 24s
```

- Ctrl-C 在当前候选完成后停止；
- 已完成候选和 sidecar 保留；
- `--resume` 续跑；
- 不显示“首帧延迟”或“streaming”。

---

## 8. 仓库结构与依赖

```text
breeze-tts-cli/
├── pyproject.toml
├── README.md
├── LICENSE
├── THIRD_PARTY_NOTICES.md
├── vendor.lock.json
├── PRD.md
├── scripts/
│   └── sync_breeze_vendor.py
├── src/btts_cli/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── commands/
│   │   ├── doctor.py
│   │   ├── design.py
│   │   ├── model.py
│   │   ├── say.py
│   │   └── voice.py
│   ├── engine/
│   │   ├── base.py
│   │   └── mps.py
│   ├── domain/
│   │   ├── design.py
│   │   ├── model.py
│   │   ├── recipe.py
│   │   ├── run.py
│   │   └── voice.py
│   ├── audio/
│   │   ├── io.py
│   │   ├── hash.py
│   │   ├── metrics.py
│   │   └── play.py
│   ├── output/
│   │   ├── errors.py
│   │   └── json.py
│   ├── resources/
│   │   ├── design_templates.yaml
│   │   └── audition.html.j2
│   └── _vendor/breeze_tts/
│       ├── LICENSE
│       ├── breeze_infer/
│       └── models/
└── tests/
    ├── fake_engine.py
    ├── test_cli.py
    ├── test_design.py
    ├── test_freeze.py
    ├── test_model.py
    ├── test_say_routes.py
    ├── test_resume.py
    └── test_wheel_contents.py
```

### 8.1 CLI 与基础依赖

- `click`：嵌套命令、completion、参数校验、可测试性；
- `pydantic>=2`：profile / recipe / lock schema；
- `PyYAML`：design YAML；
- `rich`：进度和 ETA；
- `platformdirs`、`filelock`；
- `soundfile`、`numpy`；
- `Jinja2`：离线 audition HTML；
- `huggingface-hub`：Hugging Face 下载与断点续传。纯 Python，不引入 torch；
- ModelScope 下载只用标准库 `urllib`，不新增 `modelscope` 依赖。

不把 torch / transformers / qwen-tts 放进基础依赖。

### 8.2 pyproject

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "breeze-tts-cli"
version = "0.1.0"
requires-python = ">=3.10,<3.13"
dependencies = [
  "click>=8.1,<9",
  "pydantic>=2.8,<3",
  "PyYAML>=6,<7",
  "rich>=13.7,<15",
  "platformdirs>=4,<5",
  "filelock>=3.15,<4",
  "soundfile>=0.13",
  "numpy>=2,<3",
  "Jinja2>=3.1,<4",
]

[project.optional-dependencies]
mps = [
  "torch==2.9.1",
  "torchaudio==2.9.1",
  "transformers==4.57.3",
  "qwen-tts==0.1.1",
  "huggingface-hub>=0.34,<1",
]
metrics = [
  "librosa>=0.10",
  "praat-parselmouth>=0.4",
]
dev = [
  "pytest>=8",
  "pytest-cov>=5",
  "ruff>=0.12",
  "build>=1.2",
  "mypy>=1.11",
]

[project.scripts]
btts = "btts_cli.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/btts_cli"]
```

### 8.3 wheel 内容检查

`tests/test_wheel_contents.py` 解压 wheel，断言：

- 没有 `*.safetensors`；
- 没有 `audio_tokenizer/`；
- 没有 `ms-models/`；
- 包含根 LICENSE、THIRD_PARTY_NOTICES、vendor LICENSE；
- 基础安装（未装 torch）可执行 `btts --help`。

---

## 9. P0 验收标准

1. `pip install -e '.[mps]'` 成功；基础安装未装 torch 时 `btts --help` 可用。
2. wheel 中无权重、无 `audio_tokenizer/`、无 `ms-models/`；许可证文件齐全。
3. `doctor --bench` 检测 MPS，记录模型加载时间、一次短生成 RTF、MPS 内存；MPS 不可用时明确失败，不静默用 CPU。
4. vendored 推理包可独立导入并加载模型（已实测 7.2 秒）。
5. `design init` 产出可编辑 YAML；`design lint` 对缺字段、冲突描述、空泛描述给出 block/warn，离线且确定性。
6. 默认 `design explore` 只生成 4 个候选；每个 WAV 有 sidecar；中断后 `--resume` 不重做已完成且 hash 正确的候选。
7. `design report` 输出 CSV、Markdown 表、`all_candidates.wav` 和离线 HTML；指标包含时长、削波、静音，`full` 模式包含 F0 分布、HNR、频谱重心、LTAS 斜率和 HF/LF 频段能量比。
8. `design freeze --as design` 生成 design profile；`--as clone --confirm-transcript` 生成 clone profile；缺失确认、hash 不匹配、take 不完整时失败。
9. `say --design`、`say --voice @clone`、`say --voice @clone --direction` 三条路径都输出 24kHz mono WAV 和 recipe sidecar，cfg 默认分别为 4.0 / 1.0 / 4.0。
10. clone 路由缺少精确 `ref_text` 或未确认时拒绝执行；不自动转写。
11. `--play` 只在完整 WAV 落盘后调用 `afplay`；播放失败返回非零退出码，不删除文件。
12. 中文和英文各至少一个 smoke case 通过；括号/方括号事件文本原样进入模型输入。
13. 使用 fake engine 的单元测试不依赖 MPS；至少一项真实 MPS 集成测试覆盖模型加载和三条路由。
14. sidecar / profile / manifest 不出现 token、私有 URL、绝对路径；所有输出采用临时文件 + rename。
15. README、`doctor`、`voice add` 明确提示模型与自托管输出的非商业许可。
16. `model download --source huggingface|modelscope` 能把公开 checkpoint 下载到目标目录，逐文件校验 SHA-256，写 `.btts-model.json`，并让 `doctor` / `model verify` 通过。
17. 下载前显示来源、repo、revision、文件数和总大小；非 TTY 缺少 `--yes` 返回 exit 2 且不发起网络请求；磁盘不足、非空冲突目录返回 exit 2。
18. 中断后重跑可断点续传；大小或 SHA-256 不符返回 exit 5；失败后 `.btts-model.incomplete.json` 使 `doctor` 告警且 verify 不通过，成功后才删除；`--force` 不删除目标目录中的无关文件。
19. `model verify --json` 与 `model download --json` 的成功路径 stdout 都可被严格 JSON 解析；失败时 stdout 保持为空或单一 JSON，错误只写 stderr；`model` 命令不导入 torch。
20. HF 下载使用 `huggingface-hub`；ModelScope 下载不要求安装 `modelscope` 包。

---

## 10. P1 / P2

### P1

- `worker start|status|stop`：常驻模型，Unix socket，idle timeout 30 分钟；
- 远程 HTTP 后端（`breeze_infer.api`）；
- `design audition` 交互式盲听、偏好记录、胜率 / Bradley-Terry；
- `design stability`：同一 take 换多文本，测 F0 / 频谱重心漂移；
- `design shortlist`：客观淘汰，不做审美裁判；
- `voice preview/rm/export/import`；
- `direction lint/explore`；
- `text lint`；
- `batch plan/run`；
- `project init/lock/verify`；
- `replay`；
- `cache stats/prune`；
- `--out -` 与流式 PCM。

### P2

- `design rewrite --advisor URL`（默认关闭）；
- `voice publish`；
- `bench`；
- `evaluator add/list`；
- `project render`。

---

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 16GB 内存机器 | 8GB 模型 + 系统可能吃紧 | `doctor` 报告可用内存并警告；不承诺 16GB 可用 |
| MPS 算子回退 | 部分 op 走 CPU，速度波动 | 设置 `PYTORCH_ENABLE_MPS_FALLBACK=1`；真实 smoke 覆盖 |
| 非商业许可 | 不能直接服务商业配音 | 全链路提示；定位研究/评估/原型 |
| design 跨文本漂移 | 用户误以为 instruction 永久代表音色 | design profile 不承诺稳定；推荐 `--as clone` |
| 指标被误当审美裁判 | 选出技术指标好但听感差的 take | 指标只用于淘汰/排序；最终由人决定 |
| 上游代码漂移 | vendored 代码与上游分叉 | `vendor.lock.json` + 同步脚本 + 显式升级 |
| 生成音频转写不精确 | clone 参考文本与音频不一致 | `--as clone` 必须人工确认；P1 可选 ASR 辅助 |
| 模型下载网络中断/镜像不可用 | 首次使用被阻塞 | 逐文件 `.part` + Range 续传；HF/ModelScope 双源；错误 exit 3 并给出可重试提示 |
| 下载 7.7GB 占满磁盘 | 下载失败或系统异常 | 下载前检查剩余空间（≥1.05 倍），不足直接 exit 2 |
| 下载内容与许可被误解 | 用户误以为可商用 | 下载前打印来源、大小与非商业许可；确认后才联网 |
| 镜像文件被替换/损坏 | 加载失败或行为异常 | ModelScope 逐文件 `Sha256`、HF `huggingface-hub` 校验；`model verify --deep` 可重算 |

---

## 12. 下一步

P0 主体已完成并通过 Codex 终审（见 `IMPLEMENTATION_NOTES.md`）。v0.3 追加：

1. 实现 `model verify`（离线完整性 + 可选 `--deep` 哈希）；
2. 实现 `model download --source huggingface`（`huggingface-hub`，断点续传）；
3. 实现 `model download --source modelscope`（标准库 HTTP，逐文件 SHA-256）；
4. 接入 `doctor` 的下载来源与未完成下载提示；
5. fake provider / 本地 HTTP 单测 + 受限在线测试，再由 Codex 复审后提交。
