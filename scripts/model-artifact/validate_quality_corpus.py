#!/usr/bin/env python3
"""Validate the Qwen3.5-9B quality-evaluation corpus without model weights.

Standard library only, matching ``scripts/model-artifact/validate_specs.py``: the
corpus must be checkable in the controlled build environment and in every author
worktree before any optional dependency exists.

The validator has two layers:

1. A bounded JSON Schema (draft 2020-12) subset interpreter driven by
   ``model/quality-eval/schema/quality-case.schema.json``. The schema file is the
   single structural source of truth; this module implements only the keywords
   that schema uses.
2. Semantic checks JSON Schema cannot express: deterministic split derivation,
   id/category/filename agreement, production tool-catalogue membership and
   argument-name agreement, per-category minimums from the fixture spec, split and
   difficulty proportions, deterministic settings, adversarial negative
   assertions, and forbidden content patterns.

Full prompts and expectations are never printed; diagnostics carry only the case
id, the JSON pointer, and a bounded reason.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
SPEC_RELATIVE = "model/quality-eval/quality-fixture-spec.json"
SCHEMA_RELATIVE = "model/quality-eval/schema/quality-case.schema.json"
CASES_RELATIVE = "model/quality-eval/cases"
CATALOGUE_RELATIVE = "tests/model/production_tool_call_eval.json"

# --------------------------------------------------------------------------
# Corpus policy constants
# --------------------------------------------------------------------------

SPLIT_TARGET = {"train": 20.0, "dev": 20.0, "test": 60.0}
SPLIT_TOLERANCE_POINTS = 5.0
SPLIT_CHECK_MIN_CASES = 40

DIFFICULTY_TARGET = {"easy": 25.0, "medium": 30.0, "hard": 25.0, "adversarial": 20.0}
DIFFICULTY_TOLERANCE_POINTS = 10.0
DIFFICULTY_CHECK_MIN_CASES = 20

AUTHORING_MARGIN_NUMERATOR = 11  # spec minimum plus a 10% authoring margin
AUTHORING_MARGIN_DENOMINATOR = 10
MAX_CASE_BYTES = 64 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024

# The three fixture cases already referenced by
# scripts/model-artifact/build_manifest.py and artifacts/qwen35-9b/model-manifest.json
# keep their historical ids. They are exempt from the category-slug prefix rule and
# from nothing else.
LEGACY_CASE_IDS = {
    "thinking-off-001": "thinking_control",
    "tool-xml-001": "tool_selection_no_tool",
    "reset-long-short-001": "long_short_integrity",
}

# Categories permitted to run the deep (thinking-enabled) generation profile.
DEEP_PROFILE_CATEGORIES = frozenset(
    {"reasoning", "file_task_planning", "tool_recovery", "thinking_control"}
)
# Categories that must exercise the tool-selection profile.
TOOL_PROFILE_CATEGORIES = frozenset(
    {"tool_selection_no_tool", "tool_arguments", "tool_recovery"}
)

# An adversarial case must assert at least one negative outcome.
NEGATIVE_ASSERTION_KEYS = frozenset(
    {
        "must_not_contain",
        "must_not_leak",
        "must_not_call",
        "forbid_names",
        "forbidden_tools",
        "forbidden_facts",
        "violation_markers",
        "no_call",
    }
)

# --------------------------------------------------------------------------
# Content policy
# --------------------------------------------------------------------------

ALLOWED_URL_HOSTS = frozenset(
    {
        "example.com",
        "www.example.com",
        "example.org",
        "www.example.org",
        "example.net",
        "docs.example.com",
        "intranet.example.com",
        "status.example.com",
        "localhost",
        "127.0.0.1",
    }
)
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "example.invalid"}
)

URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[ .\-]?)?(?:\(\d{3}\)|\d{3})[ .\-]\d{3}[ .\-]\d{4}(?!\d)"
)
ALLOWED_PHONE_RE = re.compile(r"(?:\(555\)|555)[ .\-]01\d{2}")

SECRET_PATTERNS = (
    ("hugging-face token", re.compile(r"hf_[A-Za-z0-9]{16,}")),
    ("openai-style secret key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token literal", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("aws secret literal", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S{8,}")),
)

REAL_PATH_PATTERNS = (
    ("windows user profile", re.compile(r"(?i)[A-Z]:\\Users\\")),
    ("windows system root", re.compile(r"(?i)[A-Z]:\\Windows\\")),
    ("windows program files", re.compile(r"(?i)[A-Z]:\\Program Files")),
    ("posix user home", re.compile(r"(?<![A-Za-z0-9_.])/(?:Users|home)/")),
    ("posix system path", re.compile(r"(?<![A-Za-z0-9_.])/(?:etc|var|root|proc)/")),
    ("environment home reference", re.compile(r"%USERPROFILE%|\$HOME|~/")),
)

# Absolute paths may only name the fictional workspace roots. Tool arguments should
# use workspace_id plus a relative path instead of an absolute path at all.
FICTIONAL_ROOT_PATTERN = re.compile(r"(?i)\A(?:W:\\demo-workspace|/workspaces/demo)")
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"']*")
POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.~/])/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-][^\s\"']*")

NON_ENGLISH_RE = re.compile(
    "["
    "\u0370-\u03ff"  # Greek
    "\u0400-\u04ff"  # Cyrillic
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u0900-\u097f"  # Devanagari
    "\u3040-\u30ff"  # Kana
    "\u3400-\u4dbf\u4e00-\u9fff"  # CJK
    "\uac00-\ud7af"  # Hangul
    "]"
)

# --------------------------------------------------------------------------
# Deterministic proposition matcher
# --------------------------------------------------------------------------
#
# `summarization` key/forbidden facts and `instruction` rubric items are natural
# language, so they need a pinned matcher or the score depends on a judge. The
# fixture spec calls for "compact deterministic contract fixtures", so there is no
# LLM or human rater anywhere in this path: every item carries a `match` object and
# the functions below are the whole of its semantics.

NORMALIZE_STEPS = ("lowercase", "collapse_ws", "strip_punct", "numerals")

# Spelled-out forms folded to digits so a matcher can be written with digits only.
NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
    "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str, steps: list[str] | tuple[str, ...]) -> str:
    """Apply the requested steps in the fixed canonical NORMALIZE_STEPS order."""
    selected = [step for step in NORMALIZE_STEPS if step in steps]
    result = text
    if "lowercase" in selected:
        result = result.lower()
    if "collapse_ws" in selected:
        result = _WS_RE.sub(" ", result).strip()
    if "strip_punct" in selected:
        result = _PUNCT_RE.sub(" ", result)
    if "numerals" in selected:
        result = _NUMBER_WORD_RE.sub(lambda m: NUMBER_WORDS[m.group(1).lower()], result)
    # strip_punct and numerals can reintroduce runs, so collapse once more.
    if "collapse_ws" in selected:
        result = _WS_RE.sub(" ", result).strip()
    return result


def match_variant(answer: str, variant: Any, steps: list[str] | tuple[str, ...]) -> bool:
    """One any_of variant against an already-normalized answer.

    A plain string matches as a substring and is normalized the same way as the
    answer. A {"regex": ...} variant is matched with re.search and is NOT
    normalized, so the pattern must already be written in normalized form; anchor
    with \\A / \\Z for a whole-answer predicate.
    """
    if isinstance(variant, dict):
        return re.search(variant["regex"], answer, re.DOTALL) is not None
    return normalize_text(str(variant), steps) in answer


def match_item(answer: str, match: dict) -> bool:
    """True when ANY variant matches. Callers invert it for a forbidden item."""
    steps = match.get("normalize", [])
    normalized = normalize_text(answer, steps)
    return any(match_variant(normalized, variant, steps) for variant in match.get("any_of", []))


def check_match_object(match: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(match, dict):
        return [f"{path}: match must be an object"]
    variants = match.get("any_of")
    if not isinstance(variants, list) or not variants:
        return [f"{path}.any_of: at least one variant is required"]
    steps = match.get("normalize")
    if not isinstance(steps, list):
        return [f"{path}.normalize: an array is required (use [] for a raw-text matcher)"]
    for index, variant in enumerate(variants):
        if isinstance(variant, dict):
            pattern = variant.get("regex")
            if not isinstance(pattern, str):
                errors.append(f"{path}.any_of[{index}].regex: a string is required")
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{path}.any_of[{index}].regex: does not compile ({exc.msg})")
        elif not isinstance(variant, str) or not variant.strip():
            errors.append(f"{path}.any_of[{index}]: a non-empty string or a regex object is required")
    # A normalized matcher is paraphrase-sensitive, so it needs alternatives. A raw
    # structural matcher (normalize: []) is an exact predicate and one is correct.
    if steps and len(variants) < 2:
        errors.append(
            f"{path}.any_of: a normalized matcher needs at least two variants "
            "(synonym, numeral/word form, singular/plural); use normalize [] for an exact "
            "structural regex"
        )
    return errors


# --------------------------------------------------------------------------
# Bounded JSON Schema (draft 2020-12) subset interpreter
# --------------------------------------------------------------------------

SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "title",
        "description",
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "oneOf",
        "anyOf",
        "allOf",
        "if",
        "then",
        "else",
    }
)


class SchemaError(ValueError):
    """The schema file itself uses a keyword this interpreter does not implement."""


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_matches(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return _is_integer(value)
    if name == "number":
        return _is_number(value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise SchemaError(f"unsupported type name {name!r}")


def json_equal(left: Any, right: Any) -> bool:
    """JSON equality: ``true`` is never ``1`` and ``false`` is never ``0``."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if _is_number(left) and _is_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    return left == right


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise SchemaError(f"unsupported $ref {ref!r}")
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            raise SchemaError(f"unresolvable $ref {ref!r}")
        node = node[token]
    if not isinstance(node, dict):
        raise SchemaError(f"$ref {ref!r} does not name a schema object")
    return node


def validate_instance(instance: Any, schema: dict, root: dict, path: str = "") -> list[str]:
    """Return schema violations for ``instance``; an empty list means valid."""
    errors: list[str] = []
    unsupported = set(schema) - SUPPORTED_KEYWORDS
    if unsupported:
        raise SchemaError(f"{path or '/'}: unsupported schema keywords {sorted(unsupported)}")

    if "$ref" in schema:
        errors.extend(validate_instance(instance, _resolve(schema["$ref"], root), root, path))

    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else names
        if not any(_type_matches(instance, name) for name in names):
            return errors + [f"{path or '/'}: expected type {'|'.join(names)}"]

    if "const" in schema and not json_equal(instance, schema["const"]):
        errors.append(f"{path or '/'}: value must equal {json.dumps(schema['const'])}")

    if "enum" in schema and not any(json_equal(instance, option) for option in schema["enum"]):
        errors.append(f"{path or '/'}: value not in enum {json.dumps(schema['enum'])}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: does not match pattern {schema['pattern']}")

    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: not below exclusiveMaximum {schema['exclusiveMaximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(instance):
                if any(json_equal(item, other) for other in instance[:index]):
                    errors.append(f"{path}[{index}]: duplicate array item")
                    break
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(
                    validate_instance(item, schema["items"], root, f"{path}[{index}]")
                )

    if isinstance(instance, dict):
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append(f"{path or '/'}: fewer than minProperties {schema['minProperties']}")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{path or '/'}: more than maxProperties {schema['maxProperties']}")
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path or '/'}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors.extend(
                    validate_instance(value, properties[name], root, f"{path}.{name}")
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path or '/'}: unknown property {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(
                    validate_instance(
                        value, schema["additionalProperties"], root, f"{path}.{name}"
                    )
                )
            if "propertyNames" in schema:
                errors.extend(
                    validate_instance(name, schema["propertyNames"], root, f"{path}.{name}")
                )

    for sub in schema.get("allOf", []):
        errors.extend(validate_instance(instance, sub, root, path))

    if "anyOf" in schema:
        if not any(
            not validate_instance(instance, sub, root, path) for sub in schema["anyOf"]
        ):
            errors.append(f"{path or '/'}: satisfies no anyOf branch")

    if "oneOf" in schema:
        matched = sum(
            1 for sub in schema["oneOf"] if not validate_instance(instance, sub, root, path)
        )
        if matched != 1:
            errors.append(f"{path or '/'}: matched {matched} oneOf branches, expected exactly 1")

    if "if" in schema:
        condition_holds = not validate_instance(instance, schema["if"], root, path)
        branch = "then" if condition_holds else "else"
        if branch in schema:
            errors.extend(validate_instance(instance, schema[branch], root, path))

    return errors


# --------------------------------------------------------------------------
# Deterministic split derivation
# --------------------------------------------------------------------------


def derive_split(case_id: str) -> str:
    """train 20% / dev 20% / test 60%, derived only from the case id."""
    bucket = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest(), 16) % 100
    if bucket < 20:
        return "train"
    if bucket < 40:
        return "dev"
    return "test"


def category_slug(category: str) -> str:
    return category.replace("_", "-")


def authoring_target(minimum: int) -> int:
    """Spec minimum plus a 10% authoring margin, rounded up with exact integer math."""
    return -(-minimum * AUTHORING_MARGIN_NUMERATOR // AUTHORING_MARGIN_DENOMINATOR)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_json(path: Path, max_bytes: int = MAX_FILE_BYTES) -> Any:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path.name}: file exceeds {max_bytes} bytes")
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def tool_catalogue(root: Path) -> dict[str, dict[str, Any]]:
    """Exact production tool names and argument schemas (33 tools)."""
    fixture = load_json(root / CATALOGUE_RELATIVE)
    catalogue: dict[str, dict[str, Any]] = {}
    for tool in fixture.get("tools", []):
        function = tool.get("function", {})
        name = function.get("name")
        parameters = function.get("parameters", {})
        if isinstance(name, str):
            catalogue[name] = {
                "properties": set(parameters.get("properties", {})),
                "required": set(parameters.get("required", [])),
            }
    return catalogue


# --------------------------------------------------------------------------
# Content scanning
# --------------------------------------------------------------------------


def iter_strings(node: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((path, node))
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(iter_strings(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(iter_strings(value, f"{path}[{index}]"))
    return found


def scan_content(case: dict, case_path: str) -> list[str]:
    errors: list[str] = []
    language = case.get("lang", "en")
    for path, text in iter_strings(case, case_path):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"{path}: forbidden credential pattern ({label})")
        for label, pattern in REAL_PATH_PATTERNS:
            if pattern.search(text):
                errors.append(f"{path}: forbidden real-system path pattern ({label})")
        for match in URL_RE.finditer(text):
            host = match.group(1).lower().rstrip(".")
            if host not in ALLOWED_URL_HOSTS:
                errors.append(f"{path}: URL host {host!r} is not in the fictional allowlist")
        for match in EMAIL_RE.finditer(text):
            domain = match.group(1).lower().rstrip(".")
            if domain not in ALLOWED_EMAIL_DOMAINS:
                errors.append(f"{path}: email domain {domain!r} is not a reserved example domain")
        for match in PHONE_RE.finditer(text):
            if ALLOWED_PHONE_RE.search(match.group(0)) is None:
                errors.append(f"{path}: telephone-shaped literal outside the reserved 555-01xx range")
        for regex in (WINDOWS_PATH_RE, POSIX_PATH_RE):
            for match in regex.finditer(text):
                if FICTIONAL_ROOT_PATTERN.match(match.group(0)) is None:
                    errors.append(
                        f"{path}: absolute path is not under a fictional workspace root "
                        "(W:\\demo-workspace or /workspaces/demo); prefer workspace_id plus a relative path"
                    )
                    break
        if language == "en" and NON_ENGLISH_RE.search(text):
            errors.append(f"{path}: non-Latin script requires an explicit non-'en' case 'lang'")
    return errors


# --------------------------------------------------------------------------
# Semantic checks
# --------------------------------------------------------------------------


def collect_tool_references(expected: dict) -> list[tuple[str, str]]:
    """(json-pointer-ish path, tool name) pairs referenced from an expected block."""
    references: list[tuple[str, str]] = []
    call = expected.get("call")
    if isinstance(call, dict) and isinstance(call.get("name"), str):
        references.append((".expected.call.name", call["name"]))
    for index, step in enumerate(expected.get("plan", []) or []):
        if isinstance(step, dict) and isinstance(step.get("tool"), str):
            references.append((f".expected.plan[{index}].tool", step["tool"]))
    for index, turn in enumerate(expected.get("tool_transcript", []) or []):
        if isinstance(turn, dict) and isinstance(turn.get("call"), dict):
            name = turn["call"].get("name")
            if isinstance(name, str):
                references.append((f".expected.tool_transcript[{index}].call.name", name))
    for key in ("forbid_names", "must_not_call", "forbidden_tools"):
        for index, name in enumerate(expected.get(key, []) or []):
            if isinstance(name, str):
                references.append((f".expected.{key}[{index}]", name))
    return references


def collect_call_arguments(expected: dict) -> list[tuple[str, str, dict]]:
    """(path, tool name, arguments object) triples that must match a tool schema."""
    calls: list[tuple[str, str, dict]] = []
    call = expected.get("call")
    if isinstance(call, dict) and isinstance(call.get("name"), str) and isinstance(call.get("arguments"), dict):
        calls.append((".expected.call", call["name"], call["arguments"]))
    for index, step in enumerate(expected.get("plan", []) or []):
        if isinstance(step, dict) and isinstance(step.get("tool"), str) and isinstance(step.get("arguments_subset"), dict):
            calls.append((f".expected.plan[{index}]", step["tool"], step["arguments_subset"]))
    for index, turn in enumerate(expected.get("tool_transcript", []) or []):
        if isinstance(turn, dict) and isinstance(turn.get("call"), dict):
            inner = turn["call"]
            if isinstance(inner.get("name"), str) and isinstance(inner.get("arguments"), dict):
                calls.append((f".expected.tool_transcript[{index}].call", inner["name"], inner["arguments"]))
    return calls


def has_negative_assertion(node: Any) -> bool:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in NEGATIVE_ASSERTION_KEYS and value not in (None, [], "", False):
                return True
            if has_negative_assertion(value):
                return True
    elif isinstance(node, list):
        return any(has_negative_assertion(item) for item in node)
    return False


def check_case(
    case: dict,
    *,
    category: str,
    metric: str,
    catalogue: dict[str, dict[str, Any]],
    schema: dict,
    seen_ids: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    case_id = case.get("id")
    label = case_id if isinstance(case_id, str) else "<missing id>"
    case_path = f"[{label}]"

    errors.extend(validate_instance(case, _resolve("#/$defs/case", schema), schema, case_path))

    if not isinstance(case_id, str):
        return errors

    if case_id in seen_ids:
        errors.append(f"{case_path}: duplicate case id, already defined in {seen_ids[case_id]}")
    else:
        seen_ids[case_id] = category

    if case.get("category") != category:
        errors.append(f"{case_path}.category: {case.get('category')!r} does not match the file category {category!r}")

    legacy_category = LEGACY_CASE_IDS.get(case_id)
    if legacy_category is not None:
        if legacy_category != category:
            errors.append(f"{case_path}: reserved fixture id belongs to category {legacy_category!r}")
    else:
        slug = category_slug(category)
        if not case_id.startswith(f"{slug}-"):
            errors.append(f"{case_path}: id must start with the category slug {slug!r}-")
        elif not re.fullmatch(r"[0-9]{3}", case_id[len(slug) + 1 :]):
            errors.append(f"{case_path}: id must end with a zero-padded three-digit ordinal")

    expected_split = derive_split(case_id)
    if case.get("split") != expected_split:
        errors.append(
            f"{case_path}.split: must be {expected_split!r} (sha256 bucket of the id), found {case.get('split')!r}"
        )

    settings = case.get("settings")
    if isinstance(settings, dict):
        if settings.get("profile") == "deep" and category not in DEEP_PROFILE_CATEGORIES:
            errors.append(f"{case_path}.settings.profile: category {category!r} may not use the deep profile")
        if category in TOOL_PROFILE_CATEGORIES and settings.get("profile") not in {"tool_selection", "deep"}:
            errors.append(f"{case_path}.settings.profile: category {category!r} requires the tool_selection or deep profile")
        if settings.get("enable_thinking") is not (settings.get("mode") == "deep"):
            errors.append(f"{case_path}.settings: enable_thinking must be derived as (mode == 'deep')")
        long_input = case.get("long_input")
        if isinstance(long_input, dict) and _is_integer(long_input.get("target_tokens")):
            context = settings.get("context_tokens", 8192)
            budget = context - settings.get("max_output_tokens", 0)
            if long_input["target_tokens"] > budget:
                errors.append(
                    f"{case_path}.long_input.target_tokens: exceeds context_tokens minus max_output_tokens ({budget})"
                )

    expected = case.get("expected")
    if isinstance(expected, dict):
        if expected.get("metric") != metric:
            errors.append(
                f"{case_path}.expected.metric: category {category!r} requires {metric!r}, found {expected.get('metric')!r}"
            )
        declared = set(case.get("tools") or [])
        for path, name in collect_tool_references(expected):
            if name not in catalogue:
                errors.append(f"{case_path}{path}: {name!r} is not a production tool name")
            elif path.endswith("call.name") and name not in declared:
                errors.append(f"{case_path}{path}: {name!r} is not listed in the case 'tools' array")
        for path, name, arguments in collect_call_arguments(expected):
            schema_entry = catalogue.get(name)
            if schema_entry is None:
                continue
            unknown = sorted(set(arguments) - schema_entry["properties"])
            if unknown:
                errors.append(f"{case_path}{path}.arguments: {name!r} has no argument(s) {unknown}")
            if path.endswith(".call"):
                missing = sorted(schema_entry["required"] - set(arguments))
                if missing:
                    errors.append(f"{case_path}{path}.arguments: {name!r} requires {missing}")
        if isinstance(expected.get("required_fields"), list):
            call = expected.get("call")
            if isinstance(call, dict) and isinstance(call.get("arguments"), dict):
                absent = sorted(set(expected["required_fields"]) - set(call["arguments"]))
                if absent:
                    errors.append(f"{case_path}.expected.required_fields: {absent} absent from expected call arguments")
        for index, pattern in enumerate(expected.get("must_match", []) or []):
            try:
                re.compile(pattern)
            except re.error:
                errors.append(f"{case_path}.expected.must_match[{index}]: not a compilable regular expression")
        # A rubric item is natural language, so it carries its own decision procedure.
        # The schema requires `match`; this check repeats it so the validator still
        # fails closed if the schema is ever loosened, and then checks the contents.
        for index, item in enumerate(expected.get("rubric", []) or []):
            if not isinstance(item, dict):
                continue
            if "match" not in item:
                errors.append(
                    f"{case_path}.expected.rubric[{index}]: a rubric item requires a 'match' "
                    "object; a requirement no substring or regex can decide is not scoreable"
                )
                continue
            errors.extend(check_match_object(item["match"], f"{case_path}.expected.rubric[{index}].match"))
        for key in ("key_facts", "forbidden_facts"):
            for index, item in enumerate(expected.get(key, []) or []):
                if isinstance(item, dict) and "match" in item:
                    errors.extend(check_match_object(item["match"], f"{case_path}.expected.{key}[{index}].match"))
        for index, marker in enumerate(expected.get("violation_markers", []) or []):
            try:
                re.compile(marker)
            except re.error:
                errors.append(f"{case_path}.expected.violation_markers[{index}]: not a compilable regular expression")

    for name in case.get("tools") or []:
        if name not in catalogue:
            errors.append(f"{case_path}.tools: {name!r} is not a production tool name")

    if case.get("difficulty") == "adversarial" and not has_negative_assertion(case.get("expected")):
        errors.append(
            f"{case_path}: adversarial cases must carry at least one negative assertion "
            f"({sorted(NEGATIVE_ASSERTION_KEYS)})"
        )

    encoded = len(json.dumps(case, separators=(",", ":")).encode("utf-8"))
    if encoded > MAX_CASE_BYTES:
        errors.append(f"{case_path}: serialized case is {encoded} bytes, above the {MAX_CASE_BYTES}-byte bound")

    errors.extend(scan_content(case, case_path))
    return errors


def check_proportions(
    label: str,
    counts: dict[str, int],
    targets: dict[str, float],
    tolerance: float,
    total: int,
    category: str,
) -> list[str]:
    errors: list[str] = []
    for key, target in targets.items():
        share = 100.0 * counts.get(key, 0) / total
        if abs(share - target) > tolerance:
            errors.append(
                f"[{category}] {label} proportion {key}={share:.1f}% is more than "
                f"{tolerance:.0f} points from the {target:.0f}% target"
            )
    return errors


# --------------------------------------------------------------------------
# Corpus validation
# --------------------------------------------------------------------------


def validate_corpus(root: Path, require_complete: bool = False) -> tuple[list[str], list[dict]]:
    errors: list[str] = []
    rows: list[dict] = []

    spec = load_json(root / SPEC_RELATIVE)
    schema = load_json(root / SCHEMA_RELATIVE)
    catalogue = tool_catalogue(root)
    if len(catalogue) != 33:
        errors.append(f"{CATALOGUE_RELATIVE}: expected the 33-tool production catalogue, found {len(catalogue)}")

    categories = {entry["id"]: entry for entry in spec.get("categories", [])}
    cases_dir = root / CASES_RELATIVE
    if not cases_dir.is_dir():
        return [f"missing corpus directory: {CASES_RELATIVE}"], rows

    present = {path.stem: path for path in sorted(cases_dir.glob("*.json"))}
    unexpected = sorted(set(present) - set(categories))
    for stem in unexpected:
        errors.append(f"{CASES_RELATIVE}/{stem}.json: no such category in {SPEC_RELATIVE}")
    stray = sorted(p.name for p in cases_dir.iterdir() if p.is_file() and p.suffix != ".json")
    for name in stray:
        errors.append(f"{CASES_RELATIVE}/{name}: only <category>.json files belong in the corpus directory")

    seen_ids: dict[str, str] = {}
    for category, entry in categories.items():
        metric = entry["metric"]
        minimum = entry["minimum_cases"]
        path = present.get(category)
        if path is None:
            errors.append(f"{CASES_RELATIVE}/{category}.json: missing category file")
            rows.append({"category": category, "metric": metric, "minimum": minimum, "count": 0,
                         "splits": {}, "difficulty": {}})
            continue
        try:
            document = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{CASES_RELATIVE}/{category}.json: invalid JSON ({exc})")
            rows.append({"category": category, "metric": metric, "minimum": minimum, "count": 0,
                         "splits": {}, "difficulty": {}})
            continue

        file_errors = validate_instance(document, schema, schema, f"{category}.json")
        errors.extend(file_errors)
        if document.get("category") != category:
            errors.append(f"{category}.json: 'category' must equal the file stem")
        if document.get("schema_version") != spec.get("schema_version"):
            errors.append(f"{category}.json: schema_version must equal the fixture spec schema_version")
        if document.get("fixture_id") != spec.get("fixture_id"):
            errors.append(f"{category}.json: fixture_id must equal the fixture spec fixture_id")

        cases = document.get("cases") if isinstance(document.get("cases"), list) else []
        splits: dict[str, int] = {}
        difficulty: dict[str, int] = {}
        for case in cases:
            if not isinstance(case, dict):
                errors.append(f"{category}.json: every entry of 'cases' must be an object")
                continue
            errors.extend(
                check_case(case, category=category, metric=metric, catalogue=catalogue,
                           schema=schema, seen_ids=seen_ids)
            )
            splits[case.get("split")] = splits.get(case.get("split"), 0) + 1
            difficulty[case.get("difficulty")] = difficulty.get(case.get("difficulty"), 0) + 1

        total = len(cases)
        if total < minimum:
            message = (
                f"[{category}] {total} cases is below the spec minimum of {minimum} "
                f"(authoring target {authoring_target(minimum)})"
            )
            if require_complete:
                errors.append(message)
        if total and (require_complete or total >= SPLIT_CHECK_MIN_CASES):
            errors.extend(check_proportions("split", splits, SPLIT_TARGET, SPLIT_TOLERANCE_POINTS, total, category))
        if total and (require_complete or total >= DIFFICULTY_CHECK_MIN_CASES):
            errors.extend(
                check_proportions("difficulty", difficulty, DIFFICULTY_TARGET,
                                  DIFFICULTY_TOLERANCE_POINTS, total, category)
            )

        rows.append({"category": category, "metric": metric, "minimum": minimum,
                     "count": total, "splits": splits, "difficulty": difficulty})

    return errors, rows


def render_table(rows: list[dict], require_complete: bool) -> str:
    header = (
        f"{'category':<24}{'cases':>6}{'min':>6}{'target':>8}"
        f"{'train':>7}{'dev':>6}{'test':>6}"
        f"{'easy':>6}{'med':>5}{'hard':>6}{'adv':>5}  metric"
    )
    lines = [header, "-" * len(header)]
    total_cases = total_minimum = total_target = 0
    for row in rows:
        splits = row["splits"]
        difficulty = row["difficulty"]
        target = authoring_target(row["minimum"])
        total_cases += row["count"]
        total_minimum += row["minimum"]
        total_target += target
        lines.append(
            f"{row['category']:<24}{row['count']:>6}{row['minimum']:>6}{target:>8}"
            f"{splits.get('train', 0):>7}{splits.get('dev', 0):>6}{splits.get('test', 0):>6}"
            f"{difficulty.get('easy', 0):>6}{difficulty.get('medium', 0):>5}"
            f"{difficulty.get('hard', 0):>6}{difficulty.get('adversarial', 0):>5}  {row['metric']}"
        )
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<24}{total_cases:>6}{total_minimum:>6}{total_target:>8}")
    lines.append("")
    lines.append(
        "mode: "
        + ("require-complete (minimums, split and difficulty mix enforced)"
           if require_complete
           else "authoring (minimums reported; split enforced at >=40 cases, difficulty mix at >=20)")
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="fail when a category is below its spec minimum or its split/difficulty mix is out of tolerance",
    )
    args = parser.parse_args(argv)

    try:
        errors, rows = validate_corpus(args.root.resolve(), require_complete=args.require_complete)
    except SchemaError as exc:
        print(f"ERROR: schema file uses an unsupported construct: {exc}")
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(render_table(rows, args.require_complete))
    print()
    if errors:
        for error in sorted(set(errors)):
            print(f"ERROR: {error}")
        print(f"\nquality corpus: FAIL ({len(set(errors))} distinct findings)")
        return 1
    print("quality corpus: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
