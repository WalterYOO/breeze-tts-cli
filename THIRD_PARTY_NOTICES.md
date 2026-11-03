# 第三方声明

## Breeze TTS 2 推理源码

- 上游：https://github.com/breezeblue-ai/breeze-tts
- Vendored commit：`daa6cb2efe5471f09369229f322d20bb956cb3c3`
- Vendored 位置：`src/btts_cli/_vendor/breeze_tts/`
- 上游许可证：Apache License 2.0，原文完整复制到
  `src/btts_cli/_vendor/breeze_tts/LICENSE`。
- 上游 `NOTICE` 文件：该 vendored commit 中不存在。

### 复制的文件

| 上游路径 | 源码 SHA-256 | Vendored SHA-256 |
|---|---|---|
| `breeze_infer/__init__.py` | `810141b5e6326542...` | `a1810d5cdbfa0e2d...` |
| `breeze_infer/runtime.py` | `a257d868b867009e...` | `6963cf7cdc998246...` |
| `breeze_infer/audio.py` | `3dc7977d8b1f3097...` | `c2e3fc424d329fc1...` |
| `breeze_infer/templates.py` | `52057796b74b95e3...` | `f3e3a6cd96daa5d2...` |
| `models/__init__.py` | `e3b0c44298fc1c14...` | `cde2c7f14643509c...` |
| `models/breeze.py` | `9c408b2323f1fb57...` | `76f48bb90ba99bd5...` |
| `models/breeze_config.py` | `f37fc58792ea936d...` | `2c1e136a70eae3b9...` |
| `models/breeze_base_config.py` | `6b8aab8979fef271...` | `e045e8fd9af993d5...` |
| `models/breeze_backbone_factory.py` | `8d8519dccb82f536...` | `827fabf8e9d1e0f8...` |
| `models/generation_breeze.py` | `fca4c2d134ab1438...` | `616c373226e6b75a...` |
| `models/logits_process.py` | `43bbcc1c85da535a...` | `63682e57478e1a44...` |
| `models/t5gemma2_compat.py` | `5902c67fdd3ca28b...` | `510fb03d4cbb4a77...` |
| `LICENSE` | `c8858a5a76440bbc...` | `c8858a5a76440bbc...` |

### 修改

1. 所有以 `models` 或 `breeze_infer` 为根的绝对 import 都改写为
   `btts_cli._vendor.breeze_tts.models` / `btts_cli._vendor.breeze_tts.breeze_infer`，避免安装上游
   通用的顶层包名。
2. 每个 vendored 文件顶部都加入两行来源头；其下保留原始版权和许可证头。
3. 未做行为性代码修改。仅 CUDA 的模块
   (`breeze_infer.api`, `breeze_infer.batch`, `models.fast_streaming`,
   `models.cudagraph`, `models.stream_runtime`, `models.text_encoder_graph`,
   `models.warmup_profile`) 有意未复制。

### 模型权重

模型权重、`audio_tokenizer/` codec 权重和 `qwen-tts` **不会**被 vendored。
它们在运行时从 `--model` 或 `BTTS_MODEL_DIR` 指定的模型目录加载，受
BreezeBlue Research and Non-Commercial License 约束。许可证声明见 `README.md`。

## Python 依赖

运行时依赖列在 `pyproject.toml` 中。各依赖保留自己的许可证；本项目不再分发它们。
