# btts 命令参考

`btts` 是入口；在未激活虚拟环境时用 `.venv/bin/btts` 或 `uv run btts`。

## 全局约定

- 模型目录优先级（对接受 `--model` 的命令）：`--model DIR` > `$BTTS_MODEL_DIR`
  > `./ms-models/Breeze-TTS-2`。`model download` 用 `--to DIR` 指定目标。
- 设备：`--device mps|cpu`，只适用于需要加载模型的 `doctor --bench`、`design explore`、`say`；
  默认优先 MPS。
- 音色库目录：`$BTTS_DATA_DIR/voices`，否则平台用户数据目录
  （macOS: `~/Library/Application Support/btts_cli/voices`）。
- `--json` 出现在 `doctor`、`model download`、`model verify`、`voice export`、
  `voice list` / `voice filter`；其余命令输出给人看。
- 退出码：1 通用 / lint blocked，2 用法，3 引擎，4 播放，5 完整性，130 中断。
- 除 `model download` 外，任何命令都不会联网或自动更新模型。

## btts completion

```bash
btts completion [SHELL] [--install | --uninstall] [--dir DIR]
```

把对应 shell 的补全脚本写到 stdout（纯脚本文本，可直接重定向）。省略 `SHELL`
时从 `$SHELL` 推断；无法识别时退出码 2，hint 会列出支持的 shell。

```bash
# bash（Click 模板需要 bash 4.4+；macOS 自带 3.2，通常改用 zsh）
btts completion bash > ~/.btts-complete.bash
echo 'source ~/.btts-complete.bash' >> ~/.bashrc

# zsh（.zshrc 需先执行 compinit）
btts completion zsh > ~/.btts-complete.zsh
echo 'source ~/.btts-complete.zsh' >> ~/.zshrc

# fish
btts completion fish > ~/.config/fish/completions/btts.fish

# PowerShell（Click 8.5+）
btts completion powershell >> $PROFILE
```

也可以写入专用补全文件，`--install` 绝不修改 `.bashrc`/`.zshrc`/`config.fish`：

```bash
btts completion zsh --install      # ~/.zfunc/_btts；stderr 打印需要的 fpath 两行
btts completion fish --install     # ~/.config/fish/completions/btts.fish（自动加载）
btts completion bash --install     # ~/.local/share/bash-completion/completions/btts
btts completion zsh --uninstall    # 只删带 btts 标记的文件；不存在时退出码 0
btts completion zsh --install --dir DIR   # 自定义目录
```

- `--install` 幂等：内容相同打印 `up to date`，不同则原子替换。
- 目标已存在但不是 `btts` 生成的、或目标是 symlink 时拒绝（退出码 2），没有
  `--force`；`--uninstall` 同样拒绝删除用户手写文件。
- `--dir` 必须与 `--install` 或 `--uninstall` 同用；二者互斥。
- PowerShell 没有统一的自动加载目录，`--install` 会拒绝（退出码 2）；
  请手工追加到 `$PROFILE`。

脚本使用 Click 的惰性补全协议：TAB 时以 `_BTTS_COMPLETE=<shell>_complete` 回调
`btts`，动态读取本地音色库。补全范围：

- `voice show NAME`、`voice export NAME ...`、`voice list` / `voice filter` 的
  `QUERY`：用户输入 `@` 时补 `@NAME`，否则补裸名。
- `voice rename OLD`、`voice tag NAME`、`voice delete NAME`：只补裸名（这些命令
  不接受 `@`；输入 `@` 时返回空候选）。
- `say --voice`：固定补 `@NAME`。
- 其他枚举型参数（`--match`、`--kind`、`--language`、`--mode`）由 Click 的
  `Choice` 自动补全。

补全只列可寻址、且目录与 `profile.json` 都不是 symlink 的音色；store 不存在、
不可读、profile 损坏或扫描抛错时静默返回空候选，绝不向 stdout 打印
warning/error。补全只做输入辅助，不改变任何命令的校验、确认流程或退出码。

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
                        [--seeds 19,97] [--cfgs 2.5,4.0] [--yes]  # seeds 省略时随机抽 2 个
                        [--max-new-tokens N] [--model DIR] [--device mps|cpu]
```

- 默认矩阵：从 100 以内的 25 个质数中随机抽 2 个 seed × cfg `2.5,4.0` × 1 个 cover = 4 个候选。抽中的 seed 记录在 `run.json` 的 `matrix.seeds` 里；需要固定矩阵时用 `--seeds`。
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
  - 同一 run 的全部完整候选都在一页；推荐项标 `Metric pick` 并默认勾选。
  - 推荐是确定性排序：先按 short/long/truncated/clipping/silence 失败项数量排序
    （失败少的优先），再按削波、静音、HNR、voiced ratio、take id 排序。
    缺失的可选指标显示 `n/a`，不算失败；全部失败时标记 degraded 并显示
    least-bad 提示。
  - 页面底部可复制选中项的 take ID、freeze 命令和实际试听文本；命令已做
    shell quoting，非法音色名用 `YOUR_VOICE_NAME` 占位。inline event 文本
    会自动改用 `--as design`（clone 会拒绝）。
  - 盲听模式隐藏推荐、选中高亮和状态栏信息；复制仍是用户显式操作。

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

`--tag` 会被规范化：去掉首尾空白、去重并保持顺序；空字符串、纯空白、超过
64 字符或含控制字符的标签会在创建前被拒绝（退出码 2），不会写入 profile。

## btts voice rename

```bash
btts voice rename OLD NEW
```

把音色目录 `voices/OLD` 重命名为 `voices/NEW`，并把 `profile.json` 里的
`name` 改成 `NEW`；`voice_id`、`tags`、`provenance`、`consent`、`license`、
`reference`（包括其中的相对文件名）等其余字段全部保留。

- `NEW` 必须通过音色名校验：字母/数字开头，只含字母、数字、`-`、`_`，
  最多 64 字符；`.`、`..`、含分隔符的名字由该校验拦截（退出码 2）。
- `NEW` 已存在时直接拒绝（退出码 2），不会静默覆盖；没有 `--force`。
  如果 `NEW` 是指向别处的 symlink，也按「已存在」拒绝（退出码 2）。
- 源音色目录是 symlink、或 `profile.json` 是 symlink 时拒绝（退出码 5）。
- 大小写不敏感文件系统（如默认 APFS）上，仅大小写不同的改名（`Dusk` → `dusk`）
  会被拒绝并提示先用中间名：`btts voice rename Dusk Dusk-tmp` 再
  `btts voice rename Dusk-tmp dusk`。
- 改名后旧名立即失效：`voice list`、`voice show NEW`、`say --voice @NEW`
  都使用新名；`voice show OLD` 报 voice not found。
- 写入顺序是「先原子改写 `profile.json`，再对目录做同文件系统的原子 rename」。
  如果进程正好停在两步之间，目录仍是 `OLD` 但 profile 已写 `NEW`；重新执行
  同一条 `btts voice rename OLD NEW` 即可修复，不会丢数据。此时
  `voice show OLD` 退出码 5，打印 error + `btts voice rename OLD NEW` 修复
  hint，不输出 profile；`voice list` 用目录名显示并打印同样的修复 warning。
- `rename` 会校验 clone profile 的 `ref.wav` / `ref.txt` 是否存在且未逃逸目录；
  缺失/逃逸时退出码 5。它**不比较文件内容哈希**：参考音频内容损坏只有
  `say` 的 SHA-256 校验会以退出码 5 报出。
- 在 POSIX 上，profile 写入和目录操作通过 `O_DIRECTORY|O_NOFOLLOW` 的目录
  描述符完成，并配合 store 级文件锁，避免验证后路径被并发替换为 symlink。

退出码：0 成功；2 用法错误、`NEW` 已存在（含 symlink 目标）、仅大小写改名、
`OLD` 不存在；5 存储完整性错误（源目录/profile 为 symlink、损坏的 profile、
clone 参考文件缺失）。

## btts voice tag

```bash
btts voice tag NAME [--add TAG ...] [--remove TAG ...] [--set TAG ...] [--clear]
```

不带任何操作时只列出该音色的 tags，不写文件，退出码 0；list 只做 profile
schema/文件名校验，**不检查** clone 参考文件是否存在。带操作时只修改
profile 的 `tags` 字段，其余字段保持不变；写入是原子的。

- 无参数（以及不带 `--add`/`--remove`/`--set`/`--clear`）= list；输出格式与
  变更后一致：`voice: NAME`、`tags: ...`，空时 `(none)`。
- `--set` 用给定标签替换全部现有标签；**每个 tag 需要自己的 flag**：
  `--set calm --set late-night`（`--set calm late-night` 会被 click 当作多余参数）。
- `--clear` 清空全部标签；可与 `--add` 组合（先清空再加入），但与 `--set`、
  `--remove` 互斥（退出码 2）。
- `--set` 与 `--add` / `--remove` 互斥（退出码 2）。
- `--add` 与 `--remove` 可以在同一次调用中组合。
- 标签会去掉首尾空白后去重，并保持首次出现的顺序；空字符串、纯空白、
  超过 64 字符或含控制字符的标签会被拒绝（退出码 2）。
- 历史遗留的未规范化 tags 会在下一次变更时被规范化/去重；`--remove ' padded '`
  能匹配并删除旧的 `" padded "`，`--add ' padded '` 不会产生重复变体。
- `--remove` 不存在的标签是幂等的，不报错。
- voice 不存在时退出码 2。`tag` 的变更操作会校验 clone profile 的参考文件
  是否存在/逃逸（缺失时退出码 5），但同样不比较内容哈希；list 不校验。

示例：

```bash
btts voice tag dusk                              # 列出 tags
btts voice tag dusk --add calm --add narrator
btts voice tag dusk --remove narrator
btts voice tag dusk --set calm --set late-night
btts voice tag dusk --clear
btts voice tag dusk --clear --add fresh
```

退出码：0 成功（含 list）；2 用法错误、非法标签、`--set`/`--clear` 互斥冲突、
voice 不存在；5 存储完整性错误（symlink/profile 损坏、clone 参考文件缺失）。

## btts voice delete

```bash
btts voice delete NAME [--yes]
```

删除 store 根目录下的单个音色目录（含 `profile.json`、参考音频、试听和许可证）。

- 默认必须交互确认（`click.confirm`，默认 No）；用户拒绝时退出码 2，
  目录保持不变。
- 非 TTY 环境没有 `--yes` 时直接拒绝（退出码 2），不会静默删除。
- `--yes` 只应在用户明确要求删除时使用；CLI 不提供通配或批量删除。
- 删除前打印目标路径和目录内容摘要（文件数、字节数）。
- 只允许删除 store 根目录内的真实音色目录；`.`、`..`、含分隔符或通配符的名字
  由音色名校验拦截（退出码 2）。音色目录或 `profile.json` 是 symlink 时拒绝
  （退出码 5），递归删除不会跟随 symlink。
- 在 POSIX 上删除前会先把目录原子改名为 `.deleting-<name>-<token>` 的
  tombstone，再通过目录描述符删除；即使验证后路径被并发替换为 symlink，
  也不会删除 store 外的文件。
- 删除失败（权限、I/O 等）会以退出码 5 报错，并在 hint 中给出 tombstone 路径；
  进程被 SIGKILL/断电时没有提示，但 `.deleting-*` 会留在 store 根目录。
  `voice list` 始终跳过 tombstone，并在 stderr 打印带路径的 warning；
  可用 `rm -rf "$STORE"/.deleting-*` 手动清理。
- 删除后 `voice list` 不再显示该音色，`voice show NAME` 报 voice not found。
- voice 不存在时退出码 2。
- `delete` 不读取/校验 clone 的参考文件；参考文件缺失的 clone 仍可删除。

退出码：0 成功；2 用法错误、用户取消、非交互环境缺少 `--yes`、voice 不存在、
store 路径不是目录；5 存储完整性错误（symlink 目录/profile、删除失败、
tombstone 无法清理）。

## btts voice export

```bash
btts voice export [NAME ...] [--all] [--mode full|wav] [-o/--out PATH]
                  [--zip] [--json]
```

把全部或指定音色导出为可移植 bundle。`NAME` 可带一个前导 `@`；多个名字会去重并
保持首次出现顺序。`--all` 与显式名字互斥；二者必须选一个。选择为空、
`--all` 与名字同用、名字非法或不存在时退出码 2。

模式：

- `--mode full`（默认）：镜像每个音色目录中所有非隐藏普通文件
  （`profile.json`、`LICENSE.txt`、`preview.wav`，以及 `design.yaml` 或
  `ref.wav`/`ref.txt`）。用于备份或迁移。
- `--mode wav`：每个音色只导出一个 `<name>.wav`，来源是 `preview.wav`；
  不含 `profile.json`、`ref.wav`、`ref.txt`、`design.yaml`。manifest 仍记录
  `name`/`voice_id`/`kind`/`language`/`tags`/`profile_sha256`。

两种模式都写：

```text
<out>/
  manifest.json
  LICENSE.txt
  README.txt
  <voice>/...        # full
  <voice>.wav        # wav
```

`manifest.json` 包含 `schema_version`、`kind=btts-voice-export`、UTC
`exported_at`、`cli_version`、`mode`、license、`root_files`、排序后的 `voices`
和 `warnings`；每个导出文件记录相对路径、`sha256:` 和字节数，不写宿主机绝对路径。

输出与覆盖：

- 默认目录 `./btts-voices-<UTC>`；`--zip` 时为 `./btts-voices-<UTC>.zip`。
- `--zip` 要求 `-o` 以 `.zip` 结尾；zip 内只有一个顶层目录（按 archive stem 命名）。
- 目标已存在（含 broken symlink）、或位于 `$BTTS_DATA_DIR/voices` 内时退出码 2。
  **没有 `--force`**，导出永不覆盖。
- 先写隐藏临时 sibling，再用 no-replace rename 发布；失败会清理临时路径。

完整性：

- 导出前严格校验 profile schema、目录名与 `profile.name` 一致、clone 的
  `ref.wav`/`ref.txt` 存在且 SHA-256 匹配、`preview.wav` 非空。
- 音色目录、`profile.json` 或音色树内出现 symlink、特殊文件时退出码 5。
- `--all` 使用严格枚举：坏音色或无权限条目会让整个导出失败（退出码 5），
  不会静默漏导；tombstone 和非音色目录会 warning 后跳过。
- 任一校验失败都不会写出目标路径。

隐私：

- `full` 可能含参考音频、逐字转写、consent/provenance 和 design persona，
  按个人数据处理。
- `wav` 只含音频样本，但 manifest 仍保留音色元数据，不是匿名导出。

`--json` 时 stdout 只有一个 JSON 文档（`out`、`archive`、`mode`、`voices`、
`files`、`bytes`、`manifest_sha256`、`warnings`），不打印 license panel。
退出码：0 成功；2 用法/选择/目标路径问题；5 源音色完整性；130 中断。

## btts voice list / btts voice filter / btts voice show

```bash
btts voice list [QUERY] [--match fuzzy|prefix|exact] [--tag TAG ...]
                [--kind design|clone] [--language zh|en]
                [--names-only | --json]
btts voice filter ...        # 与 voice list 完全相同的别名
btts voice show NAME
```

`list` 显示 name、kind、language、voice_id、cfg、tags 和 store 路径。

- 可选 `QUERY` 只匹配可寻址的音色名（目录名），不搜索 tags/voice_id 等字段。
  不可寻址的目录名不会被 `QUERY` 命中；不带查询时表格/JSON 仍会列出它们并
  打印 warning。匹配前去掉首尾空白和一个前导 `@`；空的有效查询是用法错误
  （退出码 2）。
- `--match fuzzy`（默认）为不区分大小写的连续子串：`girl` 匹配 `std-girl`，
  但 `g-r-l` 不匹配。`prefix` 为不区分大小写的前缀。`exact` 为不区分大小写的
  完整名字相等。
- `--tag` 可重复，要求音色**同时包含**全部给定标签（AND）；标签先按
  `voice tag` 的规则规范化再比较。`--kind`、`--language`、`--tag` 与 `QUERY`
  之间也全部 AND。
- 无匹配是成功空结果（退出码 0）：表格模式打印 `no matching voices.`，
  `--names-only` 输出空，`--json` 的 `count` 为 0。
- `--names-only` 每行输出一个名字（按 store 的排序），不打印表格和 store 路径；
  无法安全寻址或 symlink 的音色目录会被跳过并在 stderr 警告（严格命令如
  `voice export` 会拒绝 symlink，因此不会建议一个随后失败的管道）。适合
  `btts voice export $(btts voice filter --tag calm --names-only)`。
- `--json` 输出一个文档：`schema_version`、`kind: "btts-voice-list"`、`query`、
  `match`、`filters`（`tags`/`kind`/`language`）、`count`、`voices`、`warnings`。
  `--names-only` 与 `--json` 互斥（退出码 2）。
- 筛选是只读操作，复用 `list` 的容错扫描：坏音色被跳过，tombstone 与目录名/
  profile 名不一致在 stderr 打印 warning（`--json` 时也写入 `warnings`）。

单个损坏/非法/不可读的音色目录、无法解析的 clone 参考文件（包括 symlink loop）
都会被跳过，不会让整个 list 失败；`.deleting-*` tombstone 总是被跳过，并在
stderr 打印带路径的 warning。如果目录名与 `profile.json` 的 `name` 不一致
（例如 rename 在两步之间被中断），list 用**目录名**显示并打印修复 warning，
而不是显示尚不可寻址的“未来名字”。
store 根路径不是目录时，list 返回空列表（exit 0，`no voices yet`）；
rename/tag/delete 对该状态报 exit 2。store 根目录或其父目录存在但不可读
（如 chmod 000）时，list 报 exit 2 `cannot read voice store`，不会打印 traceback。

`show` 输出完整 profile JSON 和目录文件清单。目录名与 `profile.name` 不一致、
clone 参考文件缺失/不可读/无法解析（含 symlink loop）时退出码 5，并给出修复
提示；音色目录不可读时退出码 2。两者都不会打印 traceback。

只读路径（`list`/`show`/`say --voice`）当前仍允许跟随音色目录 symlink；
管理命令（`rename`/`tag`/`delete`）一律拒绝 symlink 目录和 symlink
`profile.json`。

## btts say

```bash
btts say TEXT (--design FILE | --voice @NAME) [--direction TEXT]
              [--seed N] [--cfg F] [--out FILE] [--play]
              [--model DIR] [--device mps|cpu] [--max-new-tokens N]
```

底层路由三类，CLI 入口四种，必须且只能选一个入口：

1. `--design FILE` → `tts_instruction`，默认 cfg 4.0、seed 19。
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
