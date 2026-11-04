# 排障

先跑 `btts doctor --json`，大多数问题能从这里定位。

## 安装与环境

| 现象 | 处理 |
|---|---|
| `btts: command not found` | 用 `.venv/bin/btts` 或 `uv run btts`；先 `uv sync` |
| `torch_installed=false` | `uv sync --extra mps`（torch / transformers / qwen-tts） |
| `metrics full` 缺 F0/HNR | `uv sync --extra metrics`（librosa + praat-parselmouth），或改用 `--metrics basic` |
| `afplay not found` / 非 macOS | 去掉 `--play`；WAV 已写好 |
| 非 TTY 下 `model download` 报错 | 加 `--yes`（在用户确认后） |

## 模型

| 现象 | 处理 |
|---|---|
| `model.exists=false` | 先问用户是否下载，再 `btts model download` |
| `model.missing` 非空 | 下载不完整：重跑 `btts model download`；或 `model verify --deep` |
| 下载中断 | 直接重跑；ModelScope 走 `.part` + Range 续传，Hugging Face 沿用 `snapshot_download` 缓存 |
| 目录非空但无法识别 | 确认是未完成下载；否则换目录，或在用户确认后 `--force` |
| 模型路径不对 | `--model DIR` 或 `export BTTS_MODEL_DIR=...` |
| `run.json` 模型摘要与当前模型不符 | 换新的 `--out`，不要混跑 |

`doctor` 会把模型缺失作为警告显示，但通常不以非零退出；要读 JSON 或人读输出。

## MPS / 内存 / 生成

| 现象 | 处理 |
|---|---|
| `mps_available=false` | 检查 Apple Silicon、macOS、torch 安装；可 `--device cpu`（很慢） |
| 退出码 3，生成失败 | 看 hint；关闭占内存的应用；缩短文本或 `--max-new-tokens` |
| 16 GB 机器警告 | 模型约占 8 GB，容易吃紧；先关其他应用 |
| 生成很慢 | RTF≈9 是预期；不要按实时 TTS 设超时 |
| 输出被截断 | 增大 `--max-new-tokens` 后重跑；截断的 take 不能 freeze |

## Design / explore

| 现象 | 处理 |
|---|---|
| `design lint` 退出码 1 | 有 block 问题：按 `references/design-yaml.md` 修 |
| `design explore` / `say --design` 退出码 2 且提示 lint block | 不是参数错误；先修 design，再重跑 |
| `--out` 非空 | 用 `--resume` 续跑，或换新目录 |
| `--wide requires at least two cover scripts` | 在 YAML 里加第二个 cover，或去掉 `--wide` |
| 矩阵超过 16 个候选 | 减少 `--seeds`/`--cfgs`；确认成本后才加 `--yes` |
| 中断 | 重跑同一条命令并加 `--resume` |
| design 改动后 resume 被拒 | 设计已变化，换新 `--out`，保持 run 可复现 |
| 指标里出现大量静音/削波 | 该 take 技术性失败，排除；不要直接选它 |

## Voice / freeze

| 现象 | 处理 |
|---|---|
| `voice already exists` | 换 name；确认后才 `--force` |
| `voice not found` | `btts voice list`；检查 `BTTS_DATA_DIR` |
| `requires --confirm-transcript` | 把 CLI 打印的文本交给用户逐字确认，再重跑 |
| `reference audio is ... over 30s` | 换更短的 take/参考，或确认后 `--allow-long-ref` |
| `hash mismatch` | `say` 会校验参考哈希；不要手改文件，重新 `voice add` 或重新 freeze |
| `say --out` 覆盖了旧文件 | CLI 不检查目标存在性；之后先检查目标，默认用时间戳路径 |
| `take ... reached max_new_tokens` | 用更大的 `--max-new-tokens` 重新生成后再 freeze |
| 参考音频少于 6 秒 | 可以用，但克隆稳定性下降；建议 6–20 秒 |

## 许可证

任何 voice 目录的 `LICENSE.txt`、`doctor`、`voice add/show`、freeze 输出都会
打印 BreezeBlue Research and Non-Commercial License 1.1。不要移除声明，
不要把生成结果描述为可商用。
