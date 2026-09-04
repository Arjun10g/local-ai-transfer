import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import vulkan_source_closure as closure


class VulkanSourceClosureTests(unittest.TestCase):
    def copy_closure(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "llama.cpp"
        shutil.copytree(closure.DEFAULT_ROOT, root)
        self.addCleanup(temp.cleanup)
        return root, root / closure.DEFAULT_MANIFEST.name

    def test_pinned_upstream_closure_is_verified(self):
        result = closure.verify_closure()
        self.assertEqual(result["revision"], closure.PINNED_REVISION)
        self.assertEqual(result["file_count"], 175)
        self.assertTrue(result["verified"])

    def test_modified_shader_is_rejected(self):
        root, manifest = self.copy_closure()
        shader = root / "ggml/src/ggml-vulkan/vulkan-shaders/add.comp"
        shader.write_text(shader.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(closure.VulkanClosureError, "hash mismatch"):
            closure.verify_closure(root, manifest)

    def test_missing_file_is_rejected(self):
        root, manifest = self.copy_closure()
        (root / "ggml/src/ggml-vulkan/ggml-vulkan.cpp").unlink()
        with self.assertRaisesRegex(closure.VulkanClosureError, "file set mismatch"):
            closure.verify_closure(root, manifest)

    def test_unexpected_file_is_rejected(self):
        root, manifest = self.copy_closure()
        (root / "ggml/src/ggml-vulkan/unexpected.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(closure.VulkanClosureError, "file set mismatch"):
            closure.verify_closure(root, manifest)

    def test_manifest_revision_and_path_scope_are_locked(self):
        root, manifest = self.copy_closure()
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["revision"] = "0" * 40
        manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(closure.VulkanClosureError, "pinned official revision"):
            closure.verify_closure(root, manifest)


if __name__ == "__main__":
    unittest.main()
