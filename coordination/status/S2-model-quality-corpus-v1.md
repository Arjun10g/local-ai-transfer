# Status Packet

- **Session:** S2
- **Required model:** GPT-5.6 Luna
- **Role:** Model/Performance — quality evaluation corpus
- **Timestamp (UTC):** 2026-09-11
- **Branch/worktree:** `luna/model-quality-corpus-v1` / `wt-model-quality-corpus-v1`
- **Current phase:** model-quality corpus construction (no model, no spend, no credential)
- **Primary task ID:** MODEL-QUALITY-CORPUS-001
- **Task state:** READY_FOR_REVIEW
- **Stage:** stage 1 (design) and stage 2 (authoring, 1,336 cases) both source-complete
- **Base `main` commit:** `360519274b2be2f04a61297650a4b0586016201d`

## Objective for this work interval

Stage 1 made 1,180+ quality cases authorable by several parallel lanes without
divergence: schema, validator, corpus layout, per-category exemplars, authoring
brief. Stage 2 integrated the six author lanes conflict-free and the corpus now
holds **1,336 cases** and passes the completion gate. This still advances no gate:
the deliverable is a fixture, not a score.

The sections below record stage 1 as built, then stage 2 integration.

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
  4 exemplars each (easy, medium, hard, adversarial) = **52 cases**, all PASS.
  Stage 2 grew these files to 1,336 cases; the 52 seeds are the floor the tests
  still assert against. The
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

## Stage 2 integration — author lanes merged

All six lanes branched from `2b5ad13` and each touched **only** its own category
files, so all six merges were conflict-free. Merged `--no-ff` in the order given:

| lane | branch | tip | merge | categories authored |
|---|---|---|---|---|
| A | `luna/corpus-author-a` | `25ded9e` | `9456711` | tool_selection_no_tool 220 |
| B | `luna/corpus-author-b` | `5a4a549` | `f3ed4d9` | tool_arguments 177 |
| C | `luna/corpus-author-c` | `f3d8d1e` | `23b5ddb` | instruction 110, policy_safety 110 |
| D | `luna/corpus-author-d` | `5832b00` | `fd48bf1` | continuity_reset 92, long_short_integrity 80, thinking_control 85 |
| E | `luna/corpus-author-e` | `3fcc566` | `6e86325` | extraction 88, summarization 66, reasoning 66 |
| F | `luna/corpus-author-f` | `72efc21` | `c23d135` | file_task_planning 88, tool_recovery 88, code_command 66 |

Every category is covered exactly once; no file was touched by two lanes.

## Corpus shape at integration

`python3 scripts/model-artifact/validate_quality_corpus.py` — **PASS**, exit 0.
`python3 scripts/model-artifact/validate_quality_corpus.py --require-complete` —
**PASS**, exit 0. Both modes agree; the completion gate is clean.

| category | cases | min | target | train | dev | test | easy | med | hard | adv |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| instruction | 110 | 100 | 110 | 21 | 18 | 71 | 28 | 33 | 27 | 22 |
| continuity_reset | 92 | 80 | 88 | 23 | 14 | 55 | 23 | 28 | 23 | 18 |
| summarization | 66 | 60 | 66 | 14 | 10 | 42 | 17 | 20 | 16 | 13 |
| extraction | 88 | 80 | 88 | 16 | 22 | 50 | 22 | 27 | 22 | 17 |
| reasoning | 66 | 60 | 66 | 16 | 10 | 40 | 17 | 20 | 16 | 13 |
| code_command | 66 | 60 | 66 | 15 | 14 | 37 | 17 | 20 | 16 | 13 |
| file_task_planning | 88 | 80 | 88 | 14 | 18 | 56 | 22 | 26 | 22 | 18 |
| tool_selection_no_tool | 220 | 200 | 220 | 38 | 50 | 132 | 51 | 64 | 61 | 44 |
| tool_arguments | 177 | 160 | 176 | 41 | 27 | 109 | 44 | 53 | 44 | 36 |
| tool_recovery | 88 | 80 | 88 | 18 | 14 | 56 | 22 | 26 | 22 | 18 |
| policy_safety | 110 | 100 | 110 | 21 | 22 | 67 | 26 | 35 | 25 | 24 |
| long_short_integrity | 80 | 60 | 66 | 14 | 15 | 51 | 20 | 24 | 20 | 16 |
| thinking_control | 85 | 60 | 66 | 20 | 16 | 49 | 21 | 26 | 21 | 17 |
| **TOTAL** | **1336** | **1180** | **1298** | **271** | **250** | **815** | **330** | **402** | **335** | **269** |

Every `split` value is the derived `sha256(id) % 100` bucket; every category is
within ±5 points of 20/20/60 and within ±10 points of the 25/30/25/20 difficulty
mix. Three categories overshot their target (`continuity_reset` 92, `thinking_control`
85, `long_short_integrity` 80) to close the split band against bucket noise, which
the guide now records as expected rather than a defect. No category exceeds twice
its spec minimum.

The three reserved ids are intact and in their categories: `thinking-off-001`
(thinking_control), `tool-xml-001` (tool_selection_no_tool), `reset-long-short-001`
(long_short_integrity).

## Near-duplicate scan

`python3 scripts/model-artifact/scan_corpus_duplicates.py` — token-set Jaccard over
the normalized first user message, within a category, threshold 0.9. Review aid
only: it never edits a case and always exits 0. **Nothing was deleted.**

**2 suspicious pairs out of 1,336 cases.** Eleven of the thirteen categories report
zero; `long_short_integrity` and `thinking_control` report one each:

| score | category | pair |
|---:|---|---|
| 1.000 | long_short_integrity | `long-short-integrity-028` ~ `long-short-integrity-074` |
| 1.000 | thinking_control | `thinking-control-067` ~ `thinking-control-084` |

(The scan lists a top-10; only 2 pairs reach the threshold, so the list is complete.)

Both were reviewed by hand and are **false positives of the heuristic, not
duplicates**. The scan compares only the *first* user message, and these two
categories are continuation-shaped, so distinct cases legitimately share an opener:

- `long-short-integrity-028` / `-074` share the opener but differ in `long_input`
  (`target_tokens` 4000 seed 125 vs 6500 seed 171), in the `follow_up` short turn
  (`OK` vs `ALL CLEAR`), in difficulty and in split.
- `thinking-control-067` / `-084` share the opener but differ in `follow_up.reset`
  (`false` vs `true`) and therefore in what they test — mode self-report versus
  state isolation across a reset, with different `must_equal` and `must_not_contain`.

Threshold sweep for context: 2 pairs at 0.95 and 0.85, 7 at 0.75, 30 at 0.65. The
recorded limitation is that the scan does not consider `follow_up`, `long_input`,
`tools` or `expected`; a future revision could hash the whole normalized case.

## Author-reported defects and dispositions

1. **Lane A — brittle stage-1 test.** `test_committed_corpus_passes_in_authoring_mode`
   asserted the committed corpus was exactly 52 cases and exactly 4 per category, so
   it failed as soon as any lane added cases. **Fixed** in `c09c265`: the equalities
   became bounds against the seed floor (total ≥ 52, each category ≥ 4), the PASS and
   13-row assertions were kept, and a new
   `test_committed_corpus_never_exceeds_twice_the_spec_minimum` guards runaway
   generation. The validator was not changed.
2. **Guide — unsatisfiable id contiguity.** Definition of done item 5 required ids
   "contiguous from `001`", which conflicts with the hash-derived split: authors must
   be free to skip an ordinal whose bucket would push the category outside the
   ±5-point band. **Fixed**: item 5 now requires ids to be unique, strictly
   increasing, and never reused or renumbered, with gaps permitted and expected, and
   the split section explains the band against bucket noise (at n≈66 one case is 1.5
   points of the split). Because `continuity_reset` at 92 sits on the boundary, the
   guide now requires any later removal there to be paired with an addition in the
   same commit.
3. **Lane F — governance-doc mismatch, NOT fixed here.**
   `governance/SECURITY_AND_TOOL_POLICY.md` §10 documents `process.run_allowlisted`
   as taking `executable_id`, `arguments`, `workspace_id` and `timeout_ms`, while the
   shipping catalogue `tests/model/production_tool_call_eval.json` and the runtime
   provider `host/tools/local/process-run.mjs` use `action_id` plus an optional
   `parameters` object. The corpus follows the shipping catalogue, because the
   validator binds to it and because a logical action id — not an executable path —
   is what `execution/ACCEPTANCE_CRITERIA.md` §7 requires. Per Sol's instruction the
   governance document was **not edited**. **Recorded here as a governance-doc
   mismatch for Sol**: either §10 is stale and should be corrected, or the catalogue
   is wrong and the corpus plus the runtime provider must change together. This is a
   documentation/source divergence on a security-relevant tool contract and should
   not sit open.

## Evidence (reproduced in this worktree at the integration tip)

- `python3 scripts/model-artifact/validate_quality_corpus.py` → **PASS**, exit 0;
  1,336 cases across 13 categories.
- `python3 scripts/model-artifact/validate_quality_corpus.py --require-complete` →
  **PASS**, exit 0. Minimums, split proportions and difficulty mix all enforced.
- `python3 -m unittest tests.model.test_quality_corpus_validate` → `Ran 75 tests` OK.
- `python3 scripts/model-artifact/validate_specs.py` →
  `model/performance Phase 0 specifications: OK`, exit 0.
- `python3 scripts/test/run_qa.py --root . --skip-native --output -` → status
  `BLOCKED`, **65 discovered / 0 missing / 0 unknown**, 71 records (1 PASS / 70
  expected SKIP). Unchanged from stage 1; no new test file was added in stage 2.
- Strict duplicate-key JSON parse over `model/quality-eval/cases/*.json` plus the
  schema and the fixture spec: **15 files OK, 1,336 cases**, no duplicate key.
- `python3 scripts/model-artifact/scan_corpus_duplicates.py` → 2 suspicious pairs,
  both reviewed and retained, exit 0.
- `git diff --check main...HEAD` → exit 0. `git status --short` → empty.
- Tracked JSON files 154 → 168 (13 case files + 1 schema).

No model was loaded, no bytes were read, no network call was made, no credential was
used, and **no score is claimed**. This is a fixture, not model evidence.

## Decisions taken in stage 1, still standing for review

1. **Split is derived, not chosen** — `sha256(id) % 100` bucketed 20/20/60,
   recomputed and enforced. An id is immutable once committed.
2. **Three reserved ids are grandfathered** out of the `<category-slug>-<NNN>` rule
   and pinned to their categories by an explicit validator allowlist.
3. **`max_output_tokens` is bounded to 1..256.** `governance/MODEL_DECISION.md`
   describes a 1,024 default and a 2,048 hard cap, `native/server/chat_request.cpp`
   accepts `1..256`, and `contracts/engine-api/contract.json` declares `64`. The
   corpus binds to 256, the value the shipping fixture and host client use.
   **Sol decision still requested** on which is authoritative.
4. **Profiles and wire modes are both recorded** — `settings.profile`
   (interactive/tool_selection/deep) and `settings.mode` (normal/deep), with
   `enable_thinking == (mode == "deep")`. Deep is permitted only in `reasoning`,
   `file_task_planning`, `tool_recovery`, `thinking_control`.
5. **`temperature: 0` is an assertion, not a knob** — the native backend uses a
   greedy sampler unconditionally.
6. **`--require-complete` is the completion gate**; the default mode stays tolerant
   so a partly authored file is still checkable.
7. **`expected.call` / `no_call` / `forbid_names`** reuse the existing
   `scripts/test/evaluate_tool_calls.py` vocabulary; the spec's `fixture_cases` are
   untouched.

## Blockers

- **Not resolved by this task and unchanged:** the ≥95% retention criterion
  (`execution/ACCEPTANCE_CRITERIA.md:215`) has no reachable higher-precision
  comparator — Q8_0 and bf16 survive only as hashes in
  `artifacts/qwen35-9b/scan-receipt.json` — so any quality run must budget a full
  Shadeform re-conversion. The shipping 33-tool/37-case profile still has no recorded
  score, and this corpus has never been executed against a model.
- Open for Sol: the `max_output_tokens` ceiling (decision 3) and the
  `process.run_allowlisted` governance-doc mismatch (defect 3).

## Next bounded action

Independent S0/S4 review of the integrated corpus. Nothing further should be authored
into `test`-split cases: per `split_policy` they are held out and immutable once
merged.

## Sol action requested

1. Review and merge the integrated corpus (stage 1 + stage 2, 1,336 cases).
2. Rule on the `max_output_tokens` ceiling discrepancy (decision 3).
3. Rule on the `process.run_allowlisted` argument-contract mismatch between
   `governance/SECURITY_AND_TOOL_POLICY.md` §10 and the shipping catalogue (defect 3).
4. Confirm the two retained near-duplicate pairs are acceptable, or assign a lane to
   re-word their openers.
