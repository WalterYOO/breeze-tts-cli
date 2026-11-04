# btts 命令参考

`btts` 是入口；在未激活虚拟环境时用 `.venv/bin/btts` 或 `uv run btts`。

## 全局约定

- 模型目录优先级（对接受 `--model` 的命令）：`--model DIR` > `$BTTS_MODEL_DIR`
  > `./ms-models/Breeze-TTS-2`。`model download` 用 `--to DIR` 指定目标。
- 设备：`--device mps|cpu`，只适用于需要加载模型的 `doctor --bench`、`design explore`、`say`；
  默认优先 MPS。
- 音色库目录：`$BTTS_DATA_DIR/voices`，否则平台用户数据目录
  （macOS: `~/Library/Application Support/btts_cli/voices`）。
- `--json` 只出现在 `doctor`、`model download`、`model verify`；其余命令输出给人看。
- 退出码：1 通用 / lint blocked，2 用法，3 引擎，4 播放，5 完整性，130 中断。
- 除 `model download` 外，任何命令都不会联网或自动更新模型。

## btts doctor

```bash
btts doctor [--bench] [--json] [--model DIR] [--device mps|cpu]
```

检查 CLI 版本、Python、平台、模型目录（存在性、缺失文件、摘要、下载标记）、
torch/MPS、内存和许可证。`--bench` 会真正加载模型并生成一句短音频（慢）。
模型缺失本身不会导致非零退出；失败主要来自 `--bench` 的加载/生成（退出码 3）。
有 `--json` 时 stdout 只有一个 JSON 文档，第三方诊断转到 stderr。

## btts model download

```bash
btts model download [--source huggingface|modelscope] [--repo ID] [--revision REV]
                    [--to DIR] [--force] [--yes] [--json]
```

显式下载或续传 checkpoint，默认 repo `BreezeBlue/breeze-tts-2`（约 7.7 GB）。
下载机制按 source 不同：Hugging Face 委托 `snapshot_download`（沿用其缓存/续传语义）；
ModelScope 使用 `.part` + HTTP Range 续传，逐文件大小校验后原子替换。
所有来源完成后都会复核文件大小，provider 给出远端 SHA-256 时再校验，
并写 `.btts-model.json` 完成标记。非 TTY 环境必须传 `--yes`；非空目录必须可识别为
未完成下载，或传 `--force`（只覆盖同名模型文件，不删无关文件）。
下载即表示接受 BreezeBlue Research and Non-Commercial License 1.1。

## btts model verify

```bash
btts model verify [--model DIR] [--deep] [--json]
```

检查必需文件、分片和大小；`--deep` 重算完成标记中每个文件的 SHA-256。
校验失败退出码 5，并给出缺失项。

## btts design init

```bash
btts design init FILE [--template T] [--name NAME] [--language zh|en] [--force]
```

写出一份可编辑的 design YAML。默认模板：zh → `zh-late-night-radio/v1`，
en → `en-podcast-host/v1`。`--template` 与 `--language` 冲突会报错（退出码 2）。
目标文件已存在时必须显式 `--force`。

可用模板：

- `zh-late-night-radio/v1`（深夜电台）
- `zh-documentary-narrator/v1`（纪录片旁白）
- `zh-anime-character/v1`（动画角色）
- `en-podcast-host/v1`
- `en-audiobook-narrator/v1`
- `en-game-character/v1`

## btts design lint

```bash
btts design lint FILE [--show-rendered]
```

离线、确定性的 Instruction Readiness Score（0–100）和分项分数。
有 `block` 级问题时退出码为 1。`design explore` 和 `say --design` 会再次 lint，
遇到 block 时抛 UsageError，退出码是 2。`--show-rendered` 打印真正发给模型的
instruction。这个分数衡量指令就绪度，不是音色质量。

## btts design explore

```bash
btts design explore FILE --out RUN_DIR [--resume] [--wide]
                        [--seeds 42,43] [--cfgs 2.5,4.0] [--yes]
                        [--max-new-tokens N] [--model DIR] [--device mps|cpu]
```

- 默认矩阵：seeds `42,43` × cfg `2.5,4.0` × 1 个 cover = 4 个候选。
- `--wide`：使用前两个 cover，默认 8 个候选；不足两个 cover 会报错。
- 超过 16 个候选必须同时给出显式 `--seeds`/`--cfgs` 和 `--yes`。
- 一次模型加载跑完整矩阵；每个候选原子写入
  `candidates/<take>.wav` 和 `<take>.recipe.json`，每个完成后更新 `run.json`。
- `--out` 已存在且非空时必须用 `--resume`；resume 只补缺失或哈希不匹配的候选。
- resume 沿用原 run 的矩阵：新传的 `--seeds`/`--cfgs`/`--wide` 会被忽略；
  `--max-new-tokens` 会改写所有计划的该字段。要改这些参数请换新的 `--out`。
- 如果 design 的 rendered instruction、template、covers 或模型摘要与 run 创建时
  不一致，会拒绝 resume（退出码 2）；此时换新的 `--out`。
- 第一次 Ctrl-C 在当前 take 完成后停止；第二次立即中断。中断后可用 `--resume`。

## btts design report

```bash
btts design report RUN_DIR [--metrics basic|full]
                          [--csv/--no-csv] [--html/--no-html] [--reel/--no-reel]
                          [--gap 1.0]
```

输出到 `RUN_DIR/report/`：

- `metrics.csv`、`report.md`
- `all_candidates.wav`（所有完整候选按 `--gap` 秒拼接）
- `audition.html`（离线试听页）

默认 `--csv`、`--html`、`--reel` 全开，`--gap` 默认 1.0 秒且限制在 0–30；
`report.md` 始终写入。`basic` 测时长、采样率、峰值/RMS、削波、静音、
过短/过长；`full` 额外测 F0 中位数、voiced ratio、HNR、频谱质心，
需要可选依赖 `[metrics]`（librosa + praat-parselmouth），未安装会打印警告并降级。
不完整的 take 会被跳过。指标用于排除技术性失败和排序，不做审美判断。

## btts design freeze

```bash
btts design freeze RUN_DIR --take TAKE_ID --name NAME
                          [--as design|clone] [--confirm-transcript]
                          [--allow-long-ref] [--force] [--tag TAG ...]
```

把一个完整 take 固化成 voice profile，默认 `--as design`。
CLI 不会自动选 take；`--take` 必须是用户试听后指定的 ID。

- `--as design`：保存 instruction、seed、cfg、`preview.wav`、`design.yaml`。
- `--as clone`：保存 `ref.wav`、`ref.txt`、`preview.wav`。先不带
  `--confirm-transcript` 运行，读取 CLI 打印的 source text，交给用户逐字确认；
  确认后再重跑并加上 flag。
- 参考音频 6–20 秒最佳；少于 6 秒警告，超过 30 秒需 `--allow-long-ref`。
- take 少于 1 秒、状态不完整、或 `finish_reason=length`（可能截断）会被拒绝，
  退出码 5。
- 同名 voice 已存在时需 `--force`。

## btts voice add

```bash
btts voice add NAME --ref REF.wav --text REF.txt --confirm-transcript
                    [--language zh|en] [--allow-long-ref] [--force] [--tag TAG ...]
```

从已获授权的参考音频和逐字转写创建 clone profile，`--language` 默认 `zh`。
`--text` 是文本文件，必须是音频的逐字转写；CLI 不会自动转写。
先不带 `--confirm-transcript` 运行，CLI 会打印转写并退出码 2；
用户逐字确认后再重跑并加上 flag。
时长：少于 1 秒失败，少于 6 秒警告，6–20 秒推荐，超过 30 秒需用户明确同意后
才加 `--allow-long-ref`。

## btts voice list / btts voice show

```bash
btts voice list
btts voice show NAME
```

`list` 显示 name、kind、language、voice_id、cfg、tags 和 store 路径。
`show` 输出完整 profile JSON 和目录文件清单。

## btts say

```bash
btts say TEXT (--design FILE | --voice @NAME) [--direction TEXT]
              [--seed N] [--cfg F] [--out FILE] [--play]
              [--model DIR] [--device mps|cpu] [--max-new-tokens N]
```

底层路由三类，CLI 入口四种，必须且只能选一个入口：

1. `--design FILE` → `tts_instruction`，默认 cfg 4.0、seed 42。
2. `--voice @NAME`（design profile）→ `tts_instruction`，沿用 profile 的
   instruction/seed/cfg。
3. `--voice @NAME`（clone profile）→ `ref_clone_tata`，cfg 1.0，seed 沿用 profile。
4. `--voice @NAME --direction TEXT`（仅 clone）→ `ref_edit_tata`，cfg 4.0，
   seed 沿用 profile。

约束：

- `--design` 与 `--voice` 互斥；`--direction` 只能配合 clone。
- `--cfg` 必须有限、大于 0 且不超过 20；`--seed` 必须非负。
- `--max-new-tokens` 未指定时按文本长度自动估算，且必须大于 0。
- 未指定 `--out` 时写到 `outputs/say-<name>-<UTC>.wav`，并写
  `outputs/say-<name>-<UTC>.recipe.json`。
- `--out` 直接覆盖同名 WAV 和 recipe，不检查存在性；不要指向输入文件。
- `--play` 仅 macOS，依赖 `afplay`；播放失败退出码 4，音频文件保留。
