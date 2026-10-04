"""Splitting the model for a GitHub release and putting it back together.

The real model is 5.6 GB, so these tests run the same code on a small synthetic
file by patching the pinned name, size and hash in both modules. They prove the
receiving side's rules: a good download joins to the pinned bytes, a corrupt
part is refused and deleted, a manifest that does not describe the pinned model
is refused, a rerun is a no-op, and a path-like part name is refused.
"""

import hashlib
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


split = load("split_for_github", ROOT / "scripts" / "model-artifact" / "split_for_github.py")
get = load("get_model", ROOT / "local" / "get_model.py")
PAYLOAD = (bytes(range(256)) * 12000)[:2_600_000]
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def patched():
    stack = [mock.patch.object(module, attr, value) for module in (split, get)
             for attr, value in (("SIZE" if module is get else "EXPECTED_SIZE", len(PAYLOAD)),
                                 ("SHA256" if module is get else "EXPECTED_SHA256", SHA))]
    return stack


class GetModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for p in patched():
            p.start()
            self.addCleanup(p.stop)
        self.model = self.root / split.NAME
        self.model.write_bytes(PAYLOAD)
        self.release = self.root / "release"
        with redirect_stdout(io.StringIO()):
            code = split.main(["--model", str(self.model), "--out", str(self.release), "--part-bytes", "1000000"])
        self.assertEqual(code, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_get(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = get.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def test_split_makes_parts_under_the_limit_that_add_up(self):
        manifest = json.loads((self.release / "MANIFEST.json").read_text())
        self.assertEqual([p["size_bytes"] for p in manifest["parts"]], [1_000_000, 1_000_000, 600_000])
        self.assertEqual(sum(p["size_bytes"] for p in manifest["parts"]), len(PAYLOAD))
        self.assertIn("Apache License 2.0", (self.release / "NOTICE.txt").read_text())
        sums = (self.release / "SHA256SUMS.txt").read_text()
        self.assertIn(f"{SHA}  {split.NAME}", sums)
        for part in manifest["parts"]:
            self.assertEqual(hashlib.sha256((self.release / part["file"]).read_bytes()).hexdigest(), part["sha256"])
        with self.assertRaises(SystemExit):
            split.main(["--model", str(self.model), "--out", str(self.root / "x"), "--part-bytes", str(2 * 1024**3)])

    def test_a_folder_of_good_parts_joins_to_the_pinned_bytes_and_a_rerun_is_a_noop(self):
        out = self.root / "out"
        code, _ = self.run_get("--folder", str(self.release), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual((out / get.NAME).read_bytes(), PAYLOAD)
        self.assertFalse((out / (get.NAME + ".joining")).exists())
        mtime = (out / get.NAME).stat().st_mtime_ns
        code, text = self.run_get("--folder", str(self.release), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("already the pinned model", text)
        self.assertEqual((out / get.NAME).stat().st_mtime_ns, mtime)

    def test_a_corrupt_part_is_refused_and_deleted_and_no_model_appears(self):
        part = self.release / "Qwen3.5-9B-Q4_K_M.gguf.part-002"
        data = bytearray(part.read_bytes())
        data[5] ^= 1
        part.write_bytes(bytes(data))
        out = self.root / "out"
        code, _ = self.run_get("--folder", str(self.release), "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertFalse((out / get.NAME).exists())
        self.assertFalse(part.exists(), "the corrupt part is deleted so a rerun fetches it afresh")

    def test_a_manifest_that_does_not_describe_the_pinned_model_is_refused(self):
        path = self.release / "MANIFEST.json"
        manifest = json.loads(path.read_text())
        for edit in (lambda m: m.update(sha256="0" * 64), lambda m: m.update(size_bytes=1),
                     lambda m: m["parts"][0].update(file="../evil"), lambda m: m["parts"][0].update(size_bytes=5),
                     lambda m: m.update(parts=[])):
            broken = json.loads(json.dumps(manifest))
            edit(broken)
            path.write_text(json.dumps(broken))
            out = self.root / "out"
            code, _ = self.run_get("--folder", str(self.release), "--out", str(out))
            self.assertNotEqual(code, 0)
            self.assertFalse((out / get.NAME).exists())

    def test_a_part_that_hashes_right_but_is_swapped_still_fails_the_pinned_total(self):
        # A consistent but WRONG release (manifest and parts agree with each other, not with the pin).
        other = bytes(reversed(PAYLOAD))
        evil = self.root / "evil"
        evil.mkdir()
        parts = []
        for i, start in enumerate(range(0, len(other), 1_000_000), 1):
            chunk = other[start:start + 1_000_000]
            (evil / f"{get.NAME}.part-{i:03d}").write_bytes(chunk)
            parts.append({"file": f"{get.NAME}.part-{i:03d}", "size_bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
        (evil / "MANIFEST.json").write_text(json.dumps({"file": get.NAME, "size_bytes": len(PAYLOAD), "sha256": SHA, "parts": parts}))
        out = self.root / "out"
        code, _ = self.run_get("--folder", str(evil), "--out", str(out))
        self.assertNotEqual(code, 0)
        self.assertFalse((out / get.NAME).exists())
        self.assertFalse((out / (get.NAME + ".joining")).exists())

    def test_download_over_http_resumes_a_partial_part(self):
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(self.release), **k)  # noqa: E731
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out = self.root / "out"
        parts = out / "model-parts"
        parts.mkdir(parents=True)
        first = self.release / "Qwen3.5-9B-Q4_K_M.gguf.part-001"
        (parts / first.name).write_bytes(first.read_bytes()[:123_456])   # a broken earlier download
        code, _ = self.run_get("--base-url", f"http://127.0.0.1:{server.server_address[1]}", "--out", str(out), "--delete-parts")
        self.assertEqual(code, 0)
        self.assertEqual((out / get.NAME).read_bytes(), PAYLOAD)
        self.assertFalse(parts.exists())


if __name__ == "__main__":
    unittest.main()
