# Implementation notes — btts_cli P0

Scope: PRD v0.2 sections 3–9 (P0). P1/P2 features are intentionally absent.

## Status (2026-11-03)

- P0 is implemented and committed as `fb48022 feat: implement btts_cli P0`
  (parent commit `4b849a4 docs: add btts_cli PRD v0.2`).
- Independent review by Codex ran twice:
  - Round 1: **FAIL** — 7 Must-fix items (path traversal, take-id traversal,
    resume mixing models, clone transcript display, bracket events, `say`
    lint bypass, `doctor --json` stdout).
  - Round 2: **FAIL** — the original 7 were confirmed closed, but M8
    (`doctor --bench --json` polluted by vendored prints) and M9 (generation
    exceptions escaping as exit 1) were found.
  - Final targeted re-check after M8/M9 fixes: **PASS**.
- Current verification: `ruff check .` clean; `pytest -q` **85 passed,
  1 skipped**; `BTTS_RUN_MPS=1 pytest -m mps` **1 passed** on this machine;
  real `doctor --bench --json` stdout parses as strict JSON.
- No git remote is configured; the commits exist only in the local repository.

## Environment

- Interpreter: `../breeze-tts/.venv/bin/python` (Python 3.12.11)
- Installed stack: torch 2.9.1, transformers 4.57.3, qwen-tts 0.1.1
- Model: `../breeze-tts/ms-models/Breeze-TTS-2` (never copied into this repo)
- `../breeze-tts` source was not modified.

The venv is uv-managed and has no `pip`, so the equivalent of
`pip install -e ".[dev]"` was run as:

```bash
uv pip install --python ../breeze-tts/.venv/bin/python -e ".[dev]"
```

## Implemented scope

| Command | Behaviour |
|---|---|
| `doctor [--bench] [--json] [--model] [--device]` | Environment, model files/digest, torch/MPS, memory, optional real bench, license. Exits 0 for plain checks; 3 when `--bench` fails. |
| `design init` | Writes an editable v1 design YAML from the template registry (6 templates). `--force` to overwrite. |
| `design lint` | Five dimensions, block/warn rules, `--show-rendered`. Exit 1 on lint blocks, 2 on schema/usage errors. |
| `design explore` | Default 4 takes (2 seeds × 2 cfgs), `--wide` 8. Explicit `--seeds`/`--cfgs`. Matrices >16 require `--yes`. Atomic WAV then recipe, manifest updated after each take. SIGINT stops after the current take. `--resume` only fills missing/hash-mismatched takes. |
| `design report` | `metrics.csv`, `report.md`, `all_candidates.wav`, `audition.html`; toggles `--csv/--no-csv`, `--html/--no-html`, `--reel/--no-reel`. |
| `design freeze` | Design profile from a take, or clone profile with `--as clone --confirm-transcript` (`--force`, `--allow-long-ref`). Rejects hash mismatch, truncated takes, inline events, and short/long references per policy. |
| `voice add/list/show` | Clone profiles store `ref.wav` + exact `ref.txt` + preview. Writes `profile.json`, `LICENSE.txt`; hashes are verified on replay. |
| `say` | Exactly one route: `--design`, `--voice @clone`, or `--voice @clone --direction`. Writes WAV, then recipe, then optionally plays with `afplay`. |
| Metrics | Basic (numpy/soundfile): duration, dBFS, clipping, silence, abnormal length. Full adds F0/HNR/centroid when librosa/parselmouth exist; otherwise degrades with an informational warning. |
| Vendoring | Exactly 12 upstream files at `daa6cb2e…`, import rewriting, provenance headers, `vendor.lock.json`, `THIRD_PARTY_NOTICES.md`, upstream Apache-2.0 LICENSE. |

`btts_cli --help` and every non-inference command start without importing
torch. Only `btts_cli.engine.mps` imports torch, inside `load_engine` at
generation time.

## Verification commands

```bash
# style + unit tests (no MPS)
../breeze-tts/.venv/bin/python -m ruff check .
../breeze-tts/.venv/bin/python -m pytest -q

# vendor drift
../breeze-tts/.venv/bin/python scripts/sync_breeze_vendor.py --check

# real MPS integration test (explicit opt-in)
BTTS_RUN_MPS=1 \
BTTS_MODEL_DIR=../breeze-tts/ms-models/Breeze-TTS-2 \
../breeze-tts/.venv/bin/python -m pytest -q -m mps
```

## Test results

- `ruff check .`: all checks passed.
- Default `pytest -q`: **85 passed, 1 skipped** (MPS test skipped).
- `BTTS_RUN_MPS=1 pytest -q -m mps`: **1 passed** in ~46–53s (repeat runs).
- Wheel test builds a real wheel and asserts no `*.safetensors`, no
  `audio_tokenizer/`, no `ms-models/`, and that the extracted wheel starts
  `btts_cli --help` without importing torch.
- Real MPS CLI smoke (2026-11-03), model digest
  `sha256:3ed2200577065bf5a5d33aece744cf56c67e8793fb7a8e220ef23e45efd0b332`:
  - `doctor --bench`: load 9.38s, generate 26.07s, RTF 7.24.
  - `say --design`: 2.48s audio, RTF 7.4.
  - `say --voice @clone`: 1.36s audio, RTF 18.4 (short line; fixed overhead dominates).
  - `say --voice @clone --direction`: 2.80s audio, RTF 7.5.
  - All three templates were exercised: `tts_instruction`, `ref_clone_tata`,
    `ref_edit_tata`.

The upstream runtime prints non-fatal warnings about missing SoX and
flash-attn. Neither is required by the eager MPS path.

## Decisions where the PRD was silent

1. **Resume validation.** A resumed run is rejected when the rendered
   instruction, design template, cover sentences, or current model digest
   changed. Every candidate recipe is also checked field-by-field against its
   plan and the manifest. Seeds/cfgs of an existing run are reused from
   `run.json`; passing new `--seeds`/`--cfgs` with `--resume` does not rewrite
   the matrix. This keeps every take reproducible.
2. **Run manifest.** `run.json` stores the compiled design snapshot, matrix,
   per-take plan, model digest, and completed IDs. `design.yaml` is also
   copied into the run directory for a self-contained record.
3. **Model digest.** SHA-256 over `config.json`, generation/index metadata,
   audio-tokenizer config and shard names/sizes — not a 7 GB weight hash.
4. **Recipes.** Sidecar `<stem>.recipe.json` next to every WAV. Paths are
   basenames, never absolute; the recipe carries text/audio/profile hashes,
   cfg, seed, template, model digest, and warnings.
5. **Voice IDs.** `v_` + 10-char Crockford timestamp + 16 random characters,
   so profiles sort by creation time and remain unique.
6. **Clone defaults.** cfg 1.0; design uses cfg 4.0 for `say` and the
   frozen recipe values for frozen designs. Direction is one-off and is not
   persisted into the profile.
7. **Reference policy.** Clone references recommend 6–20s; shorter files warn,
   files over 30s require `--allow-long-ref`; inline events are rejected
   outright because the model would speak them.
8. **Truncation.** A take with `finish_reason == "length"` or a truncation
   warning cannot be frozen.
9. **Device policy.** Auto-selection requires MPS; `--device cpu` is allowed
   only when passed explicitly (debugging), never as a silent fallback.
10. **Atomicity.** WAV/JSON writes go through a temp file + `fsync` +
    `os.replace`; playback starts only after the file is committed.
11. **Exit codes.** 2 usage/lint, 3 engine, 4 playback, 5 integrity.

## Review fixes

Two independent Codex review rounds ended in FAIL. Round 1 produced M1–M7;
round 2 confirmed those closed and produced M8–M9. All Must-fix items and
Should-fix 1/2/3/9 are fixed, each with regression tests. The final targeted
re-check returned PASS.

| Item | Fix | Regression tests |
|---|---|---|
| M1 | `ReferenceBlock.audio/transcript` only accept plain basenames. `VoiceStore` resolves both with `strict=True` and rejects anything outside the voice directory, including symlinks. `save()` applies the same checks before writing. | `test_voice_security.py::test_reference_block_rejects_unsafe_paths`, `::test_say_rejects_escaped_reference_value`, `::test_say_rejects_symlink_escape` |
| M2 | `take_id` must match `^c[0-9]{3,}$`, validated in `CandidatePlan` and again in `take_wav_path()`; the resolved path must stay under `run_dir/candidates`. Report, freeze and resume all use this function. | `test_run_security.py::test_take_wav_path_rejects_traversal`, `::test_freeze_rejects_traversal_take_id` |
| M3 | Resume compares the current metadata digest with `manifest.model.digest` and refuses mixed-model runs. `candidate_status()` now verifies take id, run id, output filename, kind, model template, instruction, language, seed, cfg, text hash, model id/digest/protocol and sample rate. Freeze provenance uses the take recipe's digest. | `test_resume.py::test_resume_refuses_changed_model_digest`, `::test_resume_regenerates_tampered_recipe`, `test_freeze.py::test_freeze_as_design` |
| M4 | Clone freeze always prints the full source sentence plus character count and audio duration, both with and without `--confirm-transcript`. | `test_freeze.py::test_freeze_as_clone`, `::test_freeze_as_clone_requires_confirmation` |
| M5 | Bracket-token detection replaces the finite verb list. All `[ ] ( ) （ ） 【 】` tokens are scanned; event terms are matched heuristically and empty brackets are rejected. Applied to lint, `--direction`, clone transcripts and `voice add`. | `test_design.py::test_inline_event_detection_covers_all_bracket_styles`, `::test_inline_event_detection_ignores_plain_text`, `test_voice_security.py::test_voice_add_rejects_bracket_events`, `test_say_routes.py::test_say_direction_rejects_inline_event` |
| M6 | `say --design` runs the same compile + blocking lint as explore and exits 2 with a lint hint before loading the engine. | `test_say_routes.py::test_say_design_rejects_blocking_lint_event`, `::test_say_design_rejects_no_anchors` |
| M7 | Errors from `with_error_handling` go to stderr. `doctor --bench --json` writes exactly one JSON document to stdout, with the failure in `bench.error`, and exits 3. | `test_cli.py::test_doctor_bench_fails_clearly_without_model` |
| S1 | Every cfg entry is finite and in `(0, 20]`: CLI, `GenerationRequest`, run matrix, recipe and profile models. `json_dumps()` uses `allow_nan=False`. | `test_say_routes.py::test_say_rejects_invalid_cfg`, `::test_generation_request_and_json_reject_non_finite_values`, `test_voice_security.py::test_profile_blocks_reject_non_finite_cfg` |
| S2 | `design report --gap` requires a finite value in `[0, 30]` before any output is written; `concat_with_gap()` also guards the range. | `test_design.py::test_report_rejects_invalid_gap` |
| S3 | `--wide` requires at least two cover scripts and exits 2 with a hint otherwise. | `test_design.py::test_wide_requires_two_covers` |
| M8 | `doctor --bench --json` wraps `_run_bench()` in `contextlib.redirect_stdout(sys.stderr)` so vendored model prints, transformers warnings and SoX messages never reach stdout. Stdout is one strict JSON document on both success and failure; diagnostics remain on stderr. Verified against the real checkpoint (`json.load` succeeds). | `test_cli.py::test_doctor_bench_json_success_keeps_stdout_clean` (fake engine prints during load and generate), plus the existing missing-model failure test |
| M9 | New shared `generate_or_raise_engine_error()` preserves `BttsError`/`KeyboardInterrupt` and maps other generation exceptions to `EngineError`. Both `say` and `design explore` use it; failures exit 3 with a readable stage + underlying error, no traceback, and no partial WAV/recipe. A failed explore take is never added to `completed`. | `test_engine_errors.py::test_say_generation_failure_exits_engine_error`, `::test_explore_generation_failure_exits_engine_error`, `::test_generation_helper_preserves_domain_and_interrupt_errors` |
| S9 | README quick start uses `c001` (default 4-take matrix). Removed the false `design lint --json` claim from these notes. | Documentation only |

## Remaining work

### P0.1 — review follow-ups (non-blocking, not in `fb48022`)

1. **`VoiceStore.save()` is not a directory-level atomic replace** —
   `src/btts_cli/domain/voice.py:249`. Files are written atomically one by one
   with `profile.json` last, so a crash can leave a mixed directory, and
   `--force` switching design↔clone leaves stale files (`design.yaml` or
   `ref.wav`/`ref.txt`). Build a sibling temporary directory, fsync, then
   rename it over the old one, or explicitly delete files that conflict with
   the target kind. Add design→clone→design `--force` tests.
2. **`interrupted` never resets** — `src/btts_cli/commands/design.py:362`.
   After an interrupted run is resumed to completion, `run.json.interrupted`
   stays `true` and disagrees with the report. Set it to `false` when no
   pending takes remain, normalized on the final manifest write.
3. **`BTTS_RUN_MPS=1` still skips instead of failing** —
   `tests/test_mps_integration.py:34,37`. Explicit opt-in should `pytest.fail()`
   with the actual model directory when MPS or model files are missing; only
   the unset case should skip.
4. **Wheel test can be stronger** — `tests/test_wheel_contents.py:41,48`.
   It asserts the vendored LICENSE but not the dist-info root LICENSE or
   `THIRD_PARTY_NOTICES.md`, and forbids `.bin` but not `.pt/.pth/.ckpt/.wav`.
   The "no torch" assertion still runs in an interpreter where torch is
   installed; an import blocker would better simulate a base install.
5. **Vendor sync hard-codes NOTICE absence** —
   `scripts/sync_breeze_vendor.py:136,230`. The vendored commit has no
   `NOTICE`, but a future upgrade should detect `NOTICE*`; a missing upstream
   LICENSE currently writes an empty LICENSE and hash instead of failing.
6. **Model digest is not a weight-content hash** —
   `src/btts_cli/engine/base.py:176`. It covers metadata, index/config and
   shard names/sizes, so a same-size weight swap is undetectable. Documented
   P0 trade-off; per-shard or full weight hashing is P1.
7. **WAV and recipe are not one transaction** — each write is atomic, but the
   pair is not. A crash between them leaves a WAV without a sidecar, which
   resume treats as corrupt and regenerates. Acceptable, but the docs should
   not claim a single atomic operation.

### P1/P2 — product scope

- Batch/long-form synthesis, streaming, HTTP API, model download, voice
  deletion.
- A persistent MPS worker to amortize the ~7–9s model load (P0 deliberately
  loads per command; measured load is fast enough for interactive use).
- Aesthetic and likeness evaluation; the current report is descriptive only.
- Full metrics require optional `librosa`/`parselmouth`; without them only
  basic numpy/soundfile metrics are computed, with an explicit notice.
- Voice-add consent metadata (`profile.consent` exists in the schema but is
  always `null`; PRD does not yet define the flag).

### Known limits

- MPS performance is measured on this machine only; RTF varies with line
  length, thermal state and memory pressure.
- Clone stability for references under 6s is reduced; the CLI warns rather
  than refusing.
- Vendoring is pinned to one upstream commit and must be re-run manually for
  upgrades (`scripts/sync_breeze_vendor.py --ref <sha>`).
- `design lint` is heuristic; unusual but valid descriptors can trigger a
  warning, and a clean lint is not a guarantee of good audio.
- Bracket-event detection is conservative: a legitimate bracketed aside that
  contains an event word is rejected rather than risk the model speaking it.
- The CLI cannot judge whether a design "sounds good"; candidate selection
  remains a human decision.

## License

CLI code is Apache-2.0. Model weights and self-hosted output are covered by
the BreezeBlue Research and Non-Commercial License 1.1. `doctor`, `voice add`,
`say`, the voice profile directory, and the README all surface this notice.
