"""Static checks for the opt-in inert Windows compile graph.

This suite never configures CMake, invokes a compiler, or executes Windows
code. Remote Windows SDK compilation remains separate evidence.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
NATIVE_CMAKE = ROOT / "native" / "CMakeLists.txt"
HARNESS = ROOT / "native" / "cmake" / "inert_windows_compile_checks.cmake"
README = ROOT / "native" / "cmake" / "INERT_WINDOWS_COMPILE_CHECKS.md"
MAX_SOURCE_BYTES = 256 * 1024

OPTION = "LAE_ENABLE_INERT_WINDOWS_COMPILE_CHECKS"
META_TARGET = "lae_inert_windows_compile_checks"
TARGET_SOURCES = {
    "lae_compilecheck_action_journal_helper": {
        "action_journal_helper/main.cpp",
        "action_journal_helper/pipe_server.cpp",
        "action_journal_helper/protocol_codec.cpp",
        "action_journal_helper/store_codec.cpp",
        "action_journal_storage/windows_storage.cpp",
    },
    "lae_compilecheck_windows_hardware_attestor": {
        "windows_hardware_attestor/windows_hardware_attestor.cpp",
    },
    "lae_compilecheck_windows_readonly_fs": {
        "windows_readonly_fs/windows_readonly_fs.cpp",
    },
    "lae_compilecheck_windows_release_verifier": {
        "windows_release_verifier/windows_release_verifier.cpp",
    },
}
TARGET_LIBRARIES = {
    "lae_compilecheck_action_journal_helper": {"advapi32", "bcrypt"},
    "lae_compilecheck_windows_hardware_attestor": {
        "advapi32",
        "bcrypt",
        "crypt32",
        "dxgi",
        "ntdll",
        "setupapi",
        "wintrust",
    },
    "lae_compilecheck_windows_readonly_fs": {"advapi32", "ntdll"},
    "lae_compilecheck_windows_release_verifier": {
        "advapi32",
        "bcrypt",
        "ntdll",
    },
}

# Each declared system library must be justified by at least one API imported
# by that target's exact source closure. This prevents STATIC archives from
# making a missing eventual link dependency look like successful evidence.
TARGET_LIBRARY_API_MARKERS = {
    "lae_compilecheck_action_journal_helper": {
        "advapi32": ("OpenProcessToken(", "GetSecurityInfo("),
        "bcrypt": ("BCryptOpenAlgorithmProvider(", "BCryptGenRandom("),
    },
    "lae_compilecheck_windows_hardware_attestor": {
        "advapi32": ("RegGetValueW(", "RegCloseKey("),
        "bcrypt": ("BCryptOpenAlgorithmProvider(",),
        "crypt32": ("CryptQueryObject(", "CertCloseStore("),
        "dxgi": ("CreateDXGIFactory1(",),
        "ntdll": ("RtlGetVersion(",),
        "setupapi": ("SetupDiGetClassDevsW(",),
        "wintrust": ("WinVerifyTrust(",),
    },
    "lae_compilecheck_windows_readonly_fs": {
        "advapi32": ("GetSecurityInfo(", "OpenProcessToken("),
        "ntdll": ("NtOpenFile",),
    },
    "lae_compilecheck_windows_release_verifier": {
        "advapi32": ("GetSecurityInfo(", "OpenProcessToken("),
        "bcrypt": ("BCryptOpenAlgorithmProvider(",),
        "ntdll": ("NtOpenFile", "NtQueryDirectoryFile"),
    },
}


def bounded_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds review bound: {path}")
    return raw.decode("utf-8", errors="strict")


def cmake_block(source: str, command: str, first_argument: str) -> str:
    pattern = re.compile(
        rf"{re.escape(command)}\(\s*{re.escape(first_argument)}"
        rf"(?![A-Za-z0-9_])(.*?)\)",
        re.S,
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"missing {command} block for {first_argument}")
    return match.group(1)


def cmake_function_body(source: str, name: str) -> str:
    match = re.search(
        rf"function\({re.escape(name)}\b[^)]*\)(.*?)endfunction\(\)",
        source,
        re.S,
    )
    if not match:
        raise AssertionError(f"missing function body for {name}")
    return match.group(1)


def modeled_optional_targets(*, enabled: bool, win32: bool, msvc: bool):
    """Reference only: mirrors the three enclosing CMake decisions."""
    if not enabled:
        return set()
    if not win32:
        raise ValueError("windows_sdk_required")
    if not msvc:
        raise ValueError("msvc_compatible_frontend_required")
    return set(TARGET_SOURCES) | {META_TARGET}


class WindowsInertCompileHarnessStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = bounded_text(NATIVE_CMAKE)
        cls.harness = bounded_text(HARNESS)
        cls.readme = bounded_text(README)

    def test_option_is_off_and_include_is_inside_windows_msvc_guards(self):
        option = re.search(
            rf"option\({OPTION}\s+\"[^\"]+\"\s+OFF\)", self.native, re.S
        )
        self.assertIsNotNone(option)
        suffix = self.native[option.end() :]
        outer = re.search(
            rf"if\({OPTION}\)(.*?)\nendif\(\)\s*$", suffix, re.S
        )
        self.assertIsNotNone(outer)
        guarded = outer.group(1)
        self.assertLess(guarded.index("if(NOT WIN32)"), guarded.index("include("))
        self.assertLess(guarded.index("if(NOT MSVC)"), guarded.index("include("))
        self.assertIn("message(FATAL_ERROR", guarded)
        self.assertIn("include(cmake/inert_windows_compile_checks.cmake)", guarded)

    def test_default_model_has_no_optional_targets_or_platform_error(self):
        self.assertEqual(
            modeled_optional_targets(enabled=False, win32=False, msvc=False), set()
        )
        self.assertEqual(
            modeled_optional_targets(enabled=False, win32=True, msvc=True), set()
        )
        with self.assertRaisesRegex(ValueError, "windows_sdk_required"):
            modeled_optional_targets(enabled=True, win32=False, msvc=True)
        with self.assertRaisesRegex(ValueError, "msvc_compatible_frontend_required"):
            modeled_optional_targets(enabled=True, win32=True, msvc=False)
        self.assertEqual(
            modeled_optional_targets(enabled=True, win32=True, msvc=True),
            set(TARGET_SOURCES) | {META_TARGET},
        )

    def test_default_product_targets_have_no_optional_dependency(self):
        prefix = self.native[: self.native.index(f"option({OPTION}")]
        runtime = cmake_block(prefix, "add_library", "lae_runtime")
        engine_link = cmake_block(prefix, "target_link_libraries", "lae-engine")
        for fragment in (
            "windows_readonly_fs",
            "windows_hardware_attestor",
            "action_journal_helper",
            "action_journal_storage",
            "windows_release_verifier",
            "lae_compilecheck_",
        ):
            self.assertNotIn(fragment, runtime)
            self.assertNotIn(fragment, engine_link)
        for target in TARGET_SOURCES:
            self.assertNotIn(target, prefix)

    def test_exact_four_boundary_targets_and_source_closure(self):
        declared = set(
            re.findall(r"add_library\((lae_compilecheck_[a-z_]+)\s+STATIC", self.harness)
        )
        self.assertEqual(declared, set(TARGET_SOURCES))
        for target, expected in TARGET_SOURCES.items():
            block = cmake_block(self.harness, "add_library", target)
            observed = set(re.findall(r'\$\{_LAE_INERT_NATIVE_ROOT\}/([^\"]+\.cpp)', block))
            self.assertEqual(observed, expected, target)
            for relative in expected:
                path = ROOT / "native" / relative
                self.assertTrue(path.is_file(), relative)
                self.assertFalse(path.is_symlink(), relative)
                self.assertLessEqual(path.stat().st_size, MAX_SOURCE_BYTES, relative)

        discovered = {
            path.relative_to(ROOT / "native").as_posix()
            for directory in (
                ROOT / "native/action_journal_helper",
                ROOT / "native/action_journal_storage",
                ROOT / "native/windows_hardware_attestor",
                ROOT / "native/windows_readonly_fs",
                ROOT / "native/windows_release_verifier",
            )
            for path in directory.glob("*.cpp")
        }
        self.assertEqual(discovered, set().union(*TARGET_SOURCES.values()))

    def test_rejected_and_product_sources_are_absent(self):
        for forbidden in (
            "windows_broker/",
            "windows_supervisor/",
            "windows_clipboard/",
            "windows_hardware_probe/",
            "native/main.cpp",
            "model_validation/",
            "backend/",
            "server/",
        ):
            self.assertNotIn(forbidden, self.harness)

    def test_standard_warnings_conformance_and_analysis_are_pinned(self):
        configure = cmake_function_body(
            self.harness, "_lae_configure_inert_compile_check"
        )
        for marker in (
            "CXX_STANDARD 17",
            "CXX_STANDARD_REQUIRED YES",
            "CXX_EXTENSIONS NO",
            "/W4",
            "/WX",
            "/EHsc",
            "/GS",
            "/permissive-",
            "/sdl",
            "/utf-8",
            "/Zc:__cplusplus",
            "/Zc:preprocessor",
            "/analyze",
        ):
            self.assertIn(marker, configure)

    def test_compile_definitions_are_finite_and_cannot_enable_trust(self):
        definitions = cmake_block(
            self.harness, "target_compile_definitions", "${target}"
        )
        observed = set(
            re.findall(r"^\s+([A-Z_][A-Z0-9_]*(?:=1)?)\s*$", definitions, re.M)
        )
        observed.discard("PRIVATE")
        self.assertEqual(
            observed,
            {
                "LAE_INERT_COMPILE_CHECK_ONLY=1",
                "NOMINMAX",
                "UNICODE",
                "WIN32_LEAN_AND_MEAN",
                "_UNICODE",
            },
        )
        for forbidden in (
            "TRUST",
            "AVAILABLE",
            "AUTHENTICATED",
            "SUPERVISOR",
            "ISSUER",
            "ACTIVATION",
            "PRODUCTION",
        ):
            self.assertNotIn(forbidden, definitions)

        anchors = "\n".join(
            bounded_text(path)
            for path in (
                ROOT / "native/windows_readonly_fs/windows_readonly_fs.cpp",
                ROOT / "native/windows_hardware_attestor/trust_anchor.hpp",
                ROOT / "native/action_journal_helper/pipe_server.cpp",
                ROOT / "native/windows_release_verifier/windows_release_verifier.cpp",
            )
        )
        for marker in (
            "kAuthenticatedGrantIssuerAvailable = false",
            "kCancellableNativeIoBoundaryAvailable = false",
            "kConfigured = false",
            "kOfflineAuthenticodePolicyReviewed = false",
            "kSupervisedGlobalDeadlineAvailable = false",
            "kAuthenticatedSupervisorIssuerAvailable = false",
            "kCompiledManifestIdentityTrusted = false",
            "kAuthenticodePolicyTrusted = false",
            "kCancellableNativeIoTrusted = false",
        ):
            self.assertIn(marker, anchors)
        for sources in TARGET_SOURCES.values():
            for relative in sources:
                self.assertNotIn(
                    "LAE_INERT_COMPILE_CHECK_ONLY",
                    bounded_text(ROOT / "native" / relative),
                    relative,
                )

    def test_only_existing_vendored_header_and_system_libraries_are_used(self):
        include = cmake_block(
            self.harness,
            "target_include_directories",
            "lae_compilecheck_action_journal_helper",
        )
        self.assertIn("vendor/llama.cpp/vendor", include)
        self.assertTrue((ROOT / "vendor/llama.cpp/vendor/nlohmann/json.hpp").is_file())
        for target, expected in TARGET_LIBRARIES.items():
            block = cmake_block(self.harness, "target_link_libraries", target)
            observed = set(re.findall(r"^\s+([a-z0-9_]+)\s*$", block, re.M))
            observed.discard("PRIVATE")
            self.assertEqual(observed, expected, target)

    def test_every_system_library_is_justified_by_target_source_imports(self):
        self.assertEqual(TARGET_LIBRARY_API_MARKERS.keys(), TARGET_LIBRARIES.keys())
        for target, libraries in TARGET_LIBRARY_API_MARKERS.items():
            self.assertEqual(libraries.keys(), TARGET_LIBRARIES[target], target)
            source = "\n".join(
                bounded_text(ROOT / "native" / relative)
                for relative in sorted(TARGET_SOURCES[target])
            )
            for library, markers in libraries.items():
                self.assertTrue(markers, f"{target}:{library}")
                self.assertTrue(
                    any(marker in source for marker in markers),
                    f"{target}:{library} lacks a source API justification",
                )

    def test_no_download_install_package_executable_or_product_coupling(self):
        for forbidden in (
            r"\bFetchContent",
            r"\bExternalProject",
            r"\bfile\s*\(\s*DOWNLOAD",
            r"\binstall\s*\(",
            r"\badd_executable\s*\(",
            r"\badd_subdirectory\s*\(",
            r"\bfind_package\s*\(",
            r"\btarget_sources\s*\(\s*lae_runtime",
            r"\btarget_link_libraries\s*\(\s*lae-engine",
        ):
            self.assertNotRegex(self.harness, forbidden)
        for path in (
            ROOT / "release/windows/RELEASE_MANIFEST.json",
            ROOT / "qa/clean-machine/package.py",
            ROOT / "lae-host.mjs",
            ROOT / "host/tools/local/index.mjs",
        ):
            text = bounded_text(path)
            self.assertNotIn("lae_compilecheck_", text)
            self.assertNotIn(META_TARGET, text)

    def test_meta_target_depends_on_all_and_docs_remain_not_ready(self):
        dependencies = cmake_block(self.harness, "add_custom_target", META_TARGET)
        self.assertEqual(
            set(re.findall(r"lae_compilecheck_[a-z_]+", dependencies)),
            set(TARGET_SOURCES),
        )
        for marker in (
            "`OFF` by default",
            "compilation evidence, not activation evidence",
            "does not authorize packaging or execution",
            "Local or target-laptop configuration/build remains prohibited",
        ):
            self.assertIn(marker, self.readme)


if __name__ == "__main__":
    unittest.main()
