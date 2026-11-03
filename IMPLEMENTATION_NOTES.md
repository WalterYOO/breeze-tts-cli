# 实现说明 — btts_cli P0

范围：PRD v0.2 第 3–9 节（P0）。P1/P2 功能有意未实现。

## 状态（2026-11-03）

- P0 已实现并提交，提交为 `fb48022 feat: implement btts_cli P0`
  （父提交 `4b849a4 docs: add btts_cli PRD v0.2`）。
- Codex 独立审核共进行两轮：
  - 第一轮：**FAIL** —— 7 个 Must-fix（路径穿越、take-id 穿越、resume 混合
    模型、clone 转写文本展示、括号事件、`say` 绕过 lint、`doctor --json`
    stdout 污染）。
  - 第二轮：**FAIL** —— 原 7 项确认关闭，但发现 M8
    （`doctor --bench --json` 被 vendored 代码的输出污染）与 M9
    （生成阶段异常以 exit 1 逃逸）。
  - M8/M9 修复后的最终定向复核：**PASS**。
- 当前验证状态：`ruff check .` 通过；`pytest -q` **85 passed, 1 skipped**；
  `BTTS_RUN_MPS=1 pytest -m mps` 在本机 **1 passed**；真实
  `doctor --bench --json` 的 stdout 可被严格 JSON 解析。
- 未配置 git remote；提交仅存在于本地仓库。

## 运行环境

- 解释器：`../breeze-tts/.venv/bin/python`（Python 3.12.11）
- 已安装依赖：torch 2.9.1、transformers 4.57.3、qwen-tts 0.1.1
- 模型：`../breeze-tts/ms-models/Breeze-TTS-2`（从未复制进本仓库）
- 未修改 `../breeze-tts` 源码。

该 venv 由 uv 管理且没有 `pip`，因此 `pip install -e ".[dev]"` 的等价命令是：

```bash
uv pip install --python ../breeze-tts/.venv/bin/python -e ".[dev]"
```

## 已实现范围

| 命令 | 行为 |
|---|---|
| `doctor [--bench] [--json] [--model] [--device]` | 检查环境、模型文件/digest、torch/MPS、内存、可选真实基准、许可证。普通检查 exit 0；`--bench` 失败 exit 3。 |
| `design init` | 从模板注册表（6 个模板）写出可编辑的 v1 design YAML。`--force` 覆盖。 |
| `design lint` | 五个维度、block/warn 规则、`--show-rendered`。lint 阻断 exit 1，schema/用法错误 exit 2。 |
| `design explore` | 默认 4 个候选（2 seeds × 2 cfgs），`--wide` 为 8 个。支持显式 `--seeds`/`--cfgs`。矩阵超过 16 个需要 `--yes`。WAV 与 recipe 依次原子写入，每个候选后更新 manifest。SIGINT 在当前候选结束后停止。`--resume` 只补齐缺失/哈希不匹配的候选。 |
| `design report` | 生成 `metrics.csv`、`report.md`、`all_candidates.wav`、`audition.html`；可切换 `--csv/--no-csv`、`--html/--no-html`、`--reel/--no-reel`。 |
| `design freeze` | 从候选固化 design profile，或通过 `--as clone --confirm-transcript` 固化 clone profile（`--force`、`--allow-long-ref`）。拒绝哈希不匹配、被截断的候选、内联事件，以及不符合短/长参考策略的音频。 |
| `voice add/list/show` | clone profile 保存 `ref.wav` + 逐字 `ref.txt` + preview。写出 `profile.json`、`LICENSE.txt`；回放时校验哈希。 |
| `say` | 只允许一条路由：`--design`、`--voice @clone` 或 `--voice @clone --direction`。先写 WAV，再写 recipe，最后可选调用 `afplay` 播放。 |
| 指标 | 基础指标（numpy/soundfile）：时长、dBFS、削波、静音、异常长度。完整模式在安装了 librosa/parselmouth 时增加 F0/HNR/频谱质心；否则降级并给出提示。 |
| Vendoring | 固定为 `daa6cb2e…` 的恰好 12 个上游文件，含 import 改写、来源头、`vendor.lock.json`、`THIRD_PARTY_NOTICES.md`、上游 Apache-2.0 LICENSE。 |

`btts_cli --help` 和所有非推理命令启动时都不会导入 torch。只有
`btts_cli.engine.mps` 会在生成阶段通过 `load_engine` 导入 torch。

## 验证命令

```bash
# 风格检查 + 单元测试（不需要 MPS）
../breeze-tts/.venv/bin/python -m ruff check .
../breeze-tts/.venv/bin/python -m pytest -q

# vendor 漂移检查
../breeze-tts/.venv/bin/python scripts/sync_breeze_vendor.py --check

# 真实 MPS 集成测试（显式开启）
BTTS_RUN_MPS=1 \
BTTS_MODEL_DIR=../breeze-tts/ms-models/Breeze-TTS-2 \
../breeze-tts/.venv/bin/python -m pytest -q -m mps
```

## 测试结果

- `ruff check .`：全部通过。
- 默认 `pytest -q`：**85 passed, 1 skipped**（跳过的是 MPS 测试）。
- `BTTS_RUN_MPS=1 pytest -q -m mps`：多次运行均为 **1 passed**，约 46–53s。
- wheel 测试会构建真实 wheel，并断言其中没有 `*.safetensors`、没有
  `audio_tokenizer/`、没有 `ms-models/`，且解包后的 wheel 能运行
  `btts_cli --help` 而不导入 torch。
- 真实 MPS CLI 冒烟测试（2026-11-03），模型 digest
  `sha256:3ed2200577065bf5a5d33aece744cf56c67e8793fb7a8e220ef23e45efd0b332`：
  - `doctor --bench`：加载 9.38s，生成 26.07s，RTF 7.24。
  - `say --design`：音频 2.48s，RTF 7.4。
  - `say --voice @clone`：音频 1.36s，RTF 18.4（短句，固定开销占主导）。
  - `say --voice @clone --direction`：音频 2.80s，RTF 7.5。
  - 三个模板均已实际跑过：`tts_instruction`、`ref_clone_tata`、
    `ref_edit_tata`。

上游运行时会打印缺少 SoX 和 flash-attn 的非致命警告。eager MPS 路径不需要这两者。

## PRD 未规定事项的决策

1. **Resume 校验。** 当渲染后的 instruction、design 模板、cover 台词或当前
   model digest 发生变化时，拒绝续跑。每个候选 recipe 还会与 plan 和 manifest
   逐字段核对。已有 run 的 seeds/cfgs 从 `run.json` 复用；带 `--resume` 传入
   新的 `--seeds`/`--cfgs` 不会改写矩阵。这样每个候选都保持可复现。
2. **Run manifest。** `run.json` 保存编译后的 design 快照、矩阵、每个候选的
   plan、model digest 和 completed ID。`design.yaml` 也会复制进 run 目录，
   作为自包含记录。
3. **Model digest。** 对 `config.json`、generation/index 元数据、
   audio-tokenizer config 以及分片名称/大小做 SHA-256，而不是对 7 GB 权重
   做哈希。
4. **Recipe。** 每个 WAV 旁边有 `<stem>.recipe.json` sidecar。路径只写
   basename，不写绝对路径；recipe 携带文本/音频/profile 哈希、cfg、seed、
   template、model digest 和 warnings。
5. **Voice ID。** `v_` + 10 位 Crockford 时间戳 + 16 位随机字符，使 profile
   按创建时间排序且保持唯一。
6. **Clone 默认值。** cfg 1.0；`say` 的 design 路由使用 cfg 4.0，已固化的
   design 使用 recipe 中的值。Direction 是一次性的，不写入 profile。
7. **参考音频策略。** clone 参考建议 6–20 秒；更短会警告，超过 30 秒需要
   `--allow-long-ref`；内联事件一律拒绝，因为模型会把它念出来。
8. **截断。** `finish_reason == "length"` 或带截断 warning 的候选不能固化。
9. **设备策略。** 自动选择要求 MPS；`--device cpu` 只在显式传入时允许
   （用于调试），绝不作为静默回退。
10. **原子性。** WAV/JSON 写入走临时文件 + `fsync` + `os.replace`；
    只有文件提交完成后才开始播放。
11. **退出码。** 2 用法/lint，3 引擎，4 播放，5 完整性。

## 审核修复

两轮独立的 Codex 审核均以 FAIL 结束。第一轮产生 M1–M7；第二轮确认这些已关闭，
并产生 M8–M9。所有 Must-fix 以及 Should-fix 1/2/3/9 均已修复，每项都有回归测试。
最终定向复核返回 PASS。

| 项目 | 修复 | 回归测试 |
|---|---|---|
| M1 | `ReferenceBlock.audio/transcript` 只接受纯 basename。`VoiceStore` 用 `strict=True` 解析两者，并拒绝 voice 目录之外的任何目标，包括 symlink。`save()` 写入前执行同样检查。 | `test_voice_security.py::test_reference_block_rejects_unsafe_paths`、`::test_say_rejects_escaped_reference_value`、`::test_say_rejects_symlink_escape` |
| M2 | `take_id` 必须匹配 `^c[0-9]{3,}$`，在 `CandidatePlan` 和 `take_wav_path()` 中双重校验；解析后的路径必须位于 `run_dir/candidates` 下。report、freeze 和 resume 都使用该函数。 | `test_run_security.py::test_take_wav_path_rejects_traversal`、`::test_freeze_rejects_traversal_take_id` |
| M3 | resume 比较当前元数据 digest 与 `manifest.model.digest`，拒绝混合模型的 run。`candidate_status()` 现在校验 take id、run id、输出文件名、kind、模型模板、instruction、语言、seed、cfg、文本哈希、model id/digest/protocol 和采样率。freeze 的 provenance 使用该候选 recipe 的 digest。 | `test_resume.py::test_resume_refuses_changed_model_digest`、`::test_resume_regenerates_tampered_recipe`、`test_freeze.py::test_freeze_as_design` |
| M4 | clone freeze 无论是否带 `--confirm-transcript`，都会打印完整源句、字符数和音频时长。 | `test_freeze.py::test_freeze_as_clone`、`::test_freeze_as_clone_requires_confirmation` |
| M5 | 括号 token 检测取代有限的动词词表。扫描所有 `[ ] ( ) （ ） 【 】` token；按事件词做启发式匹配，空括号也拒绝。应用于 lint、`--direction`、clone 转写文本和 `voice add`。 | `test_design.py::test_inline_event_detection_covers_all_bracket_styles`、`::test_inline_event_detection_ignores_plain_text`、`test_voice_security.py::test_voice_add_rejects_bracket_events`、`test_say_routes.py::test_say_direction_rejects_inline_event` |
| M6 | `say --design` 与 explore 执行相同的 compile + blocking lint，并在加载引擎前以 exit 2 和 lint 提示退出。 | `test_say_routes.py::test_say_design_rejects_blocking_lint_event`、`::test_say_design_rejects_no_anchors` |
| M7 | `with_error_handling` 的错误改写到 stderr。`doctor --bench --json` 的 stdout 只写一个 JSON 文档，失败信息放在 `bench.error`，exit 3。 | `test_cli.py::test_doctor_bench_fails_clearly_without_model` |
| S1 | 所有 cfg 入口都要求有限值且位于 `(0, 20]`：CLI、`GenerationRequest`、run 矩阵、recipe 和 profile 模型。`json_dumps()` 使用 `allow_nan=False`。 | `test_say_routes.py::test_say_rejects_invalid_cfg`、`::test_generation_request_and_json_reject_non_finite_values`、`test_voice_security.py::test_profile_blocks_reject_non_finite_cfg` |
| S2 | `design report --gap` 在写出任何文件前要求有限值且位于 `[0, 30]`；`concat_with_gap()` 也做范围兜底。 | `test_design.py::test_report_rejects_invalid_gap` |
| S3 | `--wide` 要求至少两个 cover 台词，否则 exit 2 并给出提示。 | `test_design.py::test_wide_requires_two_covers` |
| M8 | `doctor --bench --json` 用 `contextlib.redirect_stdout(sys.stderr)` 包住 `_run_bench()`，使 vendored 模型的 print、transformers 警告和 SoX 消息不会进入 stdout。成功和失败路径的 stdout 都只有一个严格 JSON 文档；诊断信息保留在 stderr。已用真实 checkpoint 验证（`json.load` 成功）。 | `test_cli.py::test_doctor_bench_json_success_keeps_stdout_clean`（fake engine 在 load 和 generate 时打印），以及原有的缺失模型失败测试 |
| M9 | 新增共享的 `generate_or_raise_engine_error()`，保留 `BttsError`/`KeyboardInterrupt`，把其他生成异常映射为 `EngineError`。`say` 与 `design explore` 都使用它；失败 exit 3，stderr 包含可读的阶段和底层错误，无 traceback，不产生半成品 WAV/recipe。失败的 explore 候选不会加入 `completed`。 | `test_engine_errors.py::test_say_generation_failure_exits_engine_error`、`::test_explore_generation_failure_exits_engine_error`、`::test_generation_helper_preserves_domain_and_interrupt_errors` |
| S9 | README 快速开始使用 `c001`（默认 4 候选矩阵）。从本文档删除了不存在的 `design lint --json` 说法。 | 仅文档 |

## 遗留工作

### P0.1 —— 审核后续项（非阻塞，未包含在 `fb48022` 中）

1. **`VoiceStore.save()` 不是目录级原子替换** ——
   `src/btts_cli/domain/voice.py:249`。文件逐个原子写入，`profile.json` 最后
   写，因此崩溃可能留下混合目录；`--force` 在 design↔clone 之间切换时会残留
   旧文件（`design.yaml` 或 `ref.wav`/`ref.txt`）。建议先构建同层临时目录、
   fsync，再 rename 覆盖旧目录；或显式删除与目标 kind 冲突的文件。补充
   design→clone→design 的 `--force` 测试。
2. **`interrupted` 不会重置** —— `src/btts_cli/commands/design.py:362`。
   中断的 run 续跑完成后，`run.json.interrupted` 仍为 `true`，与报告不一致。
   在没有 pending 候选时置为 `false`，并在最终写 manifest 时统一归一化。
3. **`BTTS_RUN_MPS=1` 仍会 skip 而不是失败** ——
   `tests/test_mps_integration.py:34,37`。显式开启时，如果 MPS 或模型文件缺失，
   应调用 `pytest.fail()` 并给出实际模型目录；只有未设置该变量时才 skip。
4. **wheel 测试可以更严格** —— `tests/test_wheel_contents.py:41,48`。
   目前只断言 vendored LICENSE，没有断言 dist-info 根 LICENSE 或
   `THIRD_PARTY_NOTICES.md`；只禁止 `.bin`，没有禁止 `.pt/.pth/.ckpt/.wav`。
   “不导入 torch”的断言仍运行在已安装 torch 的解释器中；用 import blocker
   能更真实地模拟基础安装。
5. **Vendor 同步脚本硬编码 NOTICE 缺失** ——
   `scripts/sync_breeze_vendor.py:136,227`。该 vendored commit 确实没有
   `NOTICE`，但未来升级时应探测 `NOTICE*`；上游 LICENSE 缺失时，目前会写出
   空 LICENSE 和空哈希，而不是直接失败。
6. **Model digest 不是权重内容哈希** —— `src/btts_cli/engine/base.py:176`。
   它覆盖元数据、index/config 和分片名称/大小，因此同尺寸替换权重无法检测。
   这是已记录的 P0 取舍；按分片或全量权重哈希属于 P1。
7. **WAV 与 recipe 不是单个事务** —— 两者各自原子写，但不是成对事务。二者之间
   崩溃会留下没有 sidecar 的 WAV，resume 会将其视为 corrupt 并重新生成。
   可以接受，但文档不应声称这是单一原子操作。

### P1/P2 —— 产品范围

- 批量/长文本合成、流式、HTTP API、模型下载、音色删除。
- 常驻 MPS worker，用来摊薄约 7–9 秒的模型加载（P0 有意逐命令加载；实测加载
  时间对交互式使用已经足够快）。
- 审美与相似度评估；当前报告只做描述性指标。
- 完整指标依赖可选的 `librosa`/`parselmouth`；未安装时只计算基础
  numpy/soundfile 指标，并给出明确提示。
- Voice-add 的授权元数据（schema 中已有 `profile.consent`，但始终为 `null`；
  PRD 尚未定义对应 flag）。

### 已知限制

- MPS 性能只在本机测量；RTF 会随句子长度、热状态和内存压力变化。
- 少于 6 秒的参考音频克隆稳定性会下降；CLI 只警告，不拒绝。
- Vendoring 固定在一个上游 commit，升级必须手动重跑
  （`scripts/sync_breeze_vendor.py --ref <sha>`）。
- `design lint` 是启发式的；不常见但有效的描述可能触发警告，lint 干净也不保证
  音频好听。
- 括号事件检测偏保守：含事件词但其实是正常括号补充说明的内容会被拒绝，以避免
  模型把它念出来。
- CLI 无法判断一个设计“好不好听”；候选选择仍然由人决定。

## 许可证

CLI 代码采用 Apache-2.0。模型权重和自托管生成结果受 BreezeBlue Research and
Non-Commercial License 1.1 约束。`doctor`、`voice add`、`say`、音色 profile
目录和 README 都会显示该声明。
