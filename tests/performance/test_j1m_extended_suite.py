"""The opt-in extended suite and scorer ablation, proved offline.

``scripts/j1m_orchestrator.py --suite extended`` adds, after the shipping eval
stage of the SAME paid lifecycle, the three 60-case variants chunks and a
conservative long-context harness run; ``--scorer-ablation
no-boolean-coercion`` adds a re-score of the shipping fixture with the scorer's
True/False coercion disabled. Both are default OFF.

What is proved here, without a network, a provider or a model:

* with neither option the plan, argv, uploads, fetch list and salvage caps are
  exactly what they were before this slice (a snapshot taken from the pre-edit
  code, plus a live comparison against the committed ``HEAD`` sources);
* the extended plan runs its members in a fixed order, after the shipping
  stage, with explicit time and cost numbers and an unchanged cost ceiling;
* every new remote argv passes the same persisted-argv validator;
* a member receipt that claims another run, instance, chunk, fixture or
  harness is refused at the salvage boundary and by its verifier;
* a member failure never changes the shipping job, status or receipt;
* the ablation defaults off, changes only boolean coercion, and is recorded
  so its score can never be read as the shipping one;
* the long-context member really drives the harness against a running
  (fake, loopback) engine and publishes no prompt or model text.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import j1m_dry_run as dry_run
from scripts import j1m_orchestrator as orc
from scripts import j1m_runner
from scripts.test import evaluate_tool_calls as ev
from scripts.test import remote_model_eval as rme

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
EXTRA_NAMES = (
    "ablation-no-boolean-coercion-receipt.json",
    "variants-chunk-1-receipt.json",
    "variants-chunk-2-receipt.json",
    "variants-chunk-3-receipt.json",
    "long-context-receipt.json",
)
ALL_MEMBERS = ("ablation-no-boolean-coercion", "variants-chunk-1", "variants-chunk-2",
               "variants-chunk-3", "long-context")

# ---- Snapshot of the default eval plan, captured from the pre-edit code ----
# (HEAD 6c51f21, ``j1m_orchestrator.py --mode eval``). Regenerate only for a
# reviewed change to the SHIPPING plan:
#   python3 -c "import json,hashlib,subprocess;p=json.loads(subprocess.check_output(
#     ['python3','scripts/j1m_orchestrator.py','--mode','eval']));
#     print(hashlib.sha256(json.dumps(p['commands'],sort_keys=True).encode()).hexdigest())"
DEFAULT_EVAL_PLAN_KEYS = [
    "active_run_cost_usd", "approved_targets", "artifact", "artifact_allowlist", "candidate",
    "commands", "created_at_utc", "cuda_architecture", "execution_backend", "gpu_layers", "mode",
    "mode_active_cost_usd", "mode_runtime_hours", "mutation", "orchestrator", "os_image_policy",
    "provider_backstop_cost_usd", "quality_only", "receipt_allowlist", "required_scratch_gib",
    "schema", "selected_target", "target_selection",
]
DEFAULT_EVAL_COMMANDS_SHA256 = "ddf45a59d34af27f12dc8f20c3a3c8a88ec3a626bf607f2f2ef8cf06ef6a28c1"
DEFAULT_EVAL_COMMAND_COUNT = 16
DEFAULT_EVAL_PLAN_SHA256 = "73de2d8354aa5f5fd40e4a8b238f3dd6a1eeb4ee03f0bc534dd81635e6aea716"
DEFAULT_EVAL_UPLOADS = [
    "/scratch/j1m/model-manifest.json", "/scratch/j1m/model-manifest.sha256",
    "/scratch/j1m/remote_model_eval.py", "/scratch/j1m/remote_eval_prepare.py",
    "/scratch/j1m/evaluate_tool_calls.py", "/scratch/j1m/cuda_device_probe.py",
    "/scratch/j1m/remote_toolchain_probe.py", "/scratch/j1m/engine/scripts/cuda_source_closure.py",
    "/scratch/j1m/ggml-cuda-source-lock.json", "/scratch/j1m/ggml-CMakeLists.txt",
    "/scratch/j1m/production_tool_call_eval.json", "/scratch/j1m/engine/CMakeLists.txt",
    "/scratch/j1m/engine", "/scratch/j1m/engine/tests/native/runtime_tests.cpp",
    "/scratch/j1m/engine/tests/native/model_validator_tests.cpp",
]
DEFAULT_EVAL_FETCH = [
    "eval-receipt.json", "startup-preflight-receipt.json", "eval-artifact-receipt.json",
    "toolchain-receipt.json", "cuda-device-receipt.json", "command-receipt.json",
]


def _plan(*argv: str) -> dict:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        status = orc.main(list(argv))
    assert status == 0
    return json.loads(stdout.getvalue())


def _without_clock(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key != "created_at_utc"}


def _config() -> dict:
    return j1m_runner.load_config()


def _artifact() -> dict:
    return orc._verify_eval_artifact(None, ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json", _config())


def _member_receipts(run_id: str = "J1MDRY", instance_id: str = dry_run.FAKE_INSTANCE_ID) -> dict[str, bytes]:
    return dry_run.fake_receipts(_config(), _artifact(), run_id=run_id, instance_id=instance_id, extended=True)


def _salvage_identity(run_id: str = "J1MDRY", instance_id: str = dry_run.FAKE_INSTANCE_ID) -> dict:
    preflight = orc._extended_suite_preflight(_config(), ALL_MEMBERS)
    return {
        "run_id": run_id, "instance_id": instance_id, "artifact": _artifact(),
        "fixture_sha256": orc._tool_eval_contract()["fixture_identity"]["sha256"],
        "extra_bindings": orc._extra_salvage_bindings(preflight),
    }


class _PrivateDir:
    """A private directory beneath the trusted output root, as the verifiers require."""

    def __init__(self, case: unittest.TestCase):
        self.path = Path(tempfile.mkdtemp(dir=FIXTURES, prefix=".extended-suite-"))
        case.addCleanup(shutil.rmtree, self.path, ignore_errors=True)

    def write(self, name: str, payload) -> Path:
        path = self.path / name
        data = payload if isinstance(payload, bytes) else (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        path.write_bytes(data)
        path.chmod(0o600)
        return path


class DefaultPathIsUnchangedTests(unittest.TestCase):
    """With neither option, nothing about the shipping eval moves."""

    def test_default_eval_plan_matches_the_pre_edit_snapshot(self):
        plan = _plan("--mode", "eval")
        self.assertEqual(sorted(plan), DEFAULT_EVAL_PLAN_KEYS)
        self.assertFalse([key for key in plan if "extended" in key])
        commands = json.dumps(plan["commands"], sort_keys=True).encode("utf-8")
        self.assertEqual(len(plan["commands"]), DEFAULT_EVAL_COMMAND_COUNT)
        self.assertEqual(hashlib.sha256(commands).hexdigest(), DEFAULT_EVAL_COMMANDS_SHA256)
        self.assertEqual(
            hashlib.sha256(json.dumps(_without_clock(plan), sort_keys=True).encode("utf-8")).hexdigest(),
            DEFAULT_EVAL_PLAN_SHA256)

    def test_explicit_default_options_are_byte_identical_to_no_options(self):
        bare = _without_clock(_plan("--mode", "eval"))
        explicit = _without_clock(_plan("--mode", "eval", "--suite", "shipping", "--scorer-ablation", ""))
        self.assertEqual(json.dumps(bare, sort_keys=True), json.dumps(explicit, sort_keys=True))
        for mode in ("prove", "build", "canary"):
            self.assertFalse([key for key in _plan("--mode", mode) if "extended" in key])

    def test_default_uploads_fetch_list_and_salvage_caps_are_unchanged(self):
        config = _config()
        uploads = orc._eval_uploads(config, "/scratch/j1m", None, ROOT / orc._APPROVED_EVAL_MANIFEST_RELATIVE)
        self.assertEqual([remote for _local, remote, _recursive in uploads], DEFAULT_EVAL_UPLOADS)
        self.assertEqual(orc._eval_fetch_allowlist(config, ()), DEFAULT_EVAL_FETCH)
        self.assertIs(orc._salvage_effective_allowlist(), orc._SALVAGE_RECEIPT_ALLOWLIST)
        self.assertEqual(orc._SALVAGE_MAX_FILES, len(orc._SALVAGE_RECEIPT_ALLOWLIST))
        self.assertFalse(set(EXTRA_NAMES) & set(orc._SALVAGE_RECEIPT_ALLOWLIST))
        self.assertEqual(orc._extra_members("shipping", ""), ())
        self.assertEqual(orc._extra_members(None, None), ())

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_default_lifecycle_is_identical_to_the_committed_sources(self):
        """Drive the real default eval ``execute()`` with HEAD and working-tree code.

        Every recorded argv, the salvage receipt (allowlist, caps, files), the
        persisted lifecycle receipt and the published file set must agree once
        nonces, temporary paths, timestamps and durations are normalised.
        Runs in subprocesses so neither import can leak into this process.
        """

        with tempfile.TemporaryDirectory() as directory:
            head = Path(directory)
            for relative in ("scripts/j1m_orchestrator.py", "scripts/j1m_dry_run.py"):
                shown = subprocess.run(["git", "show", f"HEAD:{relative}"], cwd=ROOT,
                                       capture_output=True, check=False)
                if shown.returncode != 0:
                    self.skipTest("HEAD sources are unavailable")
                (head / Path(relative).name).write_bytes(shown.stdout)
            outputs = [
                subprocess.run([sys.executable, "-c", _HEAD_COMPARISON, str(head), which], cwd=ROOT,
                               capture_output=True, text=True, timeout=300, check=False)
                for which in ("head", "work")
            ]
        for output in outputs:
            self.assertEqual(output.returncode, 0, output.stderr[-2000:])
        self.assertEqual(outputs[0].stdout, outputs[1].stdout)
        self.assertIn("eval-receipt.json", outputs[1].stdout)


_HEAD_COMPARISON = r'''
import copy, importlib.util, json, re, sys
from pathlib import Path
from unittest import mock
ROOT = Path.cwd(); sys.path.insert(0, str(ROOT))
head, which = Path(sys.argv[1]), sys.argv[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module
if which == "head":
    import scripts
    orch = load("scripts.j1m_orchestrator", head / "j1m_orchestrator.py"); scripts.j1m_orchestrator = orch
    orch.ROOT = ROOT
    dry = load("scripts.j1m_dry_run", head / "j1m_dry_run.py")
    dry.ROOT = ROOT; dry.DRY_RUN_ROOT = ROOT / ".secrets" / "j1m-dry-run"
else:
    from scripts import j1m_dry_run as dry, j1m_orchestrator as orch
captured = {}
real_writer, real_persist = orch._write_salvage_receipt, orch._persist_lifecycle
def salvage(destination, receipt):
    captured["salvage"] = copy.deepcopy(receipt); return real_writer(destination, receipt)
def persist(phase, value):
    captured["lifecycle"] = copy.deepcopy(value); return real_persist(phase, value)
recorder = dry.Recorder()
with dry.dry_run_key_root(None) as key_root, \
        mock.patch.object(orch, "_write_salvage_receipt", side_effect=salvage), \
        mock.patch.object(orch, "_persist_lifecycle", side_effect=persist):
    out = dry.drive("eval", inject_failure=False, key_root=key_root, recorder=recorder)
def norm(value):
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r"/[^\"]*?/j1m-[^\"/]*?-[a-z0-9_]{8}/", "<TMP>/", text)
    text = re.sub(r"j1m-[0-9a-f]{32}", "j1m-<NONCE>", text)
    text = re.sub(r"J1MDRY-[0-9a-f]{32}", "J1MDRY-<NONCE>", text)
    text = re.sub(r"\"duration_ms\": \d+", "\"duration_ms\": 0", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[0-9:.+]+", "<TS>", text)
    text = re.sub(r"\"watchdog_pid\": \d+", "\"watchdog_pid\": 0", text)
    return text
print(json.dumps({"argv": norm([entry["argv"] for entry in recorder.entries]),
                  "salvage": norm(captured["salvage"]), "lifecycle": norm(captured["lifecycle"]),
                  "status": out["status"], "published": out["published"],
                  "codes": out["salvage_codes"], "progress": out["progress"]}, sort_keys=True))
'''


class ExtendedPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = _plan("--mode", "eval", "--suite", "extended", "--scorer-ablation", "no-boolean-coercion")
        self.suite = self.plan["extended_suite"]

    def test_members_are_ordered_after_the_shipping_eval_with_their_own_receipts(self):
        self.assertEqual(self.suite["members"], list(ALL_MEMBERS))
        self.assertEqual(self.plan["extended_fetch_allowlist"], list(EXTRA_NAMES))
        self.assertEqual([command[command.index("--suite-member") + 1] for command in self.plan["extended_commands"]],
                         list(ALL_MEMBERS))
        # The shipping plan inside the extended plan is the shipping plan.
        self.assertEqual(self.plan["commands"], _plan("--mode", "eval")["commands"])
        self.assertEqual(self.plan["extended_uploads"], [
            "/scratch/j1m/variants-chunk-1.json", "/scratch/j1m/variants-chunk-2.json",
            "/scratch/j1m/variants-chunk-3.json", "/scratch/j1m/long_context_eval.py"])
        for command, member in zip(self.plan["extended_commands"], ALL_MEMBERS):
            receipt = command[command.index("--receipt") + 1]
            preflight = command[command.index("--preflight-receipt") + 1]
            self.assertEqual(receipt, f"/scratch/j1m/artifacts/{member}-receipt.json")
            # Never the shipping receipt or preflight path, and never salvageable.
            self.assertNotIn("/artifacts/", preflight)
            self.assertEqual(command[command.index("--expected-fixture-sha256") + 1],
                             self.suite["fixtures_sha256"][member])
        self.assertEqual(self.suite["fixtures_sha256"]["variants-chunk-2"], orc._VARIANT_CHUNK_SHA256["variants-chunk-2"])

    def test_cost_projection_is_explicit_and_the_ceiling_is_unchanged(self):
        budget = self.suite["budget"]
        config = _config()
        self.assertIs(budget["raises_authorized_cost"], False)
        self.assertEqual(budget["authorized_active_cost_usd"], self.plan["mode_active_cost_usd"])
        self.assertEqual(budget["authorized_active_cost_usd"],
                         j1m_runner.mode_active_cost_usd(config, "eval", budget["hourly_usd"]))
        self.assertEqual(budget["runtime_hours"], config["modes"]["eval"]["runtime_hours"])
        self.assertEqual(budget["stage_budget_seconds"]["long-context"], 900.0)
        self.assertEqual(budget["stage_budget_seconds"]["variants-chunk-1"],
                         float(config["modes"]["eval"]["stage_budgets_seconds"]["evaluation"]))
        self.assertEqual(budget["required_seconds"], sum(budget["stage_budget_seconds"].values()))
        self.assertGreater(budget["worst_marginal_cost_usd"], budget["expected_marginal_cost_usd"])
        self.assertAlmostEqual(budget["expected_marginal_cost_usd"],
                               round(budget["hourly_usd"] * budget["expected_extra_seconds"] / 3600, 4))
        self.assertTrue(budget["uploads_fit_static_slack"])
        # The host-side long-context cap sits inside its stage budget.
        self.assertLess(rme.LONG_CONTEXT_TOTAL_TIMEOUT, budget["stage_budget_seconds"]["long-context"])
        self.assertLessEqual(orc._EVAL_MEMBER_REMOTE_TIMEOUT, rme.EVAL_TOTAL_TIMEOUT)
        self.assertEqual(self.suite["long_context_profile"], rme.LONG_CONTEXT_PROFILE)
        self.assertEqual(rme.LONG_CONTEXT_PROFILE["sizes"], [1000, 2000, 4000, 6000])
        self.assertEqual(rme.LONG_CONTEXT_PROFILE["styles"], ["turns", "tool_loop"])
        self.assertEqual(rme.LONG_CONTEXT_PROFILE["depths"], [0.1, 0.5, 0.9])
        self.assertEqual(rme.LONG_CONTEXT_PROFILE["trials"], 1)

    def test_each_option_alone_selects_only_its_members(self):
        self.assertEqual(orc._extra_members("extended", ""), ALL_MEMBERS[1:])
        self.assertEqual(orc._extra_members("shipping", "no-boolean-coercion"), ALL_MEMBERS[:1])
        plan = _plan("--mode", "eval", "--scorer-ablation", "no-boolean-coercion")
        self.assertEqual(plan["extended_fetch_allowlist"], [EXTRA_NAMES[0]])
        self.assertEqual(plan["extended_uploads"], [])

    def test_every_new_remote_argv_passes_the_persisted_argv_validator(self):
        for command in self.plan["extended_commands"]:
            self.assertEqual(j1m_runner.validate_persisted_argv(command), command)
            joined = " ".join(command)
            for forbidden in ("--token", "token-file", "HF_TOKEN", "SHADEFORM", "secret"):
                self.assertNotIn(forbidden, joined)

    def test_unapproved_requests_are_refused_before_any_provider_call(self):
        for argv in (["--mode", "eval", "--suite", "everything"],
                     ["--mode", "eval", "--scorer-ablation", "no-scoring"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                orc.main(argv)
        with self.assertRaisesRegex(ValueError, "only available in eval mode"):
            orc.main(["--mode", "prove", "--suite", "extended"])
        with self.assertRaisesRegex(ValueError, "not combined with comparators"):
            orc.main(["--mode", "eval", "--suite", "extended", "--evaluate-comparators", "q8"])
        # The real ``execute()``, driven offline: both refusals happen before
        # any ledger row, key, upload, provider call or teardown.
        key_root = Path(tempfile.mkdtemp(prefix="extended-refusal-keys-", dir=ROOT))
        os.chmod(key_root, 0o700)
        self.addCleanup(shutil.rmtree, key_root, ignore_errors=True)
        for options, message in (({"comparators": "q8", "suite": "extended"}, "not combined with comparators"),
                                 ({"suite": "all"}, "eval suite is not an approved request"),
                                 ({"scorer_ablation": "everything"}, "scorer ablation is not an approved request")):
            recorder = dry_run.Recorder()
            run = dry_run.drive("eval", inject_failure=False, key_root=key_root, recorder=recorder, **options)
            with self.subTest(options=options):
                self.assertIn(message, run["error"] or "")
                self.assertEqual((run["cost_events"], run["teardown_calls"], recorder.entries), ([], [], []))
                self.assertFalse(run["published"])

    def test_a_stale_variants_chunk_is_refused_pre_spend(self):
        stale = dict(orc._VARIANT_CHUNK_SHA256, **{"variants-chunk-3": "0" * 64})
        with mock.patch.object(orc, "_VARIANT_CHUNK_SHA256", stale):
            with self.assertRaisesRegex(ValueError, "pinned sha256"):
                orc._extended_suite_preflight(_config(), ALL_MEMBERS)


class SalvageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.receipts = _member_receipts()
        self.identity = _salvage_identity()
        self.allowlist = orc._salvage_effective_allowlist(EXTRA_NAMES)

    def validate(self, name: str, raw: bytes, identity: dict | None = None):
        return orc._salvage_validated_payload(name, raw, identity or self.identity, allowlist=self.allowlist)

    def refused(self, name: str, raw: bytes, identity: dict | None = None) -> str:
        with self.assertRaises(orc._SalvageRefusal) as caught:
            self.validate(name, raw, identity)
        return caught.exception.code

    @staticmethod
    def edit(raw: bytes, **changes) -> bytes:
        payload = json.loads(raw)
        for key, value in changes.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    def test_extra_names_are_fetchable_only_when_requested(self):
        info = {"instance_info": {"ip": "203.0.113.17", "ssh_user": "ubuntu", "ssh_port": 22}}
        with self.assertRaises(orc._SalvageRefusal):
            orc._salvage_transport_argv(info, Path("/k"), Path("/kh"), "variants-chunk-1-receipt.json", Path("/s"))
        with self.assertRaisesRegex(ValueError, "not source-allowlisted"):
            orc._salvage_effective_allowlist(("variants-chunk-4-receipt.json",))
        with self.assertRaisesRegex(ValueError, "not source-allowlisted"):
            orc._salvage_effective_allowlist(("eval-receipt.json",))
        for name in EXTRA_NAMES:
            self.assertRegex(name, r"\A[a-z][a-z0-9-]*\.json\Z")
            self.assertIn(name, self.allowlist)

    def test_correct_member_receipts_pass_the_boundary(self):
        for name in EXTRA_NAMES:
            with self.subTest(name=name):
                self.assertEqual(self.validate(name, self.receipts[name])["suite_member"], name[:-len("-receipt.json")])

    def test_a_receipt_claiming_another_run_or_instance_is_refused(self):
        raw = self.receipts["variants-chunk-1-receipt.json"]
        self.assertEqual(self.refused("variants-chunk-1-receipt.json", self.edit(raw, run_id="J1M-OTHER")),
                         "salvage_identity_mismatch")
        self.assertEqual(self.refused("variants-chunk-1-receipt.json", self.edit(raw, instance_id="instance-other")),
                         "salvage_identity_mismatch")
        self.assertEqual(self.refused("variants-chunk-1-receipt.json", self.edit(raw, run_id=None)),
                         "salvage_identity_missing")

    def test_a_receipt_claiming_another_chunk_fixture_or_harness_is_refused(self):
        chunk3 = self.receipts["variants-chunk-3-receipt.json"]
        # Chunk 3's genuine receipt under chunk 2's name.
        self.assertEqual(self.refused("variants-chunk-2-receipt.json", chunk3), "salvage_identity_mismatch")
        # Chunk 2's stamp with chunk 3's fixture.
        self.assertEqual(self.refused("variants-chunk-2-receipt.json",
                                      self.edit(chunk3, suite_member="variants-chunk-2")),
                         "salvage_identity_mismatch")
        ablation = self.receipts["ablation-no-boolean-coercion-receipt.json"]
        self.assertEqual(self.refused("ablation-no-boolean-coercion-receipt.json", self.edit(ablation, scorer_ablation=None)),
                         "salvage_required_key_missing")
        self.assertEqual(self.refused("ablation-no-boolean-coercion-receipt.json",
                                      self.edit(ablation, scorer_ablation={"name": "ablation-no-boolean-coercion",
                                                                           "boolean_coercion": True})),
                         "salvage_identity_mismatch")
        self.assertEqual(self.refused("variants-chunk-1-receipt.json",
                                      self.edit(self.receipts["variants-chunk-1-receipt.json"],
                                                scorer_ablation={"name": "x", "boolean_coercion": False})),
                         "salvage_identity_mismatch")
        long_context = json.loads(self.receipts["long-context-receipt.json"])
        long_context["harness"]["sha256"] = "f" * 64
        self.assertEqual(self.refused("long-context-receipt.json",
                                      (json.dumps(long_context) + "\n").encode("utf-8")), "salvage_identity_mismatch")
        artifact = dict(json.loads(chunk3)["artifact"], sha256="e" * 64)
        self.assertEqual(self.refused("variants-chunk-3-receipt.json", self.edit(chunk3, artifact=artifact)),
                         "salvage_identity_mismatch")

    def test_a_member_receipt_can_never_be_published_as_the_shipping_receipt(self):
        for name in ("variants-chunk-1-receipt.json", "ablation-no-boolean-coercion-receipt.json"):
            self.assertEqual(self.refused("eval-receipt.json", self.receipts[name]), "salvage_schema_mismatch")

    def test_a_typed_failure_receipt_is_carried_and_still_bound(self):
        failure = {"schema": "local_bmo.j1m.variants-eval-receipt.v1", "status": "failed",
                   "error_code": "engine_ready_timeout", "error_type": "ValueError",
                   "suite_member": "variants-chunk-1", "prompt_response_logging": False, "tokens_logged": False,
                   "run_id": "J1MDRY", "instance_id": dry_run.FAKE_INSTANCE_ID}
        raw = (json.dumps(failure) + "\n").encode("utf-8")
        self.assertEqual(self.validate("variants-chunk-1-receipt.json", raw)["status"], "failed")
        self.assertEqual(self.refused("variants-chunk-2-receipt.json", raw), "salvage_identity_mismatch")


class MemberVerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = _PrivateDir(self)
        self.receipts = _member_receipts()
        self.artifact = _artifact()

    def test_chunk_receipts_verify_against_their_own_identity_never_the_shipping_one(self):
        path = self.dir.write("variants-chunk-1-receipt.json", self.receipts["variants-chunk-1-receipt.json"])
        verified = orc._verify_member_eval_receipt(path, self.artifact, "variants-chunk-1")
        self.assertEqual((verified["status"], verified["case_count"]), ("verified", 60))
        self.assertEqual(verified["fixture_sha256"], orc._VARIANT_CHUNK_SHA256["variants-chunk-1"])
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            orc._verify_eval_receipt(path, self.artifact)
        with self.assertRaisesRegex(ValueError, "claims another member"):
            orc._verify_member_eval_receipt(path, self.artifact, "variants-chunk-2")
        # A chunk receipt carrying the shipping fixture identity is refused.
        payload = json.loads(self.receipts["variants-chunk-1-receipt.json"])
        payload["fixture"] = orc._tool_eval_contract()["fixture_identity"]
        with self.assertRaisesRegex(ValueError, "fixture identity mismatch"):
            orc._verify_member_eval_receipt(self.dir.write("variants-chunk-1-receipt.json", payload),
                                            self.artifact, "variants-chunk-1")

    def test_ablation_receipt_is_recorded_as_an_ablation_and_refused_as_shipping(self):
        path = self.dir.write("ablation-no-boolean-coercion-receipt.json",
                              self.receipts["ablation-no-boolean-coercion-receipt.json"])
        verified = orc._verify_member_eval_receipt(path, self.artifact, "ablation-no-boolean-coercion")
        self.assertEqual(verified["scorer_ablation"], {"name": "ablation-no-boolean-coercion", "boolean_coercion": False})
        self.assertEqual(verified["fixture_sha256"], orc._tool_eval_contract()["fixture_identity"]["sha256"])
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            orc._verify_eval_receipt(path, self.artifact)
        stripped = json.loads(self.receipts["ablation-no-boolean-coercion-receipt.json"])
        stripped.pop("scorer_ablation")
        with self.assertRaisesRegex(ValueError, "ablation record invalid"):
            orc._verify_member_eval_receipt(self.dir.write("ablation-no-boolean-coercion-receipt.json", stripped),
                                            self.artifact, "ablation-no-boolean-coercion")

    def test_failed_case_attribution_carries_through_to_the_variants_summary(self):
        payload = json.loads(self.receipts["variants-chunk-2-receipt.json"])
        cases = json.loads(orc._variant_chunk_bytes("variants-chunk-2"))["cases"]
        picked = [case for case in cases if case["category"] == "argument_fidelity"][:2]
        metrics = payload["metrics"]
        metrics["passed"] -= 2
        metrics["failed"] = 2
        metrics["category_summary"]["argument_fidelity"]["passed"] -= 2
        metrics["category_summary"]["argument_fidelity"]["failed"] = 2
        metrics["quality_diagnostics"] = {
            "schema": "local_bmo.tool-call-quality-diagnostics.v1", "total_failed": 2,
            "overall": {"argument_value_mismatch": 2},
            "by_category": {category: ({"argument_value_mismatch": 2} if category == "argument_fidelity" else {})
                            for category in metrics["category_summary"]}}
        metrics["failed_cases"] = [{"id": case["id"], "category": case["category"], "reason": "argument_value_mismatch"}
                                   for case in picked]
        payload["status"] = "completed_with_failures"
        self.dir.write("variants-chunk-2-receipt.json", payload)
        phase = {"members": ["variants-chunk-2"], "stages": [{"member": "variants-chunk-2", "status": "completed"}]}
        salvage = [{"name": "variants-chunk-2-receipt.json", "status": "completed"}]
        orc._verify_extra_members(phase, salvage, self.dir.path, self.artifact, harness_sha256=None)
        receipt = phase["receipts"]["variants-chunk-2"]
        self.assertEqual(receipt["failed_cases"], metrics["failed_cases"])
        summary = phase["variants_summary"]
        self.assertEqual((summary["case_count"], summary["passed"], summary["failed"]), (60, 58, 2))
        origins = {case["id"].split(".")[1] for case in picked}
        self.assertEqual(set(summary["by_origin"]), origins)
        self.assertNotIn("cases", summary)
        j1m_runner.validate_persisted_receipt(phase)

    def test_long_context_receipt_is_bound_to_profile_harness_and_counts(self):
        raw = self.receipts["long-context-receipt.json"]
        harness = orc._extended_suite_preflight(_config(), ("long-context",))["harness_sha256"]
        path = self.dir.write("long-context-receipt.json", raw)
        verified = orc._verify_long_context_receipt(path, self.artifact, harness_sha256=harness)
        self.assertEqual((verified["status"], verified["cells_run"]), ("completed", 120))
        for mutate, message in (
            (lambda p: p["plan"].update(sizes=[1000, 2000, 4000, 8000]), "approved profile"),
            (lambda p: p["harness"].update(sha256="0" * 64), "harness identity"),
            (lambda p: p["results"]["outcomes"].update(pass_=1), "results invalid"),
            (lambda p: p["results"].update(not_passed=[{"id": "needle/turns/s1000/d0.1/t0", "outcome": "fail",
                                                       "reason": "missing_fact"}]), "results invalid"),
            (lambda p: p.update(prompt_response_logging=True), "logging policy"),
        ):
            payload = json.loads(raw)
            mutate(payload)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                orc._verify_long_context_receipt(self.dir.write("long-context-receipt.json", payload), self.artifact,
                                                 harness_sha256=harness)


class LifecycleTests(unittest.TestCase):
    """The real ``execute()``, driven by the offline dry-run harness."""

    @classmethod
    def setUpClass(cls):
        cls.key_root = Path(tempfile.mkdtemp(prefix="extended-keys-", dir=ROOT))
        os.chmod(cls.key_root, 0o700)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_root, ignore_errors=True)

    def drive(self, **kwargs):
        return dry_run.drive("eval", inject_failure=False, key_root=self.key_root,
                             recorder=dry_run.Recorder(), **kwargs)

    def test_an_extra_member_failure_leaves_the_shipping_result_intact(self):
        plain = self.drive()
        failed = self.drive(suite="extended", scorer_ablation="no-boolean-coercion",
                            fail_eval_stage="variants-chunk-2.json")
        phase = failed["extended_suite"]
        self.assertEqual(failed["status"], plain["status"])
        self.assertEqual(failed["eval_receipt_status"], "verified")
        self.assertEqual(failed["job_status"], "completed")
        self.assertEqual((phase["status"], phase["failed_member"]), ("failed", "variants-chunk-2"))
        self.assertEqual([stage["member"] for stage in phase["stages"]], list(ALL_MEMBERS[:3]))
        self.assertEqual({item["member"]: item["reason"] for item in phase["skipped"]},
                         {"variants-chunk-3": "extended_skipped_after_member_failure",
                          "long-context": "extended_skipped_after_member_failure"})
        self.assertEqual(phase["receipts"]["variants-chunk-3"], {"status": "not_attempted"})
        self.assertEqual(failed["salvage_codes"]["eval-receipt.json"], "completed")
        self.assertTrue(failed["teardown_calls"])
        self.assertEqual(failed["key_cleanup"].get("status"), "removed")

    def test_an_unexpected_phase_error_is_contained(self):
        with mock.patch.object(orc, "_run_extra_members", side_effect=RuntimeError("boom")):
            run = self.drive(suite="extended")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["eval_receipt_status"], "verified")
        self.assertEqual((run["extended_suite"]["status"], run["extended_suite"]["reason"]),
                         ("failed", "extended_phase_error"))
        self.assertTrue(run["teardown_calls"])

    def test_successful_extended_run_salvages_and_verifies_every_member(self):
        run = self.drive(suite="extended", scorer_ablation="no-boolean-coercion")
        phase = run["extended_suite"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(phase["status"], "completed")
        self.assertEqual({member: phase["receipts"][member]["status"] for member in ALL_MEMBERS},
                         {**{member: "verified" for member in ALL_MEMBERS[:4]}, "long-context": "completed"})
        self.assertEqual(phase["variants_summary"]["case_count"], 180)
        # The phase rides in the persisted lifecycle receipt, so it must pass the
        # same no-credential screen; a refusal there would be swallowed.
        j1m_runner.validate_persisted_receipt(phase)
        self.assertTrue(run["lifecycle_persisted"])
        for name in EXTRA_NAMES:
            self.assertEqual(run["salvage_codes"][name], "completed")
            self.assertIn(name, run["published"])

    def test_the_clock_gate_skips_members_that_do_not_fit(self):
        config = _config()
        phase: dict = {}
        self.enterContext(mock.patch.object(orc, "_progress"))
        with mock.patch.object(orc, "_remote") as remote:
            orc._run_extra_members(
                config, [["true"]] * 2, ("variants-chunk-1", "long-context"), ssh_prefix=["ssh"],
                execution_deadline=time.monotonic() + 1200.0, progress_path=ROOT / "nonexistent-progress.json",
                phase_id="p", phase=phase, lifecycle={})
        remote.assert_not_called()
        self.assertEqual(phase["status"], "refused")
        self.assertEqual({item["reason"] for item in phase["skipped"]}, {"extended_clock_insufficient"})
        # Enough for the first member only: it runs, the long-context one is skipped.
        phase = {}
        budget = (float(config["modes"]["eval"]["stage_budgets_seconds"]["cleanup_reserve"]) +
                  orc._DELETION_RESERVE_SECONDS + 480.0 + 60.0)
        with mock.patch.object(orc, "_remote", return_value={"status": "completed", "exit_code": 0}) as remote:
            orc._run_extra_members(
                config, [["a"], ["b"]], ("variants-chunk-1", "long-context"), ssh_prefix=["ssh"],
                execution_deadline=time.monotonic() + budget, progress_path=ROOT / "nonexistent-progress.json",
                phase_id="p", phase=phase, lifecycle={})
        self.assertEqual(remote.call_count, 1)
        self.assertLessEqual(remote.call_args.kwargs["timeout"], 480.0)
        self.assertEqual(phase["status"], "partial")
        self.assertEqual(phase["skipped"], [{"member": "long-context", "reason": "extended_clock_insufficient"}])


class ScorerAblationTests(unittest.TestCase):
    TOOLS = ev.load_fixture(ROOT / "tests" / "model" / "production_tool_call_eval.json")["tools"]
    CASE = {"id": "c", "category": "tool_selection", "messages": [],
            "expected": {"call": {"name": "mail.list_messages",
                                  "arguments": {"folder": "inbox", "unread_only": True, "limit": 10}}}}
    OUTPUT = ("<tool_call>\n<function=mail.list_messages>\n<parameter=folder>\ninbox\n</parameter>\n"
              "<parameter=unread_only>\nTrue\n</parameter>\n<parameter=limit>\n10\n</parameter>\n"
              "</function>\n</tool_call>")

    def test_the_ablation_defaults_off_and_disables_only_boolean_coercion(self):
        self.assertIs(ev.BOOLEAN_COERCION_DEFAULT, True)
        self.assertEqual(ev.evaluate_case(self.CASE, self.OUTPUT, self.TOOLS), (True, "exact_call"))
        passed, reason = ev.evaluate_case(self.CASE, self.OUTPUT, self.TOOLS, boolean_coercion=False)
        self.assertFalse(passed)
        self.assertIn(reason, ev.QUALITY_CODES)
        exact = self.OUTPUT.replace("True", "true")
        self.assertEqual(ev.evaluate_case(self.CASE, exact, self.TOOLS, boolean_coercion=False), (True, "exact_call"))
        with mock.patch.object(ev, "run_local", return_value={"errors": 0, "failed": 0, "cases": []}) as run_local, \
                mock.patch.object(ev, "aggregate_result", return_value={}), \
                mock.patch.object(ev, "load_bearer_token", return_value="t" * 32), \
                contextlib.redirect_stdout(io.StringIO()):
            ev.main(["--endpoint", "http://127.0.0.1:9/v1/chat/completions"])
            self.assertNotIn("boolean_coercion", run_local.call_args.kwargs)
            ev.main(["--endpoint", "http://127.0.0.1:9/v1/chat/completions", "--no-boolean-coercion"])
            self.assertIs(run_local.call_args.kwargs["boolean_coercion"], False)

    def test_the_host_passes_the_flag_only_for_the_ablation_member_and_records_it(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            receipt = Path(directory) / "ablation-no-boolean-coercion-receipt.json"
            fixture = ROOT / "tests" / "model" / "production_tool_call_eval.json"
            sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
            base = ["--model", "m", "--model-manifest", "mm", "--model-manifest-lock", "ml",
                    "--source-revision", "c" * 40, "--llama-revision", "b" * 40, "--llama-checkout", "x",
                    "--engine", "e", "--evaluator", "ev", "--fixture", str(fixture), "--toolchain-receipt", "t"]
            result = {"schema": "local_bmo.j1m.real-tool-eval-receipt.v1", "status": "verified",
                      "prompt_response_logging": False, "tokens_logged": False}
            artifact = {"name": "Qwen3.5-9B-Q4_K_M.gguf"}
            member = base + ["--receipt", str(receipt), "--preflight-receipt", str(Path(directory) / "p.json"),
                             "--suite-member", "ablation-no-boolean-coercion", "--expected-fixture-sha256", sha]
            with mock.patch.object(rme, "verify_artifact", return_value=artifact), \
                    mock.patch.object(rme, "_launch_and_evaluate", return_value=dict(result)) as launch, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rme.main(member), 0)
                self.assertEqual(launch.call_args.kwargs["extra_evaluator_args"], ("--no-boolean-coercion",))
                written = json.loads(receipt.read_text())
                self.assertEqual(written["schema"], "local_bmo.j1m.scorer-ablation-eval-receipt.v1")
                self.assertEqual(written["scorer_ablation"], {"name": "ablation-no-boolean-coercion",
                                                              "boolean_coercion": False})
                shipping = Path(directory) / "eval-receipt.json"
                self.assertEqual(rme.main(base + ["--receipt", str(shipping)]), 0)
                self.assertEqual(launch.call_args.kwargs, {"deadline": mock.ANY})
                plain = json.loads(shipping.read_text())
                self.assertEqual(plain["schema"], "local_bmo.j1m.real-tool-eval-receipt.v1")
                self.assertNotIn("suite_member", plain)
                self.assertNotIn("scorer_ablation", plain)

    def test_a_member_with_the_wrong_fixture_is_refused_before_the_model_is_hashed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            receipt = Path(directory) / "variants-chunk-1-receipt.json"
            argv = ["--model", "m", "--model-manifest", "mm", "--model-manifest-lock", "ml",
                    "--source-revision", "c" * 40, "--llama-revision", "b" * 40, "--llama-checkout", "x",
                    "--engine", "e", "--evaluator", "ev",
                    "--fixture", str(ROOT / "tests" / "model" / "production_tool_call_eval.json"),
                    "--toolchain-receipt", "t", "--receipt", str(receipt),
                    "--preflight-receipt", str(Path(directory) / "p.json"),
                    "--suite-member", "variants-chunk-1",
                    "--expected-fixture-sha256", orc._VARIANT_CHUNK_SHA256["variants-chunk-1"]]
            with mock.patch.object(rme, "verify_artifact") as verify, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rme.main(argv), 1)
                verify.assert_not_called()
                written = json.loads(receipt.read_text())
                self.assertEqual((written["schema"], written["error_code"], written["suite_member"]),
                                 ("local_bmo.j1m.variants-eval-receipt.v1", "evaluator_fixture_identity_mismatch",
                                  "variants-chunk-1"))
                # A member may never write over the shipping evidence paths.
                clash = argv[:argv.index("--receipt")] + ["--receipt", str(Path(directory) / "eval-receipt.json")] + \
                    argv[argv.index("--receipt") + 2:]
                self.assertEqual(rme.main(clash), 1)
                self.assertEqual(json.loads((Path(directory) / "eval-receipt.json").read_text())["error_code"],
                                 "suite_member_invalid")
                verify.assert_not_called()


class LongContextHostTests(unittest.TestCase):
    """The host side of the long-context member, against a loopback fake engine."""

    def test_the_harness_runs_against_the_running_engine_and_publishes_no_text(self):
        from tests.model.long_context_fake_engine import SECRET_OUTPUT_MARKER, FakeLongContextEngine

        class LiveEngine:
            pid = 1

            def poll(self):
                return None

        directory = _PrivateDir(self)
        fixture = ROOT / "tests" / "model" / "production_tool_call_eval.json"
        args = argparse.Namespace(long_context_harness=str(orc._LONG_CONTEXT_HARNESS),
                                  evaluator=str(ROOT / "scripts" / "test" / "evaluate_tool_calls.py"),
                                  fixture=str(fixture))
        artifact = _artifact()
        with FakeLongContextEngine(effective_window=3000) as fake, tempfile.TemporaryDirectory() as private:
            token = Path(private) / "engine-token"
            token.write_text(fake.token + "\n", encoding="utf-8")
            token.chmod(0o600)
            receipt = rme._long_context_session(
                args=args, artifact=artifact, port=int(fake.base.rsplit(":", 1)[1]), token_file=token,
                process=LiveEngine(), deadline=time.monotonic() + 600, started=time.monotonic(),
                fixture_identity=rme._fixture_contract(fixture)["fixture_identity"], context_tokens=8192,
                build_info={"llama_cpp_revision": artifact["llama_cpp_revision"], "model": "qwen35-9b-q4-k-m"},
                model_preflight={"status": "verified", "size_bytes": artifact["size_bytes"],
                                 "sha256": artifact["sha256"], "gguf_version": 3})
            sent = json.dumps(fake.requests)
        stamped = {**rme._stamp_suite_member(receipt, "long-context"), "run_id": "r", "instance_id": "i"}
        encoded = json.dumps(stamped, indent=2, sort_keys=True) + "\n"
        self.assertLess(len(encoded), rme.MAX_RECEIPT_BYTES)
        self.assertGreater(len(fake.requests), 120)
        self.assertIn("for project", sent, "facts really were planted in the prompts")
        for leaked in (fake.token, SECRET_OUTPUT_MARKER, "<tool_call>", "for project"):
            self.assertNotIn(leaked, encoded)
        j1m_runner.validate_persisted_receipt(json.loads(encoded))
        j1m_runner.validate_persisted_document(encoded)
        results = receipt["results"]
        self.assertEqual((receipt["status"], results["cells_run"], results["cells_planned"]), ("completed", 120, 120))
        self.assertGreater(results["outcomes"]["fail"], 0, "the fake's effective window produces real misses")
        harness = orc._extended_suite_preflight(_config(), ("long-context",))["harness_sha256"]
        verified = orc._verify_long_context_receipt(directory.write("long-context-receipt.json", stamped), artifact,
                                                    harness_sha256=harness)
        self.assertEqual(verified["outcomes"], results["outcomes"])

    def test_the_distiller_refuses_anything_but_typed_cells(self):
        base = {"schema": "local_bmo.long-context-eval.v1", "prompt_response_logging": False, "complete": True,
                "engine_context_tokens": 8192, "stopped_early": None,
                "cells": [{"id": "needle/turns/s1000/d0.1/t0", "probe": "needle", "style": "turns",
                           "target_tokens": 1000, "depth": 0.1, "outcome": "pass", "reason": "recalled"}]}
        self.assertEqual(rme._distill_long_context(base)["outcomes"]["pass"], 1)
        for change in ({"id": "the code word for project Orion is AMBER"}, {"outcome": "great"},
                       {"reason": "The model said: hello"}, {"style": "single"}, {"target_tokens": 9000}):
            bad = copy.deepcopy(base)
            bad["cells"][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "long_context_receipt_invalid"):
                rme._distill_long_context(bad)
        with self.assertRaisesRegex(ValueError, "long_context_receipt_invalid"):
            rme._distill_long_context({**base, "prompt_response_logging": True})


if __name__ == "__main__":
    unittest.main()
