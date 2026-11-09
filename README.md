# breeze-tts-cli (`btts`)

面向 Apple Silicon（MPS）的本地音色设计工作台，也是 **Breeze TTS 2** 的可复现请求运行器。它把“写 instruction → 生成 seed/cfg 网格 → 比较 → 固化音色”这套手工循环，收敛成一条可脚本化的命令链。

```
design init → design lint → design explore → design report
            → design freeze → voice add/list/show/rename/tag/delete → say
```

## 许可证——请先阅读

- **本 CLI 的源码**采用 Apache-2.0（见 `LICENSE`）。随包引入（vendored）的上游推理源码同样是 Apache-2.0（见 `THIRD_PARTY_NOTICES.md`）。
- **Breeze TTS 2 模型权重及由其生成的全部音频**受 **BreezeBlue Research and Non-Commercial License 1.1** 约束。自托管生成结果**未**获准用于商业用途；商业使用需向 BreezeBlue 另行取得书面许可。
- `doctor`、`voice add`、`voice show`、freeze 输出，以及每个生成的音色目录中的 `LICENSE.txt` 都会打印这份声明。

本项目是研究 / 评估 / 原型工具，不承诺流式或实时性能（本机 MPS 实测 RTF ≈ 9）。

## 安装

```bash
# Python 3.10–3.12。基础安装不会拉取 torch。
pip install -e ".[dev]"

# 本地推理扩展（torch 2.9.1 / transformers 4.57.3 / qwen-tts 0.1.1）
pip install -e ".[mps]"

# 可选的客观指标（F0 / HNR / 频谱质心）
pip install -e ".[metrics]"
```

模型目录**不随包分发**，也绝不会被复制进包内。请把 CLI 指向本地的 Breeze-TTS-2 checkpoint：

```bash
export BTTS_MODEL_DIR=/path/to/Breeze-TTS-2
# 或在具体命令后传 --model /path/to/Breeze-TTS-2
```

期望的 checkpoint 结构：

```
Breeze-TTS-2/
  config.json
  generation_config.json
  tokenizer.json
  tokenizer_config.json
  model.safetensors.index.json
  model-0000*-of-0000*.safetensors
  audio_tokenizer/{config.json,model.safetensors}
```

### 下载模型

模型仓库是公开的（`BreezeBlue/breeze-tts-2`，全部文件约 7.68 GB），无需申请权限。下载只由 `model download` 显式触发；`doctor`、`design explore`、`say` 等命令永远不会联网或自动更新模型。

```bash
# Hugging Face（默认，使用 huggingface-hub；HF_TOKEN 只从环境读取）
btts model download

# ModelScope（只用标准库 urllib，不需要安装 modelscope）
btts model download --source modelscope

# 指定目录、revision，或非交互执行
btts model download --to ms-models/Breeze-TTS-2 --revision main --yes

export BTTS_MODEL_DIR="$PWD/ms-models/Breeze-TTS-2"

# 默认检查必需文件、分片和标记中的大小；--deep 重算 SHA-256
btts model verify
btts model verify --deep --json
```

默认目标目录是 `$BTTS_MODEL_DIR`，否则 `./ms-models/Breeze-TTS-2`。下载前会显示来源、repo、revision、文件数、总大小和许可证；非 TTY 环境必须传 `--yes`；非空目录必须可识别为未完成下载，或传 `--force`（只覆盖同名模型文件，不删除无关文件）。中断后重跑会复用 `.part` 并通过 HTTP `Range` 续传，逐文件校验大小与 SHA-256 后原子替换。完成后写入 `.btts-model.json`（来源、repo、revision、下载时间及每个文件的大小/SHA-256），`doctor` 会显示这些信息。下载即表示接受 BreezeBlue Research and Non-Commercial License 1.1。

如果偏好手动管理 Hugging Face 缓存，仍可使用 `huggingface_hub`：

```bash
hf download BreezeBlue/breeze-tts-2 --local-dir ms-models/Breeze-TTS-2
```

```python
from huggingface_hub import snapshot_download

print(snapshot_download("BreezeBlue/breeze-tts-2"))
```

手动下载不会写 `.btts-model.json`，但 `model verify` 仍可做必需文件/分片完整性检查。

## 快速开始

```bash
# 检查机器、模型文件、许可证，并可选地跑基准
btts doctor
btts doctor --bench --json

# 创建并 lint 一份可编辑的音色设计描述
btts design init dusk.yaml
btts design lint dusk.yaml --show-rendered

# 生成默认的 2 seed × 2 cfg 矩阵（4 个候选，MPS 上约 1.5 分钟）
btts design explore dusk.yaml --out runs/dusk-01

# 客观指标、CSV/Markdown、试听合集，以及离线 audition 页面
btts design report runs/dusk-01 --metrics full

# 把选中的候选固化成可复现的 design 配方
btts design freeze runs/dusk-01 --take c001 --name dusk --as design

# 或固化成 clone 参考（需要人工逐字确认转写文本）
btts design freeze runs/dusk-01 --take c001 --name dusk --as clone --confirm-transcript

btts voice list                                  # 全部音色
btts voice list girl                             # 模糊：名字包含 girl（不区分大小写）
btts voice list --match prefix std-              # 前缀匹配
btts voice list --match exact @dusk              # 精确匹配完整名字
btts voice list --tag calm --tag night --kind clone --language zh
btts voice filter girl --names-only              # 别名；每行一个名字，便于管道
btts voice list --json                           # 单个 JSON 文档
btts voice show dusk

# say 的三条路由
btts say "欢迎收听今晚的节目。" --design dusk.yaml
btts say "欢迎收听今晚的节目。" --voice @dusk
btts say "欢迎收听今晚的节目。" --voice @dusk --direction "更轻、更慢，带一点笑意"

# 管理已有音色
btts voice rename dusk late-night
btts voice tag late-night                              # 只列出 tags
btts voice tag late-night --add calm --add narrator
btts voice tag late-night --set calm --set late-night  # 每个 tag 一个 flag
btts voice tag late-night --clear
btts voice tag late-night --clear --add fresh
btts voice delete late-night          # 交互确认；用户明确同意后才用 --yes

# 导出：全部音色的完整信息 / 指定音色的音频样本 / 单个 zip
btts voice export --all -o ~/backup/btts-voices
btts voice export @dusk @late-night --mode wav -o ./samples
btts voice export --all --zip -o btts-voices.zip --json
```

## Shell 补全

`btts completion` 输出对应 shell 的补全脚本。脚本在按 TAB 时回调 `btts`，
因此 `voice show`、`voice export`、`voice tag`、`voice delete`、`voice rename OLD`、
`voice list`/`voice filter` 的 `QUERY`，以及 `say --voice` 都会动态补全本地音色名
（`say --voice` 固定补 `@NAME`；其余命令跟随用户输入，输入 `@` 就补 `@NAME`）。
省略 `SHELL` 时从 `$SHELL` 推断，支持 `bash`、`zsh`、`fish`、`powershell`。

```bash
# bash（Click 的补全脚本需要 bash 4.4+；macOS 自带 3.2，请用 zsh 或新版 bash）
btts completion bash > ~/.btts-complete.bash
echo 'source ~/.btts-complete.bash' >> ~/.bashrc

# zsh（确保 .zshrc 已调用 compinit）
btts completion zsh > ~/.btts-complete.zsh
echo 'source ~/.btts-complete.zsh' >> ~/.zshrc

# fish
btts completion fish > ~/.config/fish/completions/btts.fish

# PowerShell（Click 8.5+；追加到 $PROFILE，重开终端后生效）
btts completion powershell >> $PROFILE
```

也可以让 `btts` 写入专用补全文件，**绝不修改 rc 文件**：

```bash
btts completion zsh --install     # 写入 ~/.zfunc/_btts，并打印 .zshrc 需要的 fpath 两行
btts completion fish --install    # 写入 ~/.config/fish/completions/btts.fish，fish 自动加载
btts completion bash --install    # 写入 ~/.local/share/bash-completion/completions/btts
btts completion zsh --uninstall   # 只删除带 btts 标记的文件，可重复执行
btts completion zsh --install --dir ~/my-completions   # 自定义目录
```

`--install` 是幂等的；目标文件若已存在但不是 `btts` 生成的、或目标是 symlink，
会拒绝覆盖/写入（退出码 2）。`--dir` 必须与 `--install`/`--uninstall` 同用。
PowerShell 没有统一的自动加载目录，所以不支持 `--install`，请手工追加到 `$PROFILE`。

补全只列出可寻址、非 symlink 的音色；store 不存在、不可读或 profile 损坏时静默
返回空候选，不会向 stdout 打印 warning/error，也不会改变任何命令的校验和退出码。

## 命令（P0）

| 命令 | 用途 |
|---|---|
| `doctor [--bench] [--json]` | 检查环境、模型文件、MPS、内存、指标后端（basic/partial/full）、许可证；可选执行加载 + 短句生成基准 |
| `model download [--source huggingface\|modelscope] [--repo ID] [--revision REV] [--to DIR] [--force] [--yes] [--json]` | 显式下载或续传 checkpoint，逐文件校验并写完成标记 |
| `model verify [--model DIR] [--deep] [--json]` | 检查必需文件/分片/大小；`--deep` 重算 SHA-256 |
| `design init FILE [--template T] [--language zh\|en]` | 写出一份可编辑的 design YAML |
| `design lint FILE [--show-rendered]` | 离线、确定性的指令就绪度评分（Instruction Readiness Score） |
| `design explore FILE --out RUN_DIR [--resume] [--wide]` | 生成候选；每个候选原子写入 WAV + recipe sidecar |
| `design report RUN_DIR [--metrics basic\|full]` | 指标、CSV、Markdown、`all_candidates.wav`、离线 HTML |
| `design freeze RUN_DIR --take ID --name NAME [--as design\|clone]` | 把某个候选固化成音色 profile |
| `voice add NAME --ref WAV --text FILE --confirm-transcript` | 用已获授权的参考音频添加 clone profile |
| `voice list [QUERY] [--match fuzzy\|prefix\|exact] [--tag TAG ...] [--kind design\|clone] [--language zh\|en] [--names-only] [--json]` | 查看并按名字/标签/类型/语言筛选本地音色库；`voice filter` 是同一命令的别名 |
| `voice rename OLD NEW` | 同时改目录名和 profile 的 `name`；目标已存在时拒绝，绝不覆盖 |
| `voice tag NAME [--add TAG ...] [--remove TAG ...] [--set TAG ...] [--clear]` | 无参数列出 tags；否则只修改 `tags`。`--set`/`--clear` 与相应操作互斥，每个 tag 需要自己的 flag |
| `voice delete NAME [--yes]` | 删除单个音色目录；默认交互确认，非 TTY 环境必须显式 `--yes` |
| `voice export [NAME ...] [--all] [--mode full\|wav] [-o PATH] [--zip] [--json]` | 导出全部/指定/多个音色；`full` 含 profile、参考文件与配方，`wav` 只含可试听样本；目录或 zip bundle 永不覆盖已有路径 |
| `completion [SHELL] [--install\|--uninstall] [--dir DIR]` | 输出或安装 `bash`/`zsh`/`fish`/`powershell` 补全脚本；动态补全音色名，省略 SHELL 时从 `$SHELL` 推断 |
| `say TEXT --design FILE` | `tts_instruction`，cfg 4.0 |
| `say TEXT --voice @NAME` | `ref_clone_tata`（clone）或已保存的 design 配方；clone 使用 cfg 1.0 |
| `say TEXT --voice @NAME --direction TEXT` | `ref_edit_tata`，cfg 4.0 |

`voice list` 的筛选规则：`QUERY` 只匹配可寻址的音色名（目录名），`fuzzy` 为不区分大小写的连续子串，`prefix` 为不区分大小写的前缀，`exact` 为不区分大小写的完整名字；`QUERY` 前后的空白和一个前导 `@` 会被忽略。`--tag` 可重复，要求**同时包含**所有给定标签；`--kind`、`--language`、`--tag` 与 `QUERY` 之间全部是 AND。无匹配是成功空结果（退出码 0）。不可寻址的目录名不会被 `QUERY` 命中，但不带查询时表格/JSON 仍会列出并 warning，方便修复。`--names-only` 每行输出一个名字，跳过不可寻址或 symlink 音色并在 stderr 警告；`--json` 输出一个带 `query`/`match`/`filters`/`count`/`voices`/`warnings` 的 JSON 文档，两者互斥。筛选是只读操作，和 `voice list` 一样使用容错扫描：跳过坏音色，并对 tombstone、目录名/profile 名不一致、不可寻址或 symlink 条目打印 warning。

explore 的默认矩阵：从 100 以内的 25 个质数里随机抽 2 个 seed × `cfg=2.5,4.0` = **4 个候选**。抽中的 seed 会写进 `run.json`，take 本身仍可复现；想要固定矩阵就用 `--seeds` 指定。`--wide` 会加入第二段 cover 台词 = **8 个候选**。超过 16 个候选的显式矩阵必须同时传 `--seeds`/`--cfgs` 和 `--yes`。

## Design YAML

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
covers:
  - "晚上好，欢迎收听今天的节目。"
  - "外面下着雨，我们慢慢聊。"
```

编译器会保存用户意图（`structured`）、真正发给模型的字符串（`rendered_instruction`），以及 `compiler_version`。

## design 与 clone 的区别

Breeze 没有可保存的音色 prompt。一次生成等于 `instruction + text + seed + cfg + model digest`。

- **`design` profile** 保存 instruction、seed、cfg 和一段试听。它回放的是配方，同一条 instruction 在不同文本上可能出现音色漂移。
- **`clone` profile** 保存参考 WAV 及其逐字转写文本。这是跨文本最稳定的身份锚点（`ref_clone_tata`），也是生产角色的推荐选择。
- **Direction**（`ref_edit_tata`）是一次性的表演提示，永远不会写进音色身份。

以 clone 方式固化需要 `--confirm-transcript`；CLI 会打印源文本，供人工检查是否有漏字或多字。参考音频建议 6–20 秒；低于 6 秒会警告，超过 30 秒需要 `--allow-long-ref`。

## 数据与输出

- 音色库：`$BTTS_DATA_DIR/voices`，或平台用户数据目录（macOS 上为 `~/Library/Application Support/btts_cli/voices`）。`voice rename` 会整体移动音色目录并同步 `profile.json` 的 `name`；目标检查与移动在 store 锁内完成，并使用 no-replace 原语（macOS `renamex_np(RENAME_EXCL)`、Linux `renameat2(RENAME_NOREPLACE)`），所以目标已存在、仅大小写改名、源目录/profile 为 symlink 时拒绝（指向源目录的 symlink 目标按「已存在」处理），并发 `voice add` 也不会覆盖刚改名的音色。`voice tag` 无参数只列出 tags（不检查 clone 参考文件，退出码 0），带操作时只原子改写 `tags`；`voice add --tag` 和 `voice tag` 都会去空白、去重并校验标签。`voice delete` 只删除 store 内的单个真实目录，拒绝 symlink，POSIX 上先改名为 `.deleting-*` tombstone 再用 no-follow 目录描述符删除；删除失败会以退出码 5 给出 tombstone 路径，SIGKILL/断电没有提示，但 `voice list` 会跳过 tombstone 并在 stderr 打印带路径的 warning，可手动 `rm -rf` 清理。`rename`/`tag` 变更会校验 clone 参考文件是否存在/逃逸（不比较哈希），`delete` 不校验。
- `design explore` 写出 `runs/<run>/run.json`、`candidates/<take>.wav` 和 `<take>.recipe.json`。每个完成的候选都原子写入；`--resume` 只补齐缺失或哈希不匹配的候选。
- `design report` 写出 `report/metrics.csv`、`report/report.md`、`report/all_candidates.wav` 和 `report/audition.html`。试听页把该 run 的全部完整候选放在一起，标出指标推荐项并默认选中，可复制选中项的 take ID、freeze 命令和试听文本。
- `say` 在未指定 `--out` 时写出 `outputs/say-*.wav` 及 recipe sidecar。
- `voice export` 在目标目录或 `--zip` 里写出 `manifest.json`、`LICENSE.txt`、`README.txt`；`full` 模式会镜像每个音色目录里所有非隐藏的普通文件（通常为 `profile.json`、`LICENSE.txt`、`preview.wav`，以及 `design.yaml` 或 `ref.wav`/`ref.txt`），`wav` 模式下每个音色一个 `<name>.wav`。manifest 记录每个文件的 `sha256:` 哈希；`wav` 模式虽然不含 profile/参考文件，manifest 仍保留音色名、voice_id、kind、language、tags 和 profile 哈希。导出前会严格校验 profile、目录名、clone 参考哈希和 preview；`--all` 遇到坏音色或无权限条目会整体失败而不是静默漏导，目标已存在（含 broken symlink）或位于音色库内时拒绝，先写隐藏临时目录再用 no-replace rename 发布。`full` 可能包含参考音频、逐字转写和 consent 元数据，按个人数据处理。
- sidecar 和 profile 中永远不含 token、私有 URL 或绝对路径。

## 指标不是评委

`design report` 会测量时长、削波、静音；`full` 模式还会测 F0 中位数 / p10–p90 / 标准差、HNR、频谱质心、LTAS 斜率（dB/oct）和 2–6 kHz 对 100–500 Hz 的能量比（HF/LF）。HF/LF 能区分中位 F0 相同、但气声或摩擦噪声不同的音色。这些指标用于**排除和排序**技术性失败，不是审美评分；`audition.html` 的 `Metric pick` 只是按这些指标给出的默认选项，最终选择仍然由人试听决定。

## 测试

```bash
ruff check .
pytest                         # 只用 fake engine，不需要 MPS
BTTS_RUN_MPS=1 pytest -m mps   # 真实加载模型 + 三条 say 路由
```

`tests/test_wheel_contents.py` 会真正构建 wheel，并断言其中不含权重、不含 `audio_tokenizer/`、不含 `ms-models/`，且包含全部许可证文件。

## Vendored 推理代码

只复制上游 `breezeblue-ai/breeze-tts` 中 MPS 所需的最小子集，并把 import 改成带包名的形式，放在 `src/btts_cli/_vendor/breeze_tts/` 下。`vendor.lock.json` 固定上游 commit 与哈希；`scripts/sync_breeze_vendor.py --check` 校验代码树，`--ref <commit>` 执行显式升级。详见 `THIRD_PARTY_NOTICES.md`。
