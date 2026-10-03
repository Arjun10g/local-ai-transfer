"""The opt-in memory-eval member (``--suite memory``), proved offline.

``scripts/j1m_orchestrator.py --mode eval --suite memory`` runs the shipping
eval stage unchanged and then ONLY the memory member:
``scripts/test/memory_eval.py`` driven against the same freshly built engine
with the host's own ``host/agent/memory-prompts.json``. Proved here without a
network, a provider or a model:

* the default, ``--suite extended`` and ablation plans are byte-identical to
  the plans captured before this slice, and the extended lifecycle matches the
  committed ``HEAD`` sources;
* the memory plan is the memory member alone, with its own explicit 660 s
  stage budget, three bounded uploads and an unchanged cost ceiling;
* the harness, its long-context dependency and the prompt file are pinned by
  sha256 at plan time, on the host before the model is hashed, at the salvage
  boundary and in the verifier, so a run can never pass for another prompt
  version;
* the host drives the real harness against a loopback fake engine and
  publishes a recomputed, bounded receipt with no prompt, note or model text;
* a member failure never touches the shipping result.
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
from scripts.test import memory_eval as me
from scripts.test import remote_model_eval as rme

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
SHIPPING_FIXTURE = ROOT / "tests" / "model" / "production_tool_call_eval.json"

# Digests of the eval plans (``created_at_utc`` removed) captured from the
# committed code at 4d4020a BEFORE the memory member existed. They must not move.
PRE_MEMORY_PLAN_SHA256 = {
    (): "73de2d8354aa5f5fd40e4a8b238f3dd6a1eeb4ee03f0bc534dd81635e6aea716",
    ("--suite", "extended"): "e17daf390c11bcf26cfecb09b3ee0f33eb5c16cbbbc5a95004af54ccdef3652b",
    ("--suite", "extended", "--scorer-ablation", "no-boolean-coercion"):
        "8c7add581f26ae65a83adeb68294e30f79f9e284a4c43e1a7b6040f07f8913a6",
    ("--scorer-ablation", "no-boolean-coercion"): "27d4a08230b2e2a7958988ecd108d2bd52a1dcc1b193f0c65a0e09f704090249",
}


def _plan(*argv: str) -> dict:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert orc.main(["--mode", "eval", *argv]) == 0
    return json.loads(stdout.getvalue())


def _digest(plan: dict) -> str:
    plan = {key: value for key, value in plan.items() if key != "created_at_utc"}
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact() -> dict:
    return orc._verify_eval_artifact(None, ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json",
                                     j1m_runner.load_config())


def _memory_identity() -> dict:
    return orc._memory_preflight(orc._tool_eval_contract())


def _receipts() -> dict[str, bytes]:
    return dry_run.fake_receipts(j1m_runner.load_config(), _artifact(), run_id="J1MDRY",
                                 instance_id=dry_run.FAKE_INSTANCE_ID, extended=True)


class _PrivateDir:
    def __init__(self, case: unittest.TestCase):
        self.path = Path(tempfile.mkdtemp(dir=FIXTURES, prefix=".memory-suite-"))
        case.addCleanup(shutil.rmtree, self.path, ignore_errors=True)

    def write(self, name: str, payload) -> Path:
        path = self.path / name
        data = payload if isinstance(payload, bytes) else (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        path.write_bytes(data)
        path.chmod(0o600)
        return path


class ExistingPlansAreUnchangedTests(unittest.TestCase):
    def test_default_extended_and_ablation_plans_match_the_pre_memory_digests(self):
        for argv, digest in PRE_MEMORY_PLAN_SHA256.items():
            with self.subTest(argv=argv):
                plan = _plan(*argv)
                self.assertEqual(_digest(plan), digest)
                self.assertNotIn("memory", json.dumps(plan.get("extended_suite", {})).replace("memory_", ""))

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_extended_lifecycle_is_identical_to_the_committed_sources(self):
        """Drive the real extended ``execute()`` with HEAD and working-tree code."""

        with tempfile.TemporaryDirectory() as directory:
            head = Path(directory)
            for relative in ("scripts/j1m_orchestrator.py", "scripts/j1m_dry_run.py"):
                shown = subprocess.run(["git", "show", f"HEAD:{relative}"], cwd=ROOT, capture_output=True, check=False)
                if shown.returncode != 0:
                    self.skipTest("HEAD sources are unavailable")
                (head / Path(relative).name).write_bytes(shown.stdout)
            if b'"extended"' not in (head / "j1m_orchestrator.py").read_bytes():
                self.skipTest("HEAD predates the extended suite")
            outputs = [subprocess.run([sys.executable, "-c", _HEAD_COMPARISON, str(head), which], cwd=ROOT,
                                      capture_output=True, text=True, timeout=300, check=False)
                       for which in ("head", "work")]
        for output in outputs:
            self.assertEqual(output.returncode, 0, output.stderr[-2000:])
        self.assertEqual(outputs[0].stdout, outputs[1].stdout)
        self.assertIn("long-context-receipt.json", outputs[1].stdout)


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
    orch.ROOT = ROOT; orch._LONG_CONTEXT_HARNESS = ROOT / "scripts" / "test" / "long_context_eval.py"
    # Once HEAD itself carries the memory suite its pinned files must also point at the repository.
    for attribute, relative in (("_MEMORY_HARNESS", "scripts/test/memory_eval.py"), ("_MEMORY_PROMPTS", "host/agent/memory-prompts.json")):
        if hasattr(orch, attribute): setattr(orch, attribute, ROOT / relative)
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
    out = dry.drive("eval", inject_failure=False, key_root=key_root, recorder=recorder,
                    suite="extended", scorer_ablation="no-boolean-coercion")
def norm(value):
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r"/[^\"]*?/j1m-[^\"/]*?-[a-z0-9_]{8}/", "<TMP>/", text)
    text = re.sub(r"j1m-[0-9a-f]{32}", "j1m-<NONCE>", text)
    text = re.sub(r"J1MDRY-[0-9a-f]{32}", "J1MDRY-<NONCE>", text)
    text = re.sub(r"\"duration_ms\": \d+", "\"duration_ms\": 0", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT[0-9:.+]+", "<TS>", text)
    text = re.sub(r"\"watchdog_pid\": \d+", "\"watchdog_pid\": 0", text)
    text = re.sub(r"\"available_seconds\": [0-9.]+", "\"available_seconds\": 0", text)
    return text
print(json.dumps({"argv": norm([entry["argv"] for entry in recorder.entries]),
                  "salvage": norm(captured["salvage"]), "lifecycle": norm(captured["lifecycle"]),
                  "status": out["status"], "published": out["published"],
                  "codes": out["salvage_codes"], "progress": out["progress"]}, sort_keys=True))
'''


class MemoryPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = _plan("--suite", "memory")
        self.suite = self.plan["extended_suite"]

    def test_the_memory_suite_is_the_shipping_eval_then_the_memory_member_only(self):
        self.assertEqual(self.suite["members"], ["memory"])
        self.assertEqual(self.plan["commands"], _plan()["commands"])
        self.assertEqual(self.plan["extended_fetch_allowlist"], ["memory-receipt.json"])
        self.assertEqual(self.plan["extended_uploads"], [
            "/scratch/j1m/long_context_eval.py", "/scratch/j1m/memory_eval.py", "/scratch/j1m/memory-prompts.json"])
        self.assertEqual(len(self.plan["extended_commands"]), 1)
        joined = " ".join(self.plan["extended_commands"][0])
        for absent in ("variants-chunk", "long-context-receipt", "--no-boolean-coercion", "ablation"):
            self.assertNotIn(absent, joined)
        self.assertEqual(orc._extra_members("memory", ""), ("memory",))
        self.assertEqual(orc._extra_members("memory", "no-boolean-coercion"), ("ablation-no-boolean-coercion", "memory"))

    def test_harness_and_prompt_versions_are_pinned_in_the_plan_and_the_argv(self):
        identity = self.suite["memory_identity"]
        self.assertEqual(identity["harness_sha256"], _sha(orc._MEMORY_HARNESS))
        self.assertEqual(identity["long_context_sha256"], _sha(orc._LONG_CONTEXT_HARNESS))
        self.assertEqual(identity["memory_prompts_sha256"], _sha(orc._MEMORY_PROMPTS))
        self.assertEqual(identity["memory_prompts_version"], json.loads(orc._MEMORY_PROMPTS.read_text())["version"])
        command = self.plan["extended_commands"][0]
        self.assertEqual(command[command.index("--memory-prompts-sha256") + 1], identity["memory_prompts_sha256"])
        self.assertEqual(command[command.index("--timeout") + 1], str(int(rme.MEMORY_TOTAL_TIMEOUT)))
        self.assertEqual(j1m_runner.validate_persisted_argv(command), command)
        self.assertEqual(self.suite["memory_profile"], rme.MEMORY_PROFILE)

    def test_budget_is_explicit_bounded_and_does_not_raise_the_ceiling(self):
        budget = self.suite["budget"]
        self.assertEqual(budget["stage_budget_seconds"], {"memory": 660.0})
        self.assertLess(rme.MEMORY_TOTAL_TIMEOUT, budget["stage_budget_seconds"]["memory"])
        self.assertIs(budget["raises_authorized_cost"], False)
        self.assertEqual(budget["authorized_active_cost_usd"], self.plan["mode_active_cost_usd"])
        self.assertEqual(budget["extra_upload_count"], 3)
        self.assertTrue(budget["uploads_fit_static_slack"])
        self.assertEqual(budget["required_seconds"], 660.0)
        self.assertLess(budget["expected_marginal_cost_usd"], budget["worst_marginal_cost_usd"])
        # The host cap is the hard bound, and it is more than 3x the estimate.
        self.assertGreaterEqual(rme.MEMORY_TOTAL_TIMEOUT, 3 * budget["expected_stage_seconds"]["memory"])

    def test_profile_matches_the_harness_and_its_request_count(self):
        self.assertEqual(rme.MEMORY_FACT_TYPES, tuple(me.FACT_TYPES))
        self.assertEqual(rme.MEMORY_PROBES, tuple(me.PROBES))
        self.assertEqual(rme.MEMORY_OUTCOMES, tuple(sorted(me.OUTCOMES)))
        self.assertEqual(rme.MEMORY_AGE_BUCKETS, tuple(me.AGE_BUCKETS))
        self.assertEqual(_memory_identity()["planned_requests"], 300)
        self.assertEqual(rme.MEMORY_PROFILE["max_requests"], 300)
        self.assertEqual(rme._memory_planned_requests(rme.MEMORY_PROFILE), 300)

    def test_a_changed_prompt_file_changes_the_pinned_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            changed = Path(directory) / "memory-prompts.json"
            payload = json.loads(orc._MEMORY_PROMPTS.read_text())
            payload["version"] = "memory-prompts.v2"
            changed.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(orc, "_MEMORY_PROMPTS", changed):
                other = _memory_identity()
        self.assertNotEqual(other["memory_prompts_sha256"], _memory_identity()["memory_prompts_sha256"])
        self.assertEqual(other["memory_prompts_version"], "memory-prompts.v2")

    def test_unapproved_combinations_are_refused_before_any_spend(self):
        with self.assertRaisesRegex(ValueError, "only available in eval mode"):
            orc.main(["--mode", "prove", "--suite", "memory"])
        with self.assertRaisesRegex(ValueError, "not combined with comparators"):
            orc.main(["--mode", "eval", "--suite", "memory", "--evaluate-comparators", "q8"])
        key_root = Path(tempfile.mkdtemp(prefix="memory-refusal-keys-", dir=ROOT))
        os.chmod(key_root, 0o700)
        self.addCleanup(shutil.rmtree, key_root, ignore_errors=True)
        recorder = dry_run.Recorder()
        run = dry_run.drive("eval", inject_failure=False, key_root=key_root, recorder=recorder,
                            comparators="q8", suite="memory")
        self.assertIn("not combined with comparators", run["error"] or "")
        self.assertEqual((run["cost_events"], run["teardown_calls"], recorder.entries), ([], [], []))


class MemorySalvageTests(unittest.TestCase):
    def setUp(self):
        self.receipts = _receipts()
        preflight = orc._extended_suite_preflight(j1m_runner.load_config(), ("memory",))
        self.identity = {"run_id": "J1MDRY", "instance_id": dry_run.FAKE_INSTANCE_ID, "artifact": _artifact(),
                         "fixture_sha256": orc._tool_eval_contract()["fixture_identity"]["sha256"],
                         "extra_bindings": orc._extra_salvage_bindings(preflight)}
        self.allowlist = orc._salvage_effective_allowlist(("memory-receipt.json",))

    def refused(self, raw: bytes) -> str:
        with self.assertRaises(orc._SalvageRefusal) as caught:
            orc._salvage_validated_payload("memory-receipt.json", raw, self.identity, allowlist=self.allowlist)
        return caught.exception.code

    def edited(self, change) -> bytes:
        payload = json.loads(self.receipts["memory-receipt.json"])
        change(payload)
        return (json.dumps(payload) + "\n").encode("utf-8")

    def test_a_correct_receipt_passes_and_is_not_fetchable_by_default(self):
        payload = orc._salvage_validated_payload("memory-receipt.json", self.receipts["memory-receipt.json"],
                                                 self.identity, allowlist=self.allowlist)
        self.assertEqual(payload["suite_member"], "memory")
        self.assertNotIn("memory-receipt.json", orc._salvage_effective_allowlist())
        self.assertNotIn("memory-receipt.json", orc._salvage_effective_allowlist(("long-context-receipt.json",)))

    def test_another_prompt_version_harness_run_or_member_is_refused(self):
        cases = {
            "prompts": (lambda p: p["memory_prompts"].update(sha256="0" * 64), "salvage_identity_mismatch"),
            "harness": (lambda p: p["harness"].update(sha256="1" * 64), "salvage_identity_mismatch"),
            "long_context": (lambda p: p["harness"].update(long_context_sha256="2" * 64), "salvage_identity_mismatch"),
            "run": (lambda p: p.update(run_id="J1M-OTHER"), "salvage_identity_mismatch"),
            "instance": (lambda p: p.update(instance_id="instance-other"), "salvage_identity_mismatch"),
            "unbound": (lambda p: p.pop("run_id"), "salvage_identity_missing"),
            "member": (lambda p: p.update(suite_member="long-context"), "salvage_identity_mismatch"),
            "artifact": (lambda p: p["artifact"].update(sha256="e" * 64), "salvage_identity_mismatch"),
            "no_prompts": (lambda p: p.pop("memory_prompts"), "salvage_required_key_missing"),
        }
        for label, (change, code) in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.refused(self.edited(change)), code)
        # Another member's genuine receipt under the memory name.
        self.assertEqual(self.refused(self.receipts["long-context-receipt.json"]), "salvage_schema_mismatch")


class MemoryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = _PrivateDir(self)
        self.raw = _receipts()["memory-receipt.json"]
        self.binding = _memory_identity()

    def verify(self, payload):
        return orc._verify_memory_receipt(self.dir.write("memory-receipt.json", payload), _artifact(), binding=self.binding)

    def test_a_correct_receipt_verifies_with_its_prompt_version(self):
        verified = self.verify(self.raw)
        self.assertEqual((verified["status"], verified["cells_run"]), ("completed", 288))
        self.assertEqual(verified["memory_prompts"]["sha256"], self.binding["memory_prompts_sha256"])

    def test_tampered_receipts_are_refused(self):
        for change, message in (
            (lambda p: p["memory_prompts"].update(version="memory-prompts.v0"), "prompt identity"),
            (lambda p: p["memory_prompts"].update(sha256="0" * 64), "prompt identity"),
            (lambda p: p["harness"].update(sha256="0" * 64), "harness identity"),
            (lambda p: p["plan"].update(conversations=7), "approved profile"),
            (lambda p: p["results"]["outcomes"].update(fail=1), "results invalid"),
            (lambda p: p["results"].update(requests_sent=999), "results invalid"),
            (lambda p: p["results"].update(not_passed=[{"conversation": "conv0", "arm": "note",
                                                        "probe": "name", "outcome": "fail",
                                                        "reason": "missing_fact"}]), "results invalid"),
            (lambda p: p.update(prompt_response_logging=True), "logging policy"),
            (lambda p: p.update(status="verified"), "schema mismatch"),
        ):
            payload = json.loads(self.raw)
            change(payload)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.verify(payload)
        with self.assertRaisesRegex(ValueError, "harness identity"):
            orc._verify_memory_receipt(self.dir.write("memory-receipt.json", self.raw), _artifact(), binding=None)


class MemoryLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_root = Path(tempfile.mkdtemp(prefix="memory-keys-", dir=ROOT))
        os.chmod(cls.key_root, 0o700)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_root, ignore_errors=True)

    def drive(self, **kwargs):
        return dry_run.drive("eval", inject_failure=False, key_root=self.key_root, recorder=dry_run.Recorder(), **kwargs)

    def test_a_memory_member_failure_leaves_the_shipping_result_intact(self):
        plain = self.drive()
        failed = self.drive(suite="memory", fail_eval_stage="/scratch/j1m/artifacts/memory-receipt.json")
        phase = failed["extended_suite"]
        self.assertEqual(failed["status"], plain["status"])
        self.assertEqual((failed["eval_receipt_status"], failed["job_status"]), ("verified", "completed"))
        self.assertEqual((phase["status"], phase["failed_member"]), ("failed", "memory"))
        self.assertEqual(failed["salvage_codes"]["eval-receipt.json"], "completed")
        self.assertTrue(failed["teardown_calls"])

    def test_a_successful_memory_run_salvages_and_verifies_only_its_member(self):
        run = self.drive(suite="memory")
        phase = run["extended_suite"]
        self.assertEqual((run["status"], phase["status"]), ("completed", "completed"))
        self.assertEqual(phase["receipts"]["memory"]["status"], "completed")
        self.assertEqual(phase["receipts"]["memory"]["memory_prompts"]["sha256"], _sha(orc._MEMORY_PROMPTS))
        self.assertEqual(run["salvage_codes"]["memory-receipt.json"], "completed")
        for absent in ("long-context-receipt.json", "variants-chunk-1-receipt.json",
                       "ablation-no-boolean-coercion-receipt.json"):
            self.assertNotIn(absent, run["salvage_codes"])
        j1m_runner.validate_persisted_receipt(phase)
        self.assertTrue(run["lifecycle_persisted"])

    def test_the_clock_gate_refuses_a_memory_stage_that_does_not_fit(self):
        config = j1m_runner.load_config()
        phase: dict = {}
        cleanup = float(config["modes"]["eval"]["stage_budgets_seconds"]["cleanup_reserve"])
        with mock.patch.object(orc, "_remote") as remote, mock.patch.object(orc, "_progress"):
            orc._run_extra_members(
                config, [["true"]], ("memory",), ssh_prefix=["ssh"],
                execution_deadline=time.monotonic() + cleanup + orc._DELETION_RESERVE_SECONDS + 600.0,
                progress_path=ROOT / "nonexistent-progress.json", phase_id="p", phase=phase, lifecycle={})
        remote.assert_not_called()
        self.assertEqual(phase["skipped"], [{"member": "memory", "reason": "extended_clock_insufficient"}])


class MemoryHostTests(unittest.TestCase):
    """The host side, against the memory evaluator's own loopback fake engine."""

    def args(self, **overrides):
        values = dict(memory_harness=str(orc._MEMORY_HARNESS), long_context_harness=str(orc._LONG_CONTEXT_HARNESS),
                      memory_prompts=str(orc._MEMORY_PROMPTS), memory_prompts_sha256=_sha(orc._MEMORY_PROMPTS),
                      evaluator=str(ROOT / "scripts" / "test" / "evaluate_tool_calls.py"), fixture=str(SHIPPING_FIXTURE))
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_the_harness_runs_against_the_running_engine_and_publishes_no_text(self):
        from tests.model.long_context_fake_engine import SECRET_OUTPUT_MARKER
        from tests.model.test_memory_eval import NOTE_MARKER, PROMPT_MARKER, FakeMemoryEngine

        class LiveEngine:
            pid = 1

            def poll(self):
                return None

        artifact = _artifact()
        with FakeMemoryEngine(hallucinate=True) as fake, tempfile.TemporaryDirectory() as private:
            token = Path(private) / "engine-token"
            token.write_text(fake.token + "\n", encoding="utf-8")
            token.chmod(0o600)
            receipt = rme._memory_session(
                args=self.args(), artifact=artifact, port=int(fake.base.rsplit(":", 1)[1]), token_file=token,
                process=LiveEngine(), deadline=time.monotonic() + 600, started=time.monotonic(),
                fixture_identity=rme._fixture_contract(SHIPPING_FIXTURE)["fixture_identity"], context_tokens=8192,
                build_info={"llama_cpp_revision": artifact["llama_cpp_revision"], "model": "qwen35-9b-q4-k-m"},
                model_preflight={"status": "verified", "size_bytes": artifact["size_bytes"],
                                 "sha256": artifact["sha256"], "gguf_version": 3})
            requests = len(fake.requests)
            sent = json.dumps(fake.requests)
        stamped = {**rme._stamp_suite_member(receipt, "memory"), "run_id": "r", "instance_id": "i"}
        encoded = json.dumps(stamped, indent=2, sort_keys=True) + "\n"
        self.assertEqual(requests, 300)
        self.assertIn("for project", sent)
        for leaked in (fake.token, SECRET_OUTPUT_MARKER, NOTE_MARKER, PROMPT_MARKER, "for project", "<tool_call>",
                       "credential_masking"):
            self.assertNotIn(leaked, encoded)
        self.assertLess(len(encoded), rme.MAX_RECEIPT_BYTES)
        j1m_runner.validate_persisted_receipt(json.loads(encoded))
        j1m_runner.validate_persisted_document(encoded)
        results = receipt["results"]
        self.assertEqual((receipt["status"], results["cells_run"], results["requests_sent"]), ("completed", 288, 300))
        self.assertEqual(results["by_arm_probe"]["note/hallucination"]["fail"], 6, "the fake invents a value")
        directory = _PrivateDir(self)
        verified = orc._verify_memory_receipt(directory.write("memory-receipt.json", stamped), artifact,
                                              binding=_memory_identity())
        self.assertEqual(verified["outcomes"], results["outcomes"])

    def test_the_session_refuses_a_prompt_file_it_was_not_told_to_expect(self):
        with tempfile.TemporaryDirectory() as private:
            token = Path(private) / "t"
            token.write_text("t" * 40)
            with self.assertRaisesRegex(ValueError, "memory_prompts_identity_mismatch"):
                rme._memory_session(args=self.args(memory_prompts_sha256="0" * 64), artifact={}, port=9,
                                    token_file=token, process=None, deadline=time.monotonic() + 60,
                                    started=time.monotonic(), fixture_identity={}, context_tokens=8192,
                                    build_info={}, model_preflight={})

    def _main(self, directory: Path, *extra: str) -> tuple[int, dict]:
        receipt = directory / "memory-receipt.json"
        argv = ["--model", "m", "--model-manifest", "mm", "--model-manifest-lock", "ml",
                "--source-revision", "c" * 40, "--llama-revision", "b" * 40, "--llama-checkout", "x",
                "--engine", "e", "--evaluator", "ev", "--fixture", str(SHIPPING_FIXTURE),
                "--toolchain-receipt", "t", "--receipt", str(receipt),
                "--preflight-receipt", str(directory / "p.json"),
                "--expected-fixture-sha256", _sha(SHIPPING_FIXTURE), *extra]
        with contextlib.redirect_stdout(io.StringIO()):
            status = rme.main(argv)
        return status, json.loads(receipt.read_text())

    def test_the_host_refuses_a_wrong_prompt_version_before_hashing_the_model(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                mock.patch.object(rme, "verify_artifact") as verify, \
                mock.patch.object(rme, "_launch_and_evaluate") as launch:
            member = ["--suite-member", "memory", "--long-context-harness", str(orc._LONG_CONTEXT_HARNESS),
                      "--memory-harness", str(orc._MEMORY_HARNESS), "--memory-prompts", str(orc._MEMORY_PROMPTS)]
            status, written = self._main(Path(directory), *member, "--memory-prompts-sha256", "0" * 64)
            self.assertEqual((status, written["error_code"]), (1, "memory_prompts_identity_mismatch"))
            self.assertEqual(written["schema"], "local_bmo.j1m.memory-eval-receipt.v1")
            verify.assert_not_called()
            # Memory options without the long-context dependency, or on another member.
            status, written = self._main(Path(directory), *member[:2], *member[4:],
                                         "--memory-prompts-sha256", _sha(orc._MEMORY_PROMPTS))
            self.assertEqual(written["error_code"], "suite_member_invalid")
            status, written = self._main(Path(directory), "--suite-member", "ablation-no-boolean-coercion",
                                         "--memory-prompts-sha256", _sha(orc._MEMORY_PROMPTS))
            self.assertEqual(written["error_code"], "suite_member_invalid")
            verify.assert_not_called()
            launch.assert_not_called()
            # The right digest reaches the engine with the memory session.
            verify.return_value = {"name": "Qwen3.5-9B-Q4_K_M.gguf"}
            launch.return_value = {"schema": "x", "status": "completed", "prompt_response_logging": False,
                                   "tokens_logged": False}
            status, written = self._main(Path(directory), *member, "--memory-prompts-sha256", _sha(orc._MEMORY_PROMPTS))
            self.assertEqual(status, 0)
            self.assertIs(launch.call_args.kwargs["session"], rme._memory_session)
            self.assertEqual(written["schema"], "local_bmo.j1m.memory-eval-receipt.v1")

    def test_the_distiller_refuses_anything_but_typed_cells_and_the_pinned_plan(self):
        digest = _sha(orc._MEMORY_PROMPTS)
        plan = {"conversations": 6, "arms": ["note", "drop", "full", "recall", "both", "recall_gap"], "chunks": 2, "dropped_tokens": 2500,
                "retained_tokens": 1500, "note_tokens": 512, "note_bytes": 1536, "max_input_bytes": 6144,
                "per_message_bytes": 1536, "max_tokens": 64, "planned_requests": 300,
                "credential_masking": "not applied"}
        cell = {"arm": "note", "probe": "name", "age_turns": 12, "in_dropped": True, "outcome": "pass",
                "passed": True, "reason": "recalled", "coherent": True, "prompt_tokens": 1800, "seconds": 0.4}
        base = {"schema": "local_bmo.memory-eval.v1", "prompt_response_logging": False, "complete": True,
                "memory_prompts": {"sha256": digest, "version": "memory-prompts.v1"}, "plan": plan,
                "conversations": [{"id": "conv0", "cells": [cell], "complete": True,
                                   "note": {"outcome": "ok", "facts_in_note": {"name": True}}}]}
        self.assertEqual(rme._distill_memory(base, prompts_sha256=digest)["outcomes"]["pass"], 1)
        for path, value in ((("conversations", 0, "cells", 0, "arm"), "everything"),
                            (("conversations", 0, "cells", 0, "probe"), "secret_probe"),
                            (("conversations", 0, "cells", 0, "reason"), "The note said: Thessaly"),
                            (("conversations", 0, "id"), "the code word for project Orion"),
                            (("plan", "conversations"), 7),
                            (("plan", "arms"), ["note", "drop"])):
            bad = copy.deepcopy(base)
            target = bad
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "memory_receipt_invalid"):
                rme._distill_memory(bad, prompts_sha256=digest)
        with self.assertRaisesRegex(ValueError, "memory_prompts_identity_mismatch"):
            rme._distill_memory(base, prompts_sha256="0" * 64)

    def test_the_worst_case_receipt_stays_inside_the_host_bound(self):
        digest = _sha(orc._MEMORY_PROMPTS)
        conversations = [{"id": f"conv{index}", "complete": True, "note": {"outcome": "error", "reason": "client_timeout"},
                          "cells": [{"arm": arm, "probe": probe, "age_turns": 40, "in_dropped": True,
                                     "outcome": "timeout", "reason": "client_timeout_" + "x" * 49}
                                    for arm in ("note", "drop", "full", "recall", "both", "recall_gap") for probe in rme.MEMORY_PROBES]}
                         for index in range(6)]
        plan = {"conversations": 6, "arms": ["note", "drop", "full", "recall", "both", "recall_gap"], "chunks": 2, "dropped_tokens": 2500,
                "retained_tokens": 1500, "note_tokens": 512, "note_bytes": 1536, "max_input_bytes": 6144,
                "per_message_bytes": 1536, "max_tokens": 64, "planned_requests": 300}
        results = rme._distill_memory({"schema": "local_bmo.memory-eval.v1", "prompt_response_logging": False,
                                       "memory_prompts": {"sha256": digest}, "plan": plan,
                                       "conversations": conversations}, prompts_sha256=digest)
        receipt = {"schema": "s", "status": "partial", "results": results, "artifact": _artifact(),
                   "fixture": rme._fixture_contract(SHIPPING_FIXTURE)["fixture_identity"],
                   "plan": rme.MEMORY_PROFILE, "harness": {"sha256": "a" * 64}, "run_id": "r" * 64,
                   "instance_id": "i" * 64}
        self.assertEqual(results["not_passed_total"], 288)
        self.assertEqual(len(results["not_passed"]), rme.MEMORY_NOT_PASSED_LISTED)
        self.assertLess(len(json.dumps(receipt, indent=2, sort_keys=True)), rme.MAX_RECEIPT_BYTES)


if __name__ == "__main__":
    unittest.main()
