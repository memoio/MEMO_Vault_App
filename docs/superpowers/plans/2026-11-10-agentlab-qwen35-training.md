# AgentLab Qwen3.5 Training and Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add AgentLab-specific Qwen3.5 base/adapter serving, a real AgentLab–BrowserGym integration check, paired base-versus-adapter evaluation on the same held-out Mind2Web test set, and non-truncating LLaMA-Factory QLoRA smoke/scale/full launchers with reproducible commands in the AgentLab plan.

**Architecture:** Keep LLaMA-Factory 0.9.5 as the trainer and AgentLab + BrowserGym as the runtime path. The local-page action/feedback loop is an integration smoke, not the final Mind2Web score. The held-out evaluation sends identical test prompts to the base model and the LoRA model in separate runs, then compares decision-level action metrics by task ID. Validate all train/validation token lengths before launching; preserve rows, reject cutoff overflow, mark the current short run as a technical probe, and require `training_ready=true` for scale/full. Run AgentLab requests through a loopback compatibility proxy that maps `max_completion_tokens` to LLaMA-Factory's `max_tokens`.

**Tech Stack:** Python 3.12, pytest, pinned AgentLab / BrowserGym, LLaMA-Factory 0.9.5, Qwen3.5-0.8B, Bash.

**Spec:** `docs/superpowers/specs/2026-11-10-agentlab-qwen35-training-design.zh-CN.md`

## Global Constraints

- Qwen revision: `2fc06364715b967f1860aea9cf38778875588b17`.
- LLaMA-Factory: 0.9.5, commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`.
- AgentLab commit: `8488c564eea0525aaff5dea189bb03a9b529546e`.
- Preserve short data counts 200/28/23 and full counts 7,464/670/926; never silently filter or truncate.
- Use short `cutoff_len=131072` and full `cutoff_len=262144`; fail before training when any train/validation sample exceeds the configured cutoff.
- Register only train and validation in trainer configs. Never use test during training or validation.
- Unreviewed short data is allowed only with an explicit smoke probe flag and `probe_only=true`; scale/full require `training_ready=true`.
- All serving and compatibility endpoints bind to `127.0.0.1`; do not run QLoRA on the current 4 GB host.

## Review Focus

- A modified report, data file, model file, or manifest must fail before training; cover with hash and manifest validation tests in Task 2.
- One train or validation sample above cutoff must fail without truncation or row deletion; cover with boundary tests in Task 2.
- `training_ready=false` must allow only an explicitly requested smoke probe; cover readiness-mode matrix in Task 2.
- AgentLab's `max_completion_tokens` must be translated without mutating other request fields; cover in Task 1.
- Base and adapter runs must not overwrite prior results and must send real model output through AgentLab parsing and BrowserGym; cover path and action validation in Task 1.
- Held-out evaluation must use the same test manifest and generation settings for base and adapter; cover ID alignment, output integrity, and action metric calculations in Task 4.

---

### Task 1: AgentLab API compatibility and real BrowserGym closed loop

**Files:**
- Create: `src/everos/agent_training/agentlab_api_compat.py`
- Create: `src/everos/agent_training/agentlab_qwen_loop.py`
- Create: `scripts/serve_agentlab_lf_api_proxy.sh`
- Create: `scripts/serve_agentlab_qwen35.sh`
- Create: `scripts/agentlab_qwen_loop.py`
- Create: `scripts/run_agentlab_base_baseline.sh`
- Create: `tests/unit/test_agent_training/test_agentlab_api_compat.py`
- Create: `tests/unit/test_agent_training/test_agentlab_qwen_loop.py`
- Modify: `docs/superpowers/plans/2026-11-09-vault-agentlab-qwen35.zh-CN.md` section 10.5 and add section 12.

**Interfaces:**
- `confirm_bid_from_observation(obs: dict) -> str` returns the unique visible BrowserGym ID for the Confirm button.
- `is_expected_call(source: str, function_name: str, argument: str) -> bool` parses a single AST call and validates its function and literal argument without executing model-generated code.
- `translate_completion_request(payload: dict) -> dict` removes `max_completion_tokens`, uses it as `max_tokens` only when `max_tokens` is absent, and preserves all other keys.
- The proxy only forwards `GET /v1/models` and non-streaming `POST /v1/chat/completions`; it binds to loopback and accepts only a loopback upstream.
- The loop CLI reads `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `AL_MODEL_NAME`, and `AL_LOOP_DIR`; it writes one non-overwriting JSON result with base/adapter model ID, model response, action, browser feedback, and pass/fail.

- [x] **Step 1: Write failing tests** for completion parameter translation (precedence and immutability), unsupported streaming/path rejection, unique `Confirm` bid selection, and strict accepted action parsing.
- [x] **Step 2: Run tests and confirm the expected missing-module or missing-function failures.**
- [x] **Step 3: Implement the loopback proxy and pure AgentLab loop helpers.** The loop uses pinned AgentLab `OpenAIModelArgs` and a custom `bgym.Agent` that parses published ADP JSON actions; `MostBasicAgent` expects a code block, so it cannot consume this training target directly. The local in-memory page requires two clicks, with the updated page observation fed into the second model request.
- [x] **Step 4: Add model-serving and baseline shell wrappers.** Verify LLaMA-Factory version and fixed model hashes; support base and adapter modes; use separate upstream and proxy ports; fail if result paths already exist.
- [x] **Step 5: Run focused tests and scripted integration checks.** The AgentLab unit suite passed, the existing scripted `MostBasicAgent` smoke passed, and the new custom JSON Agent completed a two-action BrowserGym fixture run through a temporary OpenAI-compatible endpoint. This validates API/action/observation wiring, not real Qwen behavior. Complete model weights are unavailable locally, so no real Qwen loop is claimed.

### Task 2: AgentLab-specific QLoRA runner and short/scale/full wrappers

**Files:**
- Create: `src/everos/agent_training/agentlab_qlora.py`
- Create: `scripts/train_agentlab_qlora_smoke.sh`
- Create: `scripts/train_agentlab_qlora_scale.sh`
- Create: `scripts/train_agentlab_qlora_full.sh`
- Create: `tests/unit/test_agent_training/test_agentlab_qlora.py`
- Modify: `docs/superpowers/plans/2026-11-09-vault-agentlab-qwen35.zh-CN.md` section 12.

**Interfaces:**
- CLI: `python -m everos.agent_training.agentlab_qlora --mode {smoke,scale,full} --data-dir PATH --model PATH --run-dir PATH --cutoff-len INT [--probe-unverified-data] [--prepare-only]`.
- Smoke wrapper defaults to the exact short data directory and cutoff 131072; scale/full wrappers accept reviewed data and cutoff 262144. All wrappers pass through optional step, epoch, eval, and gradient-accumulation values.
- `validate_data(data_dir, mode, cutoff_len, probe_unverified_data) -> dict` checks report hashes, expected source counts, manifest ordering and coverage, token lengths, model revision/template, and readiness; it must never rewrite data.
- Training config uses `dataset=agentlab_train`, `eval_dataset=agentlab_validation` only when eval is requested, `template=qwen3_5_nothink`, QLoRA NF4, and gradient checkpointing.

- [x] **Step 1: Write failing unit tests** for exact short/full counts, test exclusion, all-row manifest coverage, cutoff equality/overflow, tampered hashes, smoke probe readiness, scale/full readiness rejection, and non-overwriting run directories.
- [x] **Step 2: Run the focused tests and confirm they fail for the missing runner.**
- [x] **Step 3: Implement a pure validation/config layer.** Check every train/validation row before constructing a config; reject any over-cutoff row, do not expose an allow-truncation escape hatch, and keep `test.jsonl` out of the config.
- [x] **Step 4: Implement run modes and shell wrappers.** Smoke uses all 200 train and 28 validation rows with `probe_only=true` only when explicitly requested; scale uses 50–100 steps and full uses the requested epoch count. Both scale/full reject `training_ready=false` data.
- [ ] **Step 5: Run focused tests and smoke prepare-only.** Focused QLoRA tests pass, but prepare-only was not run because the local model directory has tokenizer files only and no complete weights. Current data is still `training_ready=false`; no QLoRA optimizer run has been performed. Existing full loader audit recorded 7,464/670 train/validation candidates at cutoff 262144; scale/full remain gated. Do not start GPU training on the current host.

### Task 3: Paired held-out Mind2Web evaluation for base and adapter

**Files:**
- Create: `src/everos/agent_training/agentlab_mind2web_eval.py`
- Create: `scripts/eval_agentlab_mind2web.py`
- Create: `scripts/compare_agentlab_mind2web.py`
- Create: `tests/unit/test_agent_training/test_agentlab_mind2web_eval.py`
- Modify: `docs/superpowers/plans/2026-11-09-vault-agentlab-qwen35.zh-CN.md` section 12.

**Interfaces:**
- `parse_target_action(text: str) -> tuple[str, str] | None` parses the published JSON target and returns its action name and canonical JSON representation of literal call arguments; it never executes the action.
- `compare_pair_reports(base_report, adapter_report) -> dict` verifies identical ordered IDs, test-data and manifest hashes, generation settings, model name/revision, and complete status, then reports exact action match, action-type match, invalid JSON/action rates, and per-trajectory all-actions match.
- Eval CLI accepts only the held-out `test.jsonl` plus its manifest, `--model-tag base|adapter`, API URL/key/model ID, a required model fingerprint, and an output directory. It sends identical system/user messages with temperature 0 and max completion tokens 256.
- The evaluator labels results as decision-level imitation against published SFT targets; it does not claim live website task success or recover unknown original loss masks.

- [x] **Step 1: Write failing tests** for target JSON/action parsing (including markdown fences and malformed outputs), canonical expression equality, per-trajectory aggregation, test-only enforcement, and paired-run ID/hash mismatch rejection.
- [x] **Step 2: Run tests and confirm the expected missing-module/function failures.**
- [x] **Step 3: Implement pure metric and result-integrity helpers** with no execution of model-produced Python and no dependence on a live endpoint.
- [x] **Step 4: Implement the API runner and comparison command.** It writes one prediction record per input decision, preserves response text, records model fingerprint and test hashes, and refuses to overwrite a prior result directory.
- [x] **Step 5: Run focused tests and a local fake-endpoint check.** The full 926-row held-out command was exercised against a local fixture endpoint for base and adapter (1,852 requests total); ID/hash/generation alignment and paired comparison passed. This checks evaluator integrity, not model quality.

### Task 4: Reproducible AgentLab plan and operation commands

**Files:**
- Modify: `docs/superpowers/plans/2026-11-09-vault-agentlab-qwen35.zh-CN.md`

**Interfaces:**
- Section 12 documents each generated script, its environment variables, output paths, exact model revision, and commands for base/adapter services, integration smoke, paired Mind2Web base/adapter test evaluation, short probe, scale, and full training.
- The plan defines the local “closed loop” as model observation → AgentLab action parsing → BrowserGym execution → resulting observation returned to the model; it explicitly distinguishes that integration smoke from the final paired held-out evaluation.

- [x] **Step 1: Add section 12 commands** for clean-server setup, base/adapter service and proxy terminals, integration smoke, paired held-out Mind2Web evaluation and comparison, short QLoRA smoke, scale/full readiness gate, and result inspection. Update section 10.5 to call the extracted loop script. Retain sections 8–11 data references and counts.
- [x] **Step 2: Check the documentation** with `rg` for all script paths, both cutoffs, the smoke probe flag, readiness gate, paired base/adapter evaluation, test exclusion, and the integration-smoke distinction.
- [x] **Step 3: Run the focused tests and inspect the final diff** for consistent paths, counts, ports, evaluation IDs, and cutoffs.
