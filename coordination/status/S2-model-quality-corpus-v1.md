# Status Packet

- **Session:** S2
- **Required model:** GPT-5.6 Luna
- **Role:** Model/Performance — quality evaluation corpus
- **Timestamp (UTC):** 2026-09-11
- **Branch/worktree:** `luna/model-quality-corpus-v1` / `wt-model-quality-corpus-v1`
- **Current phase:** model-quality corpus construction (no model, no spend, no credential)
- **Primary task ID:** MODEL-QUALITY-CORPUS-001
- **Task state:** IN_PROGRESS
- **Stage:** stage 1 (design) source-complete on the branch; stage 2 (bulk authoring) pending
- **Base `main` commit:** `360519274b2be2f04a61297650a4b0586016201d`

## Objective for this work interval

Make 1,180+ quality cases authorable by several parallel lanes without divergence.
This interval delivers the schema, the validator, the corpus layout, per-category
exemplars, and a precise authoring brief. It does **not** author the bulk cases and
it advances no gate.

## Inputs and dependencies

- `model/quality-eval/quality-fixture-spec.json` — 13 categories, minimums totalling
  1,180, metrics, `split_policy`, `comparison`, `logging.never_record`.
- `tests/model/production_tool_call_eval.json` — the 33-tool shipping profile, sha
  `c75af5200b76a504e6b603183ffcf1cbeedb93db18ec544683044b8cc9b8ac6c`. Used directly
  as the tool catalogue; no second copy exists to drift.
- `host/agent/tool-envelope.mjs`, `scripts/test/evaluate_tool_calls.py:67-76`,
  `contracts/engine-api/contract.json` — the accepted `qwen35-xml-tool-call-v1`
  render/parse path.
- `governance/MODEL_DECISION.md` (generation profiles, model-quality acceptance),
  `execution/ACCEPTANCE_CRITERIA.md` §6/§7/§11, `execution/TEST_AND_BENCHMARK_PLAN.md`
  §5/§6, `governance/INTERSESSION_PROTOCOL.md` §6/§18, `AGENTS.md` Qwen3.5 rules.

## Work completed

- **Schema** `model/quality-eval/schema/quality-case.schema.json` — JSON Schema
  draft 2020-12 for one case and one category file
  (`{schema_version, fixture_id, category, cases}`), with a separate `expected`
  variant per category selected by `if`/`then` on `category`, plus conditional
  requirements for `follow_up`, `tools`, and profile/mode/thinking consistency.
- **Validator** `scripts/model-artifact/validate_quality_corpus.py` — stdlib only,
  no new dependency, matching `validate_specs.py`. It implements a bounded JSON
  Schema subset interpreter driven by the schema file, then adds the semantic checks
  schema cannot express: unique ids, id/category/filename agreement, deterministic
  `split = sha256(id) % 100` (<20 train, <40 dev, else test), split proportions
  within ±5 points of 20/20/60, difficulty mix within ±10 points of 25/30/25/20,
  `temperature: 0` with an explicit bounded `max_output_tokens` and explicit
  `enable_thinking`, deep-profile restriction, production tool-name and
  argument-name membership, per-metric `expected` shape, an obligatory negative
  assertion on every adversarial case, and forbidden-content scanning (emails,
  telephone literals, `hf_`/`sk-`/`gh*_`/`AKIA`/private-key material, non-allowlisted
  `http(s)://` hosts, real-system and non-fictional absolute paths, undeclared
  non-Latin script). Prints a per-category count table and PASS/FAIL; exit 0/1.
  `--require-complete` additionally enforces the spec minimums.
- **Tests** `tests/model/test_quality_corpus_validate.py` — 73 unittest cases over
  inline temporary corpora: a valid fixture passes, and each failure class fails.
  Registered in `scripts/test/run_qa.py` `TEST_INVENTORY` as `model_fixture`.
- **Seed corpus** `model/quality-eval/cases/<category>.json` for all 13 categories,
  4 exemplars each (easy, medium, hard, adversarial) = **52 cases**, all PASS. The
  spec's three exemplars are placed in their categories with ids unchanged:
  `thinking-off-001` (thinking_control), `tool-xml-001` (tool_selection_no_tool),
  `reset-long-short-001` (long_short_integrity).
- **Guide** `model/quality-eval/CORPUS_AUTHORING_GUIDE.md` — purpose, layout, id
  scheme, all 13 `expected` schemas with a worked example and scoring rule each,
  the settings rule, tool referencing and the accepted XML render, `follow_up`/`reset`
  semantics, long-input generators, split rule, difficulty mix, per-category targets
  (minimum + 10% = 1,298), content rules, the `never_record` logging rule, the
  validator commands, and a 10-point definition of done per category file.
- **Spec** `model/quality-eval/quality-fixture-spec.json` — additive pointer keys
  only (`cases_dir`, `case_schema`, `authoring_guide`, `case_id_pattern`,
  `split_derivation`). `categories`, `fixture_cases`, `comparison`, `split_policy`
  and `logging` are byte-identical, so the two manifest fragment references in
  `scripts/model-artifact/build_manifest.py:112-113` and
  `artifacts/qwen35-9b/model-manifest.json` still resolve.

## Evidence (reproduced in this worktree)

- `python3 scripts/model-artifact/validate_quality_corpus.py` → **PASS**, exit 0.
  Seed counts, all 13 categories at 4 cases, total **52** against a spec minimum
  total of **1,180** and an authoring target total of **1,298**.
- `python3 scripts/model-artifact/validate_quality_corpus.py --require-complete` →
  exit 1, as expected while the corpus is seeded rather than authored.
- `python3 scripts/model-artifact/validate_specs.py` →
  `model/performance Phase 0 specifications: OK`, exit 0.
- `python3 -m unittest tests.model.test_quality_corpus_validate` → `Ran 73 tests` OK.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -` → status
  `BLOCKED`, **65 discovered / 0 missing / 0 unknown**, 71 records (1 PASS / 70
  expected SKIP). Baseline at `360519274b2be2f04a61297650a4b0586016201d` was 64
  discovered and 70 records; the delta is exactly the one new registered test.
- `git diff --check main...HEAD` → exit 0. `git status --short` → empty.
- 13 new tracked JSON case files plus 1 new schema JSON; tracked JSON inventory
  moves 154 → 168.

No model was loaded, no bytes were read, no network call was made, no credential was
used, and no score is claimed.

## Decisions taken, for review

1. **Split is derived, not chosen.** `split = sha256(id) % 100` bucketed 20/20/60,
   recomputed and enforced by the validator. Consequence: an id is immutable once
   committed — renaming a case silently moves it across the immutable `test` split.
2. **Three reserved ids are grandfathered** out of the `<category-slug>-<NNN>` rule
   and pinned to their categories by an explicit allowlist in the validator.
3. **`max_output_tokens` is bounded to 1..256.** `governance/MODEL_DECISION.md`
   describes a 1,024 default and a 2,048 hard cap, but `native/server/chat_request.cpp`
   accepts `1..256` and `contracts/engine-api/contract.json` declares `64`. The corpus
   binds to 256, the value the shipping fixture and host client actually use.
   **Sol decision requested** on which of the three is authoritative.
4. **Profiles and wire modes are both recorded.** `settings.profile`
   (interactive/tool_selection/deep) carries the governance vocabulary;
   `settings.mode` (normal/deep) carries the wire value; `enable_thinking` must equal
   `mode == "deep"`, mirroring the single engine derivation point. Deep is permitted
   only in `reasoning`, `file_task_planning`, `tool_recovery`, `thinking_control`.
5. **`temperature: 0` is an assertion, not a knob.** The native backend uses a greedy
   sampler unconditionally; the field is retained because the spec and the shipping
   fixture already carry it.
6. **Minimums are reported but enforced only under `--require-complete`**, so author
   lanes can validate a partly-written file. Split proportions engage at ≥40 cases
   and the difficulty mix at ≥20.
7. **`expected.call` / `expected.no_call` / `expected.forbid_names`** reuse the
   existing `scripts/test/evaluate_tool_calls.py` vocabulary rather than the spec
   exemplar's `canonical_tool`; the spec's own `fixture_cases` are untouched.

## Blockers

- None for stage 2 authoring; it is unblocked and needs no model, spend, or credential.
- Unchanged and **not** resolved by this task: the ≥95% retention criterion
  (`execution/ACCEPTANCE_CRITERIA.md:215`) has no reachable higher-precision
  comparator — Q8_0 and bf16 survive only as hashes in
  `artifacts/qwen35-9b/scan-receipt.json` — so any quality run must budget a full
  Shadeform re-conversion. The shipping 33-tool/37-case profile still has no
  recorded score.

## Next bounded action

Sol assigns stage 2 author lanes, one per category file, using the AUTHOR BRIEF in
`model/quality-eval/CORPUS_AUTHORING_GUIDE.md` §9/§11/§14. Each lane runs
`python3 scripts/model-artifact/validate_quality_corpus.py` before every commit; Sol
runs it with `--require-complete` before acceptance.

## Sol action requested

1. Review and merge stage 1 (schema, validator, tests, guide, 52 seed cases).
2. Rule on the `max_output_tokens` ceiling discrepancy in decision 3.
3. Assign the 13 stage-2 author lanes and their target counts (minimum + 10%).
