"""Rehearse offline every receipt the remote plan writes on a paid host.

Every 2026-09-11/12 remote failure was a receipt being validated or written,
not the model pipeline: the 2026-09-04 build proved all 28 plan stages run on
a real host in 9.6 minutes. Each of those failures was reproducible on this
machine against artifacts already tracked in the repo -- and none of them was,
because nothing exercised these paths offline.

This is that gate. It costs nothing and it runs the PRODUCTION handlers, not
imitations of them:

  * plan stage 27, `--inspect-tensors`, end to end. The only remote-only piece
    is the GGUF binary parse, so `GGUFReader` is stubbed from the real
    `tensor-metadata.json` the 2026-09-04 build produced -- the field values,
    the 427 tensors and the model's own Jinja chat template are genuine.
  * `run_commands` over a real subprocess whose output is large, multi-byte
    and full of `%`, which is what `git clone` and `pip wheel` emit.
  * `write_progress` and the document validator over every tracked receipt.

If this file passes and a remote run still fails writing a receipt, the cause
is genuinely remote.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _StubField:
    """Matches what `_reader_field_value` consumes: a `contents()` callable."""

    def __init__(self, value):
        self._value = value

    def contents(self):
        return self._value


class _StubTensor:
    def __init__(self, name, shape, tensor_type):
        self.name = name
        self.shape = shape
        self.tensor_type = tensor_type


class ReceiptRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load(ROOT / "scripts/j1m_runner.py", "rehearsal_runner")
        cls.reference = json.loads(
            (ROOT / "artifacts/qwen35-9b/tensor-metadata.json").read_text(encoding="utf-8"))

    def setUp(self):
        # Private scratch under the trusted output root, as on the host.
        self.work = Path(tempfile.mkdtemp(prefix="rehearsal-", dir=str(ROOT / "experiments/runtime")))
        os.chmod(self.work, 0o700)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.work, ignore_errors=True)

    def _install_stub_gguf(self):
        fields = {key: _StubField(value) for key, value in self.reference["gguf_metadata"].items()
                  if key != "gguf.version"}
        tensors = [_StubTensor(item["name"], item["shape"], item["type"])
                   for item in self.reference["tensors"]]

        class _StubReader:
            version = 3

            def __init__(self, _path):
                self.fields = fields
                self.tensors = tensors

        module = types.ModuleType("gguf")
        module.GGUFReader = _StubReader
        previous = sys.modules.get("gguf")
        sys.modules["gguf"] = module
        self.addCleanup(lambda: sys.modules.__setitem__("gguf", previous)
                        if previous is not None else sys.modules.pop("gguf", None))

    def test_plan_stage_27_writes_its_receipt(self):
        """`--inspect-tensors`: the stage that failed run -h and -20260912-a.

        Both booked USD 3.27 after the model had been downloaded, converted
        and quantized, and both failed here.
        """

        self._install_stub_gguf()
        out = self.work / "tensor-metadata.json"
        code = self.runner.main([
            "--config", str(ROOT / "model/conversion/j1m-config.json"),
            "--inspect-tensors", str(self.work / "model.gguf"), str(out),
            "--source-receipt", str(ROOT / "artifacts/qwen35-9b/source-model-receipt.json"),
        ])
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["tensor_count"], len(self.reference["tensors"]))
        self.assertEqual(written["chat_template_sha256"], self.reference["chat_template_sha256"])
        self.assertFalse(written["vision_projection_present"])
        # The receipt is larger than the per-string bound; that bound is not
        # the document bound, and conflating them is what broke this stage.
        self.assertGreater(out.stat().st_size, self.runner._RECEIPT_MAX_STRING_CHARS)

    def test_a_vision_tensor_is_still_refused(self):
        """The stage's actual safety property still fails closed."""

        self._install_stub_gguf()
        sys.modules["gguf"].GGUFReader.__init__ = (
            lambda self, _p: (setattr(self, "fields", {k: _StubField(v) for k, v in
                                                       ReceiptRehearsalTests.reference["gguf_metadata"].items()
                                                       if k != "gguf.version"}),
                              setattr(self, "tensors", [_StubTensor("mmproj.weight", [1], "F16")]))[0])
        with self.assertRaises(ValueError):
            self.runner.main([
                "--config", str(ROOT / "model/conversion/j1m-config.json"),
                "--inspect-tensors", str(self.work / "m.gguf"), str(self.work / "t.json"),
                "--source-receipt", str(ROOT / "artifacts/qwen35-9b/source-model-receipt.json"),
            ])
        self.assertFalse((self.work / "t.json").exists())

    def test_run_commands_survives_real_large_multibyte_output(self):
        """What `git clone` and `pip wheel` actually emit, through the real path."""

        emitter = self.work / "emit.py"
        emitter.write_text(
            "import sys\n"
            "for i in range(6000):\n"
            "    sys.stdout.write(f'\\u2501 Receiving objects: {i%100}% ({i}/6000)\\n')\n",
            encoding="utf-8")
        receipts = self.runner.run_commands(
            [[sys.executable, str(emitter)]],
            self.work / "progress.json",
            receipt_path=self.work / "command-receipt.json",
            trusted_root=ROOT,
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "completed", receipts[0])
        self.assertEqual(receipts[0]["exit_code"], 0)
        self.assertTrue((self.work / "command-receipt.json").is_file())

    def test_a_leaked_credential_still_fails_the_stage(self):
        emitter = self.work / "leak.py"
        emitter.write_text(
            "import sys\n"
            "for i in range(3000):\n    sys.stdout.write(f'line {i} 50%\\n')\n"
            "sys.stdout.write('HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz012345\\n')\n"
            "for i in range(3000):\n    sys.stdout.write(f'tail {i}\\n')\n",
            encoding="utf-8")
        receipts = self.runner.run_commands(
            [[sys.executable, str(emitter)]],
            self.work / "progress.json",
            receipt_path=self.work / "command-receipt.json",
            trusted_root=ROOT,
        )
        self.assertEqual(receipts[0]["status"], "failed")
        self.assertEqual(receipts[0]["error_type"], "unsafe_output")
        self.assertNotIn("hf_abcdefghijklmnop", json.dumps(receipts))

    def test_every_tracked_receipt_passes_the_document_validator(self):
        """The publisher's gate and the salvage transport's gate are the same.

        A receipt that cannot pass this cannot be written on the host, and a
        fetched one is refused as `salvage_receipt_content_refused`.
        """

        checked = 0
        for path in sorted((ROOT / "artifacts/qwen35-9b").glob("*.json")):
            self.runner.validate_persisted_document(path.read_bytes())
            checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_write_progress_accepts_a_realistic_stage_payload(self):
        limit = self.runner._COMMAND_LOG_TAIL_LIMIT
        self.runner.write_progress(
            self.work / "progress.json", "stage-27-failed", trusted_root=ROOT,
            argv=[sys.executable, "j1m_runner.py", "--inspect-tensors"],
            status="failed", exit_code=2,
            stderr_tail="━ progress 50%\n" * (limit // 14),
        )
        payload = json.loads((self.work / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["stage"], "stage-27-failed")
