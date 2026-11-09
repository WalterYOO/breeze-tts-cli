# breeze-tts-cli（btts）PRD v0.4

> 状态：P0 已实现（`fb48022`）；v0.3 把模型下载纳入 P0；v0.4 补齐 v0.2.2–v0.2.5 落地的音色管理（list/filter/rename/tag/delete/export）与 Shell 补全，并同步 explore 默认 seed、report 推荐和 doctor 指标后端的行为。
> 模型：Breeze-TTS-2，本地权重 `../breeze-tts/ms-models/Breeze-TTS-2`（7.2GB）。
> 上游代码基线：`breezeblue-ai/breeze-tts` @ `daa6cb2efe5471f09369229f322d20bb956cb3c3`。
> 参考同类工具：`../qwen-tts-cli`（借鉴模式，不照搬功能）。

---

## 0. 已锁定的决策

| 决策 | 结论 | 限定条件 |
|---|---|---|
| 运行环境 | 以 macOS MPS 为主 | 定位为**离线 / 近实时制作**，不承诺流式、不承诺实时对话 |
| 推理代码 | 复制进本仓库，不依赖 `../breeze-tts` 路径 | 只 vendor MPS 必需源码；权重与 `qwen-tts` 不进仓库 |
| P0 范围 | 设计工作台 + freeze + say 三路由 + 模型下载/校验 + 音色管理/导出 + Shell 补全 | 砍掉远程后端、批处理、项目锁、自动评分、后台任务系统 |
| 模型获取 | `model download` 支持 Hugging Face 与 ModelScope；`model verify` 离线校验 | 不自动下载、不后台更新；必须显式确认并接受非商业许可 |
| 音色管理 | `list/filter/rename/tag/delete/export` 归入 P0，全部走 store 锁与 no-follow 校验 | 删除必须 TTY 确认或显式 `--yes`；导出永不覆盖已有目标 |
| Shell 补全 | `btts completion` 作为独立顶层命令，动态补全本地音色名 | 只写专用补全文件，绝不修改用户 rc；只删除自己生成的文件 |

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
→ voice add / list / filter / show → rename / tag / export
→ say（design / clone / direction 三路由）
```

`btts completion` 是随时可用的外围命令，不参与主闭环。

### 3.2 P0 命令

```text
btts model download [--source huggingface|modelscope] [--repo ID]
                        [--revision REV] [--to DIR] [--force] [--yes] [--json]
btts model verify [--model DIR] [--deep] [--json]

btts doctor [--bench] [--json]

btts design init FILE [--template T] [--name NAME] [--language zh|en] [--force]
btts design lint FILE [--show-rendered]
btts design explore FILE --out RUN_DIR [--resume] [--wide]
                        [--seeds N,N] [--cfgs N,N] [--yes]
                        [--max-new-tokens N] [--model DIR] [--device mps|cpu]
btts design report RUN_DIR [--metrics basic|full] [--csv/--no-csv]
                        [--html/--no-html] [--reel/--no-reel] [--gap SEC]
btts design freeze RUN_DIR --take ID --name NAME [--as design|clone] [--confirm-transcript]

btts voice add NAME --ref WAV --text FILE --confirm-transcript
                    [--language zh|en] [--tag TAG]... [--allow-long-ref] [--force]
btts voice list [QUERY] [--match fuzzy|prefix|exact] [--tag TAG]...
                [--kind design|clone] [--language zh|en]
                [--names-only] [--json]
btts voice filter [QUERY] [--match fuzzy|prefix|exact] [--tag TAG]...
                  [--kind design|clone] [--language zh|en]
                  [--names-only] [--json]
btts voice show NAME
btts voice rename OLD NEW
btts voice tag NAME [--add TAG]... [--remove TAG]... [--set TAG]... [--clear]
btts voice delete NAME [--yes]
btts voice export NAME... [--mode full|wav] [-o PATH] [--zip] [--json]
btts voice export --all   [--mode full|wav] [-o PATH] [--zip] [--json]

btts completion [SHELL] [--install|--uninstall] [--dir DIR]

btts say TEXT --design FILE [--seed N] [--cfg N] [--out FILE] [--play]
btts say TEXT --voice @NAME [--out FILE] [--play]
btts say TEXT --voice @NAME --direction TEXT [--cfg N] [--out FILE] [--play]
```

### 3.3 P0 明确砍掉

- 远程 HTTP 后端、鉴权、远程任务提交；
- 模型自动更新、后台下载、多镜像测速切换、模型删除/缓存 GC；
- `breeze_infer.api`、batch API、CUDA fast path；
- 常驻 worker、后台 job 系统（P1）；
- 项目锁、`.bvoice` 格式与 `voice import`、`voice publish`（`voice export` 已实现，见 §4.8）；
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
| 默认 | 2 seed × 2 cfg = **4 个候选** | 从 100 以内的 25 个质数中随机抽 2 个 seed；`cfg=2.5,4.0` |
| `--wide` | **8 个候选** | 2 seed × 2 cfg × 2 试听脚本 |
| 显式 | 16+ | 必须手动指定，并在开始前列出预计耗时、写盘大小，必要时要求 `--yes` |

抽中的 seed 写入 `run.json` 的 `matrix.seeds`，并随每个 take 的 recipe 落盘，因此 take 本身仍可复现；`--resume` 复用已记录的 seed 而不是重新抽。需要固定矩阵时显式传 `--seeds`。单 seed 默认（`voice add`、`say --design`、`doctor --bench`）统一使用 `19`。

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
- `all_candidates.wav`（默认间隔 1 秒，`--gap` 可调）；
- 离线 `audition.html`，可盲听、可看指标、本地相对路径播放；
- 确定性推荐（metric pick）：先标记技术上失败的 take（异常短/长、截断、削波比例 > 0.1%、静音比例 > 60%），其余按技术标记数、削波、静音升序，再按 HNR、voiced 比例降序，最后以 take ID 稳定排序；页面默认选中推荐项，盲听模式不暴露推荐与选中信息；可复制 take ID、shell 转义后的 freeze 命令和试听文本；
- `report.md` 同时输出 Recommendation 小节与每个 take 的技术 flags；
- 无法计算的频谱指标留空并在报告中给出警告，不写入 NaN/Inf；
- 明确声明：指标用于**淘汰和排序参考**，不是审美裁判。

指标分层：

- `basic`：只依赖 numpy + soundfile（时长、削波、静音、异常值）；
- `full`：可选 `[metrics]` 依赖（F0 分布、HNR、频谱重心、LTAS 斜率、HF/LF 频段能量比）。

推荐只用于默认选中和排序，不会自动写 profile、也不会自动执行 freeze。最终选择由人决定。

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

### 4.8 音色管理

#### 4.8.1 `voice list` / `voice filter`

- `QUERY` 只匹配可寻址的目录名（满足 `NAME_RE`），大小写不敏感；开头的 `@` 会被去掉；空查询报 usage error。
- `--match fuzzy|prefix|exact`，默认 `fuzzy`（子串）、`prefix`（前缀）、`exact`（全名相等）。
- `--tag` 可重复，语义为 AND：必须同时具备所有给定 tag；tag 按写入时的同一规则归一化。
- `--kind`、`--language` 过滤结构化字段；所有条件 AND。
- 无匹配是成功的空结果（exit 0），不报错。
- `--names-only` 每行一个可寻址、非 symlink 的音色名，供管道使用；`--json` 输出单个 JSON 文档；两者互斥（exit 2）。
- `voice filter` 与 `voice list` 完全等价，是同一实现的别名。
- 扫描容错：tombstone、目录名与 `profile.name` 不一致、不可寻址目录名、symlink 音色都会给出警告；目录名不一致时附带 `voice rename` 修复命令。`--json` 的 `warnings` 字段同时包含这些警告。

`--json` 结构：

```json
{
  "schema_version": 1,
  "kind": "btts-voice-list",
  "query": "girl",
  "match": "fuzzy",
  "filters": {"tags": ["calm"], "kind": "clone", "language": "zh"},
  "count": 1,
  "voices": [
    {
      "name": "girl-01",
      "kind": "clone",
      "language": "zh",
      "voice_id": "v_01J6R3M8Q9K2",
      "cfg_scale": 1.0,
      "tags": ["calm"]
    }
  ],
  "warnings": []
}
```

#### 4.8.2 `rename` / `tag` / `delete`

三个命令都要求音色名通过 `validate_name`，并持有 store 锁；POSIX 上通过 `O_DIRECTORY|O_NOFOLLOW` 的目录 fd 操作，路径在校验后被替换为 symlink 也无法把写入/删除引到 store 外。

- `rename OLD NEW`
  - 永不覆盖已有目标；目标与源只差大小写（大小写不敏感文件系统）时拒绝，并提示用中间名分两步完成。
  - 崩溃安全顺序：先在旧目录内原子改写 `profile.json` 的 `name`，再对目录做单次同文件系统 `os.rename`。中间崩溃会留下“目录名 OLD、profile 名 NEW”，重跑同一命令即可修复；不会出现任何命令都找不到的半改名状态。
  - 目录 rename 失败时恢复原 profile 内容再报错。
- `tag NAME`
  - 不带任何操作 flag 时只列出当前 tags。
  - `--add` / `--remove` 可重复；删除不存在的 tag 被忽略；`--set` 替换全部 tags；`--clear` 清空后可继续 `--add`。
  - `--set` 不能与 `--add`/`--remove` 同用；`--clear` 不能与 `--set`/`--remove` 同用（exit 2）。
  - 只原子改写 `tags` 字段，其余字段原样保留；legacy 未归一化 tag 在更新时收敛。
- `delete NAME`
  - 交互式终端询问确认，默认 `No`；非交互会话必须传 `--yes`，否则 exit 2 且不改动 store。
  - 先把目录原子移动到 `.deleting-<name>-<hex>` tombstone，再删除内容；中断留下的 tombstone 会被 `voice list` 以警告形式报告，可用 `--names-only` 取得干净输出。
  - 拒绝 symlink 目录、拒绝删除 store 根目录；删除失败时报告残留 tombstone 路径。

#### 4.8.3 `voice export`

```text
btts voice export NAME... [--mode full|wav] [-o PATH] [--zip] [--json]
btts voice export --all   [--mode full|wav] [-o PATH] [--zip] [--json]
```

- 选择：显式 `NAME...` 与 `--all` 二选一；名字开头的 `@` 会去掉，重复名字去重。
- `--mode full`（默认）：导出每个音色的完整目录，包括 `profile.json`、`LICENSE.txt`、`preview.wav`，以及 design 的 `design.yaml` 或 clone 的 `ref.wav` / `ref.txt` 和目录内其他常规文件；缺 `profile.json` 或 `LICENSE.txt` 视为完整性错误。
- `--mode wav`：每个音色只导出 `<name>.wav`（来自 `preview.wav`），不含 profile、参考音频或转写。
- 根目录固定包含 `LICENSE.txt`、`README.txt` 与 `manifest.json`；`manifest.json` 记录每个文件的 SHA-256 和字节数，以及每个音色的 name、voice_id、kind、language、tags、profile 哈希。
- 默认目标：当前目录下的 `btts-voices-<UTC 时间戳>`；`--zip` 时后缀为 `.zip`，归档内只有一个以归档文件名（去 `.zip`）命名的顶层目录。
- 目标已存在一律拒绝；目标解析后位于 voice store 内也拒绝；`--zip` 目标不是 `.zip` 后缀直接 exit 2。
- 发布方式：先写入隐藏的临时目录/临时 zip，全部成功后再用 no-replace rename 发布；任何失败都丢弃临时结果，不留下半成品。
- 校验先于落盘：任一选中音色缺失、含 symlink、profile 与目录名不一致、`preview`/参考音频/转写哈希不符、`--all` 遇到无法严格校验的条目，或名字在大小写不敏感文件系统上冲突，都在写目标前失败（usage 2 / integrity 5）。
- `--all` 对 tombstone 和非音色目录跳过并警告，但不会静默漏掉一个坏音色。
- `full` 导出可能包含参考音频、逐字转写和 profile 元数据，命令必须提示隐私与授权；`--json` 的 stdout 只输出单个 JSON 文档。

### 4.9 Shell 补全

```text
btts completion [SHELL] [--install|--uninstall] [--dir DIR]
```

- 支持 `bash`、`zsh`、`fish`、`powershell`（PowerShell 与实际 Click 版本相关，未实现时不列出）；省略 `SHELL` 时从 `$SHELL` 推断，无法推断则 exit 2 并列出所有支持项。
- 默认把补全脚本写到 stdout；`--install` 写入各 shell 的专用补全文件；`--uninstall` 只删除带 btts 标记的文件。命令永远不会修改 `.bashrc`、`.zshrc` 或 `config.fish`。
- 默认安装路径：
  - bash：`$XDG_DATA_HOME/bash-completion/completions/btts`（默认 `~/.local/share`）
  - zsh：存在 Oh My Zsh 时写到 `$ZSH_CUSTOM/completions/_btts`（默认 `~/.oh-my-zsh/custom/completions/_btts`，该目录通常已在 `$fpath`）；否则 `~/.zfunc/_btts`
  - fish：`$XDG_CONFIG_HOME/fish/completions/btts.fish`（默认 `~/.config`）
  - `--dir` 覆盖安装目录，且只能与 `--install`/`--uninstall` 同用。
- 动态补全：TAB 时回调 `btts`，从本地 store 补全音色名，覆盖 `voice show/export/tag/delete`、`voice rename OLD`、`voice list|filter` 的 `QUERY` 和 `say --voice`。`say --voice` 固定补 `@NAME`；其余命令跟随用户输入（输入 `@` 才补 `@NAME`）。
- 只补全可寻址、非 symlink 的音色；store 损坏或读取失败时静默返回空候选，不向 shell 的 stdout 写错误信息。
- `--install` 幂等：内容相同打印 `up to date`；目标已存在但不是 btts 生成的文件、或目标本身是 symlink 时拒绝（exit 2）；写入使用临时文件 + 原子 rename。zsh 在 Oh My Zsh 环境下提示目录已在 `$fpath`，无需修改 rc。
- `--uninstall` 对未安装的目标打印 `not installed`；拒绝删除不是 btts 生成的文件或 symlink。
- PowerShell 不支持 `--install`/`--uninstall`，明确报错并提示手动追加到 `$PROFILE`。

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
    "seed": 19,
    "cfg_scale": 4.0,
    "instruction": "一位三十多岁的女性……"
  },
  "reference": null,
  "defaults": { "cfg_scale": 4.0, "seed": 19, "direction": null },
  "provenance": {
    "source": "designed",
    "source_take_id": "c007",
    "source_session_id": "dusk-01",
    "model_digest": "sha256:...",
    "model_id": "BreezeBlue/breeze-tts-2",
    "api_protocol": "local-mps/v1",
    "cli_version": "0.2.5"
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
  "defaults": { "cfg_scale": 1.0, "seed": 19, "direction": null }
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

### 5.3 Export bundle

```text
btts-voices-<stamp>/
  LICENSE.txt
  README.txt
  manifest.json
  <voice>/                  # --mode full
    profile.json
    LICENSE.txt
    preview.wav
    design.yaml             # design profile
    ref.wav / ref.txt       # clone profile
  <voice>.wav               # --mode wav
```

`manifest.json`：

```json
{
  "schema_version": 1,
  "kind": "btts-voice-export",
  "exported_at": "2026-11-09T12:34:56Z",
  "cli_version": "0.2.5",
  "mode": "full",
  "license": {
    "model": "breezeblue-research-non-commercial",
    "cli": "Apache-2.0",
    "notice_file": "LICENSE.txt"
  },
  "root_files": [
    {"path": "LICENSE.txt", "sha256": "sha256:...", "bytes": 1024}
  ],
  "voices": [
    {
      "name": "dusk",
      "voice_id": "v_01J6R3M8Q9K2",
      "kind": "design",
      "language": "zh",
      "tags": ["narration"],
      "profile_sha256": "sha256:...",
      "files": [
        {"path": "dusk/profile.json", "sha256": "sha256:...", "bytes": 2048}
      ]
    }
  ],
  "warnings": []
}
```

规则：

- 每个导出文件在写入时重算 SHA-256；凡 profile 中有预期值的文件（`profile.json`、clone 参考音频/转写、`preview.wav`）再与预期值比对，任何不一致都视为 integrity failure。
- `profile_sha256` 覆盖原始 `profile.json` 字节；clone 的参考音频/转写哈希来自 profile 并再次校验。
- 归档内路径统一使用 `/`，不写绝对路径、token 或私有 URL。
- `--mode wav` 仍保留音色元数据与 profile 哈希，但不包含 profile 文件本身。

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
- `doctor` 报告：MPS 可用性、模型路径、模型文件完整、下载来源/状态、可用内存、许可证，以及指标后端等级（`basic` / `partial` / `full`，含 librosa 与 praat-parselmouth 版本或缺失提示）。
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
│   │   ├── _complete.py       # 动态音色名补全
│   │   ├── _common.py
│   │   ├── completion.py
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
│   │   ├── voice.py
│   │   ├── voice_export.py
│   │   └── voice_filter.py
│   ├── audio/
│   │   ├── io.py
│   │   ├── hash.py
│   │   ├── metrics.py
│   │   ├── recommend.py
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
├── skills/btts-cli/          # Agent skill：命令参考与工作流
│   ├── SKILL.md
│   └── references/
└── tests/
    ├── fake_engine.py
    ├── test_cli.py
    ├── test_completion.py
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
- ModelScope 下载只用标准库 `urllib`，不新增 `modelscope` 依赖；
- Shell 补全基于 `click.shell_completion`，不新增依赖；
- 音色导出使用标准库 `zipfile` / `hashlib` / `tempfile`，推荐排序只用 `audio/recommend.py` 的纯函数，不新增依赖。

不把 torch / transformers / qwen-tts 放进基础依赖。

### 8.2 pyproject

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "breeze-tts-cli"
version = "0.2.5"
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
6. 默认 `design explore` 只生成 4 个候选（随机 2 个 100 以内质数 seed × 2 cfg）；每个 WAV 有 sidecar；中断后 `--resume` 不重做已完成且 hash 正确的候选，并复用 `run.json` 记录的 seed。
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
21. `voice list` / `voice filter` 支持 `fuzzy`/`prefix`/`exact`、多个 `--tag` 的 AND 语义、`--kind`、`--language`；`--names-only` 每行一个可寻址名字，`--json` 可严格解析，两者互斥时 exit 2；无匹配 exit 0。
22. `voice rename` 永不覆盖；目标与源仅大小写不同时给出中间名提示；模拟“profile 已改、目录未改”的中断后重跑同一命令可修复。`voice tag` 的 `--set`/`--clear` 互斥规则正确，且只改写 tags。`voice delete` 在非交互会话缺少 `--yes` 时 exit 2 且不删除；残留 tombstone 会被 `voice list` 警告。
23. `voice export` 的 full/wav 布局与 `manifest.json` 哈希正确；目标已存在、目标位于 store 内、缺文件、哈希不符、symlink、大小写冲突都在写盘前失败且不留半成品；`--zip` 归档只有一个顶层目录；`--json` stdout 可严格解析。
24. `btts completion` 对 bash/zsh/fish（以及当前 Click 支持的 powershell）输出脚本；省略 `SHELL` 时按 `$SHELL` 推断；动态补全覆盖 `voice show/export/tag/delete`、`voice rename OLD`、`voice list|filter QUERY` 和 `say --voice`，并正确镜像 `@` 前缀。
25. `completion --install` 幂等；拒绝覆盖非 btts 生成的文件与 symlink；`--uninstall` 只删除带标记的文件；任何路径都不修改 rc；`--dir` 只能与 `--install`/`--uninstall` 同用；zsh 在 Oh My Zsh 环境下安装到 `$ZSH_CUSTOM/completions` 并提示无需改 rc。
26. `doctor` 在 `basic`/`partial`/`full` 三种指标后端状态下给出正确等级与缺失提示；`design explore` 未给 `--seeds` 时抽取 2 个 100 以内质数并写入 `run.json`，`--resume` 复用；`design report` 的推荐排序确定、默认选中但不自动 freeze，盲听模式不暴露推荐。

---

## 10. P1 / P2

### P1

- `worker start|status|stop`：常驻模型，Unix socket，idle timeout 30 分钟；
- 远程 HTTP 后端（`breeze_infer.api`）；
- `design audition` 交互式盲听、偏好记录、胜率 / Bradley-Terry；
- `design stability`：同一 take 换多文本，测 F0 / 频谱重心漂移；
- `design shortlist`：客观淘汰，不做审美裁判；
- `voice preview/import`；
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
| full 导出包含参考音频/逐字转写 | 隐私或授权泄露 | `full` 模式显式打印警告；`wav` 模式不含 profile；导出前严格校验来源一致性 |
| 补全脚本写入用户目录 | 覆盖用户已有文件 | 只覆盖带 btts 标记的文件；拒绝 symlink；不修改 rc；安装幂等 |
| rename/delete 中断 | 目录名与 profile 名不一致或残留 tombstone | 重跑同一命令可修复；`voice list` / `show` / `export` 给出修复提示或 tombstone 警告 |

---

## 12. 版本与下一步

v0.4 对应包版本 `0.2.5`。P0 主体、模型下载、音色管理与 Shell 补全均已实现。

后续按 §10 P1 推进：

1. 补一条真实 shell 冒烟（zsh / fish），覆盖 Oh My Zsh 与非 OMZ 两种安装路径。
2. `voice import` 与 `voice preview`，与 export bundle 的 `manifest.json` 对齐。
3. `worker start|status|stop`（Unix socket + JSONL，避免大块音频走 RPC）。
4. `design stability` 与 `design shortlist`：漂移检测和客观淘汰，不做审美裁判。
5. `direction lint/explore`、`batch plan/run`、`replay`。
