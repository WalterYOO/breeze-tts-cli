# Voice、run 与完整性

## Voice 库

位置：`$BTTS_DATA_DIR/voices`，否则平台用户数据目录
（macOS: `~/Library/Application Support/btts_cli/voices`）。每个音色一个目录：

```text
voices/<name>/
  profile.json          # 结构化身份、默认值、provenance、license
  LICENSE.txt           # 模型许可证声明
  preview.wav           # 试听
  ref.wav               # 仅 clone
  ref.txt               # 仅 clone，逐字转写
  design.yaml           # 仅 design（来自 run 快照）
```

`profile.json` 的关键字段：

- `kind`: `design` | `clone`
- `identity`: 结构化 design + rendered instruction + template + compiler_version
- `design`: `seed` / `cfg_scale` / `instruction` / `max_new_tokens`（仅 design）
- `reference`: 音频/转写文件名、SHA-256、`transcript_confirmed`、时长、采样率（仅 clone）
- `defaults`: 默认 cfg 和 seed（`voice add` 的 seed 为 19；freeze 沿用源 take 的 seed）
- `provenance`: 来源 take/run、模型摘要、CLI 版本
- `license`: 模型许可证标识

写入是原子的：先复制到临时文件再 rename；最后写 `profile.json`。
`--force` 会覆盖同名 profile 的文件，但不会删除目录里的无关文件。

### design vs clone

- **design**：保存配方（instruction + seed + cfg）。适合复现某次试听；
  换文本时音色可能漂移。
- **clone**：保存参考 WAV 和逐字转写。`say --voice` 走 `ref_clone_tata`，
  是跨文本最稳定的身份锚点。转写必须人工确认，CLI 不做自动转写。
- `say --direction` 走 `ref_edit_tata`，是一次性表演提示，不改变 profile。

## Run 目录

```text
runs/<run>/
  run.json                      # manifest：design 快照、矩阵、模型摘要、候选状态
  design.yaml                   # 创建 run 时的 design 快照
  candidates/
    c001.wav
    c001.recipe.json            # 输入文本、instruction、seed、cfg、模型摘要、耗时
    c002.wav
    c002.recipe.json
  report/
    metrics.csv
    report.md
    all_candidates.wav
    audition.html
```

- 每个候选先原子写 WAV，再写 recipe，再更新 `run.json`；因此中断后不会留下
  “看起来完整”的半成品。
- `run.json` 记录每个候选的 text SHA-256、模型摘要和完成状态。
- take id 形如 `c001`，必须匹配 `^c[0-9]{3,}$`。

## Resume 规则

`btts design explore FILE --out RUN_DIR --resume`：

- 只补齐缺失或哈希不匹配的候选；
- 沿用原 run 的候选矩阵；新传的 `--seeds`/`--cfgs`/`--wide` 会被忽略，
  `--max-new-tokens` 会改写所有计划的该字段；
- 如果 design 的 `rendered_instruction`、`template`、`covers` 与 run 创建时不同，
  拒绝 resume；
- 如果模型摘要变化，拒绝 resume；
- 想要尝试新 design、新矩阵、新模型或新生成参数，必须换新的 `--out`。

第一次 Ctrl-C 会等当前 take 完成后停止并标记 `interrupted`；第二次立即中断。
两种情况下都可以用 `--resume` 继续。

## 完整性校验

- `say` 的 clone/direction 路由会校验 `ref.wav` 和 `ref.txt` 的 SHA-256，
  不匹配退出码 5；`voice show` 只做 schema 和文件存在性检查，不比较哈希；
  `voice list` 会静默跳过无效 profile。
- clone 路由拒绝 `transcript_confirmed=false` 的 profile。
- `run.json` 和 `profile.json` 做的是 schema 校验与跨文件交叉校验
  （recipe/WAV、reference 路径），不是对文件自身的防篡改哈希。
- freeze 会拒绝：take 不完整、少于 1 秒、`finish_reason=length`
  或带截断 warning 的 take。
- 不要手改这些文件；需要修改时重新生成或重新 add。

## say 输出

未指定 `--out` 时：

```text
outputs/say-<name>-<UTC>.wav
outputs/say-<name>-<UTC>.recipe.json
```

recipe 记录 route、seed、cfg、instruction、模型摘要、时长和警告，
便于之后复现或排查。
