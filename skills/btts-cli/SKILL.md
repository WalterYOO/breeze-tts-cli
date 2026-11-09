---
name: btts-cli
description: >-
  Operate the local `btts` CLI (breeze-tts-cli) for Breeze TTS 2 on Apple Silicon:
  doctor, model download/verify, design init/lint/explore/report/freeze,
  voice add/list/show/rename/tag/delete/export, and say. Use when the user mentions
  btts, breeze-tts, Breeze TTS, 音色设计, voice design, 克隆音色 / voice clone,
  design explore, freeze a take, export voices, or wants local TTS with a designed
  or cloned voice.
---

# btts-cli

`btts` 是 Breeze TTS 2 的本地音色设计工作台。一次生成等于
`instruction + text + seed + cfg + model digest`；Breeze 没有可保存的「音色 prompt」，
所以可复现性和人工试听是整个流程的核心。

命令在项目根目录执行。若 `btts` 不在 PATH，改用 `.venv/bin/btts` 或 `uv run btts`，
选定一个并全程复用。

## 先读这三条

1. **许可证**：模型权重和生成音频受 BreezeBlue Research and Non-Commercial License 1.1
   约束。只用于研究、评估和非商业原型；不要承诺商业用途。
2. **不要替用户确认**：`model download`（约 7.7 GB，并代表接受许可证）、
   `--confirm-transcript`、`--force`、`--allow-long-ref`、超过 16 个候选的矩阵、
   覆盖已存在的输出，都必须先得到用户明确同意。
3. **指标不是评委**：`design report` 的指标只用于排除技术性失败和排序；
   `audition.html` 里的 Metric pick 只是指标推荐，最终选哪个 take 必须由人试听决定。

## 心智模型

底层路由只有三类，CLI 入口有四种：

| 底层路由 | CLI 入口 | 稳定性 |
|---|---|---|
| `tts_instruction` | `say TEXT --design FILE`，或 `say TEXT --voice @NAME`（design profile） | 同 instruction 换文本可能漂移 |
| `ref_clone_tata` | `say TEXT --voice @NAME`（clone profile） | 参考音频 + 逐字转写是最稳的锚点 |
| `ref_edit_tata` | `say TEXT --voice @NAME --direction "..."`（仅 clone） | 一次性，不写入音色身份 |

- **design profile** 保存 instruction、seed、cfg 和试听；回放的是「配方」。
- **clone profile** 保存 `ref.wav` + `ref.txt`；生产角色优先 clone。
- `--direction` 只能配合 clone 使用，不能与 `--design` 同用，也不属于音色身份。

## 决策树

### 0. 先确认环境

```bash
btts doctor --json
```

读 JSON 里的 `model.exists`、`model.missing`、`device.torch_installed`、
`device.mps_available`、`warnings`。注意：模型缺失通常**不会**让 `doctor` 以非零退出，
不要只看退出码。

- `torch_installed=false` → 需要 `uv sync --extra mps`。
- 模型缺失或不完整 → 先问用户是否下载，再执行 `btts model download`。
  非交互环境必须加 `--yes`。
- 下载完成后运行 `btts model verify --json`；怀疑文件损坏时加 `--deep`。

### 1. 从零设计一个音色

```bash
btts design init dusk.yaml
# 编辑 identity / delivery_baseline / scene / covers / avoid
btts design lint dusk.yaml --show-rendered
btts design explore dusk.yaml --out runs/dusk-01
btts design report runs/dusk-01 --metrics basic
# 把 runs/dusk-01/report/audition.html 交给用户试听：
#   - 同一批候选都在这一页；默认勾选 Metric pick（指标推荐，不是审美结论）
#   - 用户可改选，点 Copy take ID / Copy freeze command / Copy script
# 等用户把选中结果发回，确认 TAKE_ID 后再 freeze；在此之前不要 freeze
btts design freeze runs/dusk-01 --take <TAKE_ID> --name dusk --as clone
# CLI 打印 source text 后以退出码 2 停止；把原文交用户逐字确认
btts design freeze runs/dusk-01 --take <TAKE_ID> --name dusk --as clone --confirm-transcript
```

- `audition.html` 把该 run 的全部完整候选放在同一页，按失败项、削波、静音、
  HNR 做确定性排序并标出 `Metric pick`，默认勾选它；这**不是**审美结论。
  页面底部可复制选中项的 take ID、freeze 命令和试听文本。盲听模式会隐藏推荐
  与选中信息。
- lint 有 `block` 级问题（退出码 1）时不要硬跑 explore，先把问题修掉。
- explore 默认 4 个候选（从 100 以内质数池随机抽 2 个 seed × cfg `2.5,4.0`）；`--wide` 默认 8 个。抽中的 seed 见 `run.json`。
  RTF≈9，4 个约 1.5 分钟；超时给足，中断后用 `--resume` 续跑。
- freeze 成 clone 前，必须把 CLI 打印的 source text 交给用户逐字确认，
  `--confirm-transcript` 只能在用户确认后加。参考音频 6–20 秒最佳，
  超过 30 秒需 `--allow-long-ref`；被截断的 take（`finish_reason=length`）会被拒绝。
- 需求是「跨文本稳定」时优先 `--as clone`；只需要复现配方时用 `--as design`。

### 2. 已有参考音频

```bash
btts voice add narrator --ref ref.wav --text ref.txt
# CLI 打印 transcript 后以退出码 2 停止；把转写交用户逐字确认
btts voice add narrator --ref ref.wav --text ref.txt --confirm-transcript --language zh
```

`ref.txt` 必须是音频的逐字转写；CLI 不会自动转写。参考音频需已获授权。
时长：少于 1 秒失败，少于 6 秒警告，6–20 秒推荐，超过 30 秒必须由用户明确同意后
才加 `--allow-long-ref`。

### 3. 管理已有音色

```bash
# 改名：同时改目录名和 profile.json 的 name；目标已存在时拒绝，绝不覆盖
btts voice rename OLD NEW

# 标签：无参数只列出 tags；--set 与 --add/--remove 互斥
btts voice tag NAME
btts voice tag NAME --add calm --add narrator
btts voice tag NAME --remove narrator
btts voice tag NAME --set calm --set narrator   # 每个 tag 一个 flag
btts voice tag NAME --clear                     # 清空全部
btts voice tag NAME --clear --add fresh         # 先清空再加入

# 删除：交互确认；--yes 只用于用户已明确同意之后
btts voice delete NAME
btts voice delete NAME --yes

# 导出：全部/指定/多个；full 含完整信息，wav 只含可试听样本
btts voice export --all -o ~/backup/btts-voices
btts voice export @dusk narrator --mode wav -o ./samples
btts voice export --all --zip -o btts-voices.zip --json
```

- `rename` 保留 `voice_id`、`tags`、`provenance`、`consent`、`license` 和
  `reference` 的相对文件名，只改 `name` 和目录名。旧名立即失效，
  `voice list` / `voice show` / `say --voice @NEW` 都使用新名。目标已存在时
  拒绝；APFS 上仅大小写不同的改名也会被拒绝并提示先用中间名。`rename` 会
  校验 clone 的参考文件，缺失时退出码 5。
- `tag` 只修改 `tags` 字段；无参数时只列出 tags 且不写文件；`--remove`
  不存在的标签是幂等的；`--clear` 与 `--set`/`--remove` 互斥。
  `voice add --tag` 和 `voice tag` 都会对标签去空白、去重并校验。
- `delete` 只删除 store 根目录内的单个音色目录；默认交互确认，非 TTY 环境必须
  用户明确同意后由用户传 `--yes`。删除不会校验 clone 参考文件是否完整。
- `export` 选 `--all` 或显式名字（可带 `@`，多个会去重）。`--mode full`（默认）
  镜像完整音色目录；`--mode wav` 只导出 `<name>.wav` 试听样本。两种模式都带
  `manifest.json`、`LICENSE.txt`、`README.txt`，manifest 记录每个文件的
  `sha256:`。导出**绝不覆盖**已有目标（没有 `--force`），目标位于音色库内也拒绝；
  `--all` 遇到坏音色或无权限条目会整体失败（退出码 5），不会静默漏导。
  `full` 可能含参考音频、逐字转写和 consent 元数据，按个人数据处理。

### 4. 直接合成

```bash
btts say "晚上好。" --design dusk.yaml
btts say "晚上好。" --voice @dusk
btts say "晚上好。" --voice @dusk --direction "更轻、更慢，带一点笑意"
```

`--design` 和 `--voice` 必须二选一。默认输出
`outputs/say-<name>-<UTC>.wav`，并写同名 `.recipe.json` sidecar。
`--out` 会直接覆盖同名 WAV 和 sidecar，且不会提示；使用前检查目标是否存在，
禁止指向 design YAML、参考音频或其他输入文件。
`--play` 只在 macOS 上调用 `afplay`；播放失败退出码 4，但 WAV 已保留。

## 退出码 → 下一步

| code | 含义 | 下一步 |
|---|---|---|
| 1 | 通用错误；`design lint` 的 block | 看错误和 hint；lint 问题先修 design |
| 2 | 用法/输入校验；explore 或 `say --design` 遇到 lint block | 检查参数、路径、互斥；block 先修 design |
| 3 | 引擎 / 模型 / 设备失败 | `doctor` 看 MPS、内存、checkpoint；必要时 `--device cpu` |
| 4 | 播放失败 | WAV 已保留；检查 `afplay` 或去掉 `--play` |
| 5 | 完整性失败（hash / profile / take / export 源音色） | 不要手改文件；重新生成或用 `--resume`；坏音色修复后再导出 |
| 130 | 被 Ctrl-C 中断 | 用 `--resume` 继续 explore |

## 硬性禁止

- 不要自动下载模型，不要静默接受许可证。
- 不要代替用户确认 transcript，不要把 `--confirm-transcript` 当形式参数。
- 不要自行使用 `--allow-long-ref`；超过 30 秒的参考必须由用户决定。
- 不要用 `--out` 指向已存在文件或任何输入文件；它不会提示就覆盖。
- 不要手改 `run.json`、`*.recipe.json`、`profile.json`、`ref.txt`；
  它们有结构、哈希或交叉校验，改了会失败或破坏复现。
- 不要把不同 design、不同矩阵或不同模型生成的 take 混进同一个 run；换新的 `--out`。
- 不要在用户未确认时 `--force` 覆盖已有 voice，或删除 `runs/`、`voices/`。
- **删除音色必须由用户明确确认，不要由 agent 擅自执行**：不要在用户没有明确
  要求时运行 `voice delete`，也不要替用户加 `--yes`。
- 不要把 `voice export --mode full` 的 bundle 当匿名音频分享；它可能包含参考
  音频、逐字转写和 consent 元数据。导出目标已存在时不要尝试绕过（没有
  `--force`），让用户选新路径。
- 不要在用户试听并选定 TAKE_ID 前执行 `design freeze`。
- 不要把 metrics 当审美结论；最终选择必须有人试听。
- 不要声称支持流式或实时；本机 MPS 实测 RTF≈9。

## 参考

- 完整命令、默认值、输出路径：`references/commands.md`
- Design YAML、模板、lint 规则：`references/design-yaml.md`
- Voice/run 存储、resume、完整性：`references/voice-and-runs.md`
- 排障：`references/troubleshooting.md`
