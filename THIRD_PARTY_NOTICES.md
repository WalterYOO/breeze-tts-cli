# Third-party notices

## Breeze TTS 2 inference sources

- Upstream: https://github.com/breezeblue-ai/breeze-tts
- Vendored at commit: `daa6cb2efe5471f09369229f322d20bb956cb3c3`
- Vendor location: `src/btts_cli/_vendor/breeze_tts/`
- Upstream license: Apache License 2.0, copied verbatim to
  `src/btts_cli/_vendor/breeze_tts/LICENSE`.
- Upstream `NOTICE` file: not present at the vendored commit.

### Copied files

| Upstream path | Source SHA-256 | Vendored SHA-256 |
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

### Modifications

1. All absolute imports rooted at `models` or `breeze_infer` were rewritten to
   `btts_cli._vendor.breeze_tts.models` / `btts_cli._vendor.breeze_tts.breeze_infer` so the upstream
   generic top-level package names are never installed.
2. A two-line provenance header was prepended to every vendored file. Original
   copyright and license headers are preserved below it.
3. No behavioural code changes were made. CUDA-only modules
   (`breeze_infer.api`, `breeze_infer.batch`, `models.fast_streaming`,
   `models.cudagraph`, `models.stream_runtime`, `models.text_encoder_graph`,
   `models.warmup_profile`) are intentionally not copied.

### Model weights

Model weights, `audio_tokenizer/` codec weights and `qwen-tts` are **not**
vendored. They are loaded at runtime from a model directory supplied with
`--model` or `BTTS_MODEL_DIR`, under the BreezeBlue Research and
Non-Commercial License. See `README.md` for the license notice.

## Python dependencies

Runtime dependencies are listed in `pyproject.toml`. They retain their own
licenses; this project does not redistribute them.
