"""Drift guard for docs/DEMO_RUNBOOK.md (offline, standard library only).

The runbook is read aloud and typed from during a live demo, so every command,
path, UI message and pinned value in it must match the repository. Each test
names the source of truth it checks against.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "DEMO_RUNBOOK.md"
WINDOWS = ROOT / "local" / "windows"

# Directories whose paths the runbook may cite; each cited path must exist.
CITED_DIRS = ("local", "docs", "coordination", "host", "ui", "tests", "artifacts")
# Created on the laptop at run time, never in the repo: only checked against
# the scripts that create them.
RUNTIME_DIRS = ("local/bin/", "local/out/")
RUNTIME_PRODUCERS = ("local/windows/Start-BMO.ps1", "local/windows/Test-BMO.ps1", "local/bmo_preflight.py")

# Affirmative capability claims the product cannot back on Windows.
FORBIDDEN_CLAIMS = (
    r"\bcan (?:run|execute) (?:shell |terminal |any |your )?commands\b",
    r"\b(?:has|have|with|gives?) (?:full )?(?:shell|terminal) access\b",
    r"\b(?:shell|terminal) access (?:works|is available|is enabled|is supported)\b",
    r"\bruns? (?:shell )?commands for you\b",
    r"\bcan (?:open|use|drive) (?:a |the )?(?:shell|terminal)\b",
    r"\btested on windows\b(?![^.]*\buntil\b)",
)
CAPABILITY_WORDS = re.compile(r"\bshell\b|\bterminal access\b|\brun commands\b|\bshell commands\b", re.I)
NEGATION = re.compile(r"\b(?:no|not|never|none|cannot|can't|isn't|doesn't|without|nor)\b", re.I)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_code(text: str) -> str:
    """The prose only: fenced blocks and inline code removed."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def command_lines(text: str) -> list[str]:
    """Every fenced line (PowerShell backtick continuations joined) and inline code span."""
    lines: list[str] = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        pending = ""
        for raw in block.splitlines():
            line = raw.rstrip()
            if line.endswith("`"):
                pending += line[:-1] + " "
                continue
            lines.append((pending + line).strip())
            pending = ""
        if pending:
            lines.append(pending.strip())
    lines.extend(re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", " ", text, flags=re.S)))
    return lines


def section(text: str, heading_pattern: str) -> str:
    match = re.search(rf"^## .*{heading_pattern}.*$", text, flags=re.M | re.I)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def ps1_parameters(script: Path) -> dict[str, dict]:
    """Parameters declared in the script's top-level param() block.

    {name: {"set": [...ValidateSet values...] or None, "mandatory": bool}}
    """
    text = read(script)
    start = re.search(r"^param\(", text, flags=re.M)
    if not start:
        raise AssertionError(f"{script.name} has no param() block")
    depth, i = 0, start.end() - 1
    while True:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[start.end(): i]
    params: dict[str, dict] = {}
    pending_set, pending_mandatory = None, False
    for match in re.finditer(r"\[ValidateSet\(([^)]*)\)\]|Mandatory\s*=\s*\$true|\]\s*\$(\w+)\b", block):
        if match.group(1) is not None:
            pending_set = re.findall(r"'([^']*)'", match.group(1))
        elif match.group(2) is not None:
            params[match.group(2)] = {"set": pending_set, "mandatory": pending_mandatory}
            pending_set, pending_mandatory = None, False
        else:
            pending_mandatory = True
    return params


def ps1_invocations(text: str) -> list[tuple[str, list[str]]]:
    found = []
    for line in command_lines(text):
        for match in re.finditer(r"\.\\local\\windows\\([\w-]+\.ps1)((?:\s+[^\s#|;]+)*)", line):
            found.append((match.group(1), match.group(2).split()))
    return found


class DemoRunbookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(RUNBOOK)
        cls.prose = strip_code(cls.text)

    # -- commands -------------------------------------------------------------

    def test_powershell_invocations_use_real_scripts_and_parameters(self):
        invocations = ps1_invocations(self.text)
        self.assertGreaterEqual(len(invocations), 5, "the runbook should carry its commands")
        for script, tokens in invocations:
            path = WINDOWS / script
            self.assertTrue(path.is_file(), f"{script} does not exist in local/windows/")
            params = ps1_parameters(path)
            given = set()
            for index, token in enumerate(tokens):
                if not re.match(r"^-[A-Za-z]", token):
                    continue
                name = token[1:]
                self.assertTrue(name in params, f"{script} has no parameter -{name} (declared: {sorted(params)})")
                given.add(name)
                allowed = params[name]["set"]
                if allowed is not None:
                    self.assertLess(index + 1, len(tokens), f"{script} -{name} needs a value")
                    self.assertIn(tokens[index + 1], allowed, f"{script} -{name} {tokens[index + 1]} not in ValidateSet {allowed}")
            for name, spec in params.items():
                if spec["mandatory"]:
                    self.assertIn(name, given, f"{script} invocation omits mandatory -{name}: {tokens}")

    def test_every_mode_mentioned_is_a_real_mode(self):
        modes = set()
        for script in WINDOWS.glob("*.ps1"):
            mode = ps1_parameters(script).get("Mode")
            if mode and mode["set"]:
                modes.update(mode["set"])
        mentions = re.findall(r"-Mode\s+([A-Za-z][\w-]*)", self.text)
        self.assertTrue(mentions)
        for value in mentions:
            self.assertIn(value, modes, f"-Mode {value} is not a mode of any local/windows script")

    def test_python_launcher_flags_exist(self):
        for line in command_lines(self.text):
            for match in re.finditer(r"local[\\/](bmo_\w+\.py)((?:\s+[^\s#|;]+)*)", line):
                script = ROOT / "local" / match.group(1)
                self.assertTrue(script.is_file(), match.group(1))
                source = read(script) + read(ROOT / "local" / "bmo_chat.py")  # shared engine flags
                for flag in re.findall(r"(?<!\S)(--[a-z][\w-]*)", match.group(2)):
                    self.assertRegex(source, rf"add_argument\(\s*\"{re.escape(flag)}\"", f"{match.group(1)} has no {flag}")

    # -- paths and pinned values ---------------------------------------------------

    def test_cited_repository_paths_exist(self):
        pattern = rf"(?<![\w.:\\/-])((?:{'|'.join(CITED_DIRS)})[\\/][\w.<>*\\/-]*)"
        paths = {p.replace("\\", "/").rstrip(".,;:)") for p in re.findall(pattern, self.text)}
        self.assertTrue(paths)
        producers = "".join(read(ROOT / p) for p in RUNTIME_PRODUCERS).replace("\\", "/")
        for path in sorted(paths):
            runtime = next((d for d in RUNTIME_DIRS if path.startswith(d)), None)
            if runtime:
                self.assertIn(runtime.rstrip("/"), producers, f"no script creates {runtime}")
                name = path[len(runtime):]
                if name:
                    stem = re.split(r"[-.<]", name, maxsplit=1)[0]
                    self.assertRegex(producers, rf"[\"'/]{re.escape(stem)}[-.]", f"no script produces {path}")
                continue
            self.assertNotRegex(path, r"[<*]", f"pattern path outside a runtime dir: {path}")
            self.assertTrue((ROOT / path).exists(), f"cited path does not exist: {path}")

    def test_pinned_model_identity_matches_the_launcher_and_manifest(self):
        source = read(ROOT / "local" / "bmo_local.py")
        sha = re.search(r'^MODEL_SHA256 = "([0-9a-f]{64})"', source, flags=re.M).group(1)
        size = int(re.search(r"^MODEL_SIZE = (\d+)", source, flags=re.M).group(1))
        self.assertTrue(sha in self.text, "the runbook no longer pins the launcher's model SHA-256")
        self.assertTrue(f"{size:,}" in self.text, "the runbook no longer pins the launcher's model size")
        for other in re.findall(r"\b[0-9a-fA-F]{64}\b", self.text):
            self.assertEqual(other.lower(), sha, "an unexpected 64-hex hash is pinned in the runbook")
        for number in re.findall(r"\b\d{1,3}(?:,\d{3}){3}\b", self.text):
            self.assertEqual(number, f"{size:,}", f"a model size other than the pinned one: {number}")
        manifest = ROOT / "artifacts" / "qwen35-9b" / "model-manifest.json"
        if manifest.is_file():
            blob = json.dumps(json.loads(read(manifest)))
            self.assertTrue(sha in blob and str(size) in blob, "model-manifest.json disagrees with the pinned model")

    # -- UI wording ------------------------------------------------------------

    def test_quoted_ui_messages_are_verbatim_in_errors_js(self):
        errors = read(ROOT / "ui" / "errors.js").replace("\\'", "'")
        quoted = re.findall(r'"([^"\n]+)"', self.prose)
        playbook = re.findall(r'"([^"\n]+)"', strip_code(section(self.text, "Failure playbook")))
        self.assertGreaterEqual(len(playbook), 8, "the failure playbook should quote the UI's messages")
        for message in quoted:
            self.assertTrue(message in errors, f"not a message the UI shows (ui/errors.js): {message!r}")

    def test_scripted_flow_uses_only_tools_offered_on_windows(self):
        index = read(ROOT / "host" / "tools" / "local" / "index.mjs")
        for always in ("time.now", "system.get_info"):
            self.assertRegex(index, rf"'{re.escape(always)}': Object\.freeze\(\{{ advertised: true, reason: 'always_available'")
        self.assertIn("'process.run_allowlisted': Object.freeze({ advertised: processConfigured, reason: effectivePlatform === 'win32' ? 'unsafe_subprocess_boundary'", index)
        flow = section(self.text, "Scripted flow")
        self.assertTrue(flow)
        named = set(re.findall(r"\b((?:time|system|fs|clipboard|app|browser|process)\.[a-z_]+)\b", flow))
        offered = {"time.now", "system.get_info", "fs.list", "fs.read_text", "fs.search_text"}
        self.assertTrue(named, "the scripted flow should name the tools it shows")
        self.assertLessEqual(named, offered, f"tools not offered on Windows: {sorted(named - offered)}")

    # -- honesty ---------------------------------------------------------------

    def test_has_a_cannot_yet_section(self):
        body = section(self.text, r"cannot.*yet")
        self.assertTrue(body.strip(), "missing the 'What it cannot do yet' section")
        # No shell on Windows, not yet proven on Windows (L0), no measured speed.
        for must in ("shell", "L0", "speed"):
            self.assertIn(must.lower(), body.lower(), f"the 'cannot yet' section no longer mentions {must}")

    def test_no_sentence_claims_shell_or_terminal_access(self):
        lowered = self.prose.lower()
        for pattern in FORBIDDEN_CLAIMS:
            self.assertIsNone(re.search(pattern, lowered), f"forbidden claim matched: {pattern}")
        sentences = re.split(r"(?<=[.!?;])\s+|\n\s*\n|\n\s*[-*|\d]", self.prose)
        for sentence in sentences:
            if CAPABILITY_WORDS.search(sentence):
                self.assertRegex(sentence, NEGATION, f"capability word outside a negative sentence: {sentence.strip()!r}")

    def test_evaluation_score_matches_the_gate_and_is_caveated(self):
        gates = read(ROOT / "coordination" / "RELEASE_GATES.md")
        l1 = next(line for line in gates.splitlines() if line.startswith("| L1"))
        for score in set(re.findall(r"\b\d{2}/37\b", self.text)):
            self.assertIn(score, l1, f"{score} is not recorded in the RELEASE_GATES L1 row")
        self.assertIn("36/37", self.text)
        self.assertRegex(self.text, r"not a controlled result")
        self.assertIn("A100", self.text)

    def test_speed_is_never_claimed_as_measured_on_the_laptop(self):
        # A tokens-per-second or seconds figure in prose must sit in a sentence
        # that says where it came from (estimate, Mac, or unmeasured).
        for sentence in re.split(r"(?<=[.!?])\s+|\n\s*\n|\n\|", self.prose):
            if re.search(r"\b\d+(?:-\d+)?\s*tokens/s\b|\b\d{2,}\s*s to first token\b", sentence):
                self.assertRegex(sentence, r"(?i)estimate|unmeasured|mac|not measured", sentence.strip())


if __name__ == "__main__":
    unittest.main()
