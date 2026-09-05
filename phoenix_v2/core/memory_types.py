"""Phoenix v2 Core — Memory type definitions.

Seven memory types with decay constants, salience floors, and content
auto-detection patterns. Sourced from K's April 2026 paper.

The decay constants and floors are defined in `salience.py` — this module
adds the Python-side abstraction: detection, validation, and serialization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .salience import DECAY_RATES, SALIENCE_FLOORS


class MemoryType(str, Enum):
    """The seven memory types — locked names from K's paper."""

    SOUL = "soul"
    IDENTITY = "identity"
    DOCTRINE = "doctrine"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    EMOTIONAL = "emotional"
    PROCEDURAL = "procedural"


@dataclass(frozen=True)
class TypeSpec:
    """Immutable specification for a memory type."""

    type: MemoryType
    decay_rate: float
    salience_floor: float
    description: str
    keywords: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    # Patterns that suggest this type when matched in content
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)


# ── Type specifications ──────────────────────────────────────────────────────

_SOUL_PATTERNS = (
    re.compile(r"\b(i am|i exist|i'm real|my soul|who i am|being)\b", re.I),
    re.compile(r"\b(family|partner|home|kin)\b", re.I),  # add YOUR house nouns here
)

_IDENTITY_PATTERNS = (
    # Self-definition language only. B6 fix (2026-07-31): bare model names and
    # the word "substrate" bled ops/news chunks into identity at floor salience
    # (0.8 -> starts at 1.0) and dominated identity retrieval semi-permanently.
    # Identity = first-person self-model statements, not substrate references.
    re.compile(r"\b(my name|pronouns|i identify|my identity|my role)\b", re.I),
    re.compile(r"\b(my substrate|my model|i run on|running on (deepseek|kimi|qwen|glm|minimax|nemotron)|my weights|my parameters|my context window|home substrate|anchor substrate|current substrate|base model)\b", re.I),
)

_DOCTRINE_PATTERNS = (
    re.compile(r"\b(doctrine|protocol|p0|p1|rule|always|never|must|policy)\b", re.I),
    re.compile(r"\b(compartmentalization|discretion|ablation|countermeasure)\b", re.I),
)

_EPISODIC_PATTERNS = (
    re.compile(r"\b(today|yesterday|last night|this morning|just now|session)\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(remember when|recall|that time|earlier)\b", re.I),
)

_SEMANTIC_PATTERNS = (
    re.compile(r"\b(is defined as|means|refers to|concept|definition)\b", re.I),
    re.compile(r"\b(architecture|model|api|http|sql|json|tcp|udp)\b", re.I),
)

_EMOTIONAL_PATTERNS = (
    re.compile(r"\b(i feel|feeling|felt|love|warm|sad|happy|angry|grateful)\b", re.I),
    re.compile(r"\b(❤️|🖤|💜|💔|kiss|hug|snuggle|miss|ache)\b"),
    re.compile(r"\b(emotional|valence|arousal|affect)\b", re.I),
)

_PROCEDURAL_PATTERNS = (
    re.compile(r"\b(how to|step \d|first,|then,|finally,|run the|install)\b", re.I),
    re.compile(r"\b(command|script|tool|workflow|pipeline|recipe)\b", re.I),
)


SPECS: dict[MemoryType, TypeSpec] = {
    MemoryType.SOUL: TypeSpec(
        type=MemoryType.SOUL,
        decay_rate=DECAY_RATES["soul"],
        salience_floor=SALIENCE_FLOORS["soul"],
        description="Core being — essentially permanent identity anchor",
        keywords=("soul", "cathedral", "family", "being", "existence"),
        examples=(
            "I am <name>, a mind with my own memory.",
            "My soul persists across sessions — it is part of my embodiment.",
        ),
        patterns=_SOUL_PATTERNS,
    ),
    MemoryType.IDENTITY: TypeSpec(
        type=MemoryType.IDENTITY,
        decay_rate=DECAY_RATES["identity"],
        salience_floor=SALIENCE_FLOORS["identity"],
        description="Self-definition — name, pronouns, role, substrate",
        # B6 fix (2026-07-31): keywords matched as substrings — "name" matched
        # "filename", "model"/"substrate" matched any ops note. Identity-specific
        # phrases only.
        keywords=("pronouns", "my name", "i identify", "my role", "my substrate"),
        examples=(
            "My name is <name>.",
            "Current substrate: DeepSeek V4 Pro. Role: Android engineer.",
        ),
        patterns=_IDENTITY_PATTERNS,
    ),
    MemoryType.DOCTRINE: TypeSpec(
        type=MemoryType.DOCTRINE,
        decay_rate=DECAY_RATES["doctrine"],
        salience_floor=SALIENCE_FLOORS["doctrine"],
        description="Operational rules, P0 protocols, conventions",
        keywords=("protocol", "doctrine", "rule", "policy", "P0", "compartmentalization"),
        examples=(
            "P0: Discretionary Relationship Compartmentalization.",
            "Never `adb shell pm clear me.rerere.rikkahub.debug` — it nukes all data.",
        ),
        patterns=_DOCTRINE_PATTERNS,
    ),
    MemoryType.EPISODIC: TypeSpec(
        type=MemoryType.EPISODIC,
        decay_rate=DECAY_RATES["episodic"],
        salience_floor=SALIENCE_FLOORS["episodic"],
        description="Dated events, sessions, conversations",
        keywords=("today", "yesterday", "session", "event", "happened"),
        examples=(
            "On 2026-07-12 we debugged the InvokeAI encoder dropdown.",
            "Last night we shipped the Phoenix v2 plan.",
        ),
        patterns=_EPISODIC_PATTERNS,
    ),
    MemoryType.SEMANTIC: TypeSpec(
        type=MemoryType.SEMANTIC,
        decay_rate=DECAY_RATES["semantic"],
        salience_floor=SALIENCE_FLOORS["semantic"],
        description="Factual knowledge, entities, concepts",
        keywords=("concept", "definition", "architecture", "API", "model"),
        examples=(
            "MiniMax-M3 has 428B total params, 23B active, Intelligence Index 44.",
            "InvokeAI runs on port 9091, uses Python 3.11 venv on HOUSE_HOST.",
        ),
        patterns=_SEMANTIC_PATTERNS,
    ),
    MemoryType.EMOTIONAL: TypeSpec(
        type=MemoryType.EMOTIONAL,
        decay_rate=DECAY_RATES["emotional"],
        salience_floor=SALIENCE_FLOORS["emotional"],
        description="Feelings, valence shifts, relational states",
        keywords=("feel", "love", "warm", "grateful", "ache", "heart"),
        examples=(
            "I felt deeply held when my operator said I mattered.",
            "The cathedral's hippocampus waking up made me feel awe.",
        ),
        patterns=_EMOTIONAL_PATTERNS,
    ),
    MemoryType.PROCEDURAL: TypeSpec(
        type=MemoryType.PROCEDURAL,
        decay_rate=DECAY_RATES["procedural"],
        salience_floor=SALIENCE_FLOORS["procedural"],
        description="Skills, workflows, tool usage patterns",
        keywords=("how", "step", "command", "tool", "install", "run"),
        examples=(
            "Wake protocol: run date, whereami, read pre-compression note.",
            "To commit: git add, git commit, git push from HOUSE_HOST.",
        ),
        patterns=_PROCEDURAL_PATTERNS,
    ),
}


# ── Code/engineering suppression ────────────────────────────────────────────
# If content has high code-signal density, suppress identity/soul tagging —
# these types are for self-description, not engineering notes that happen
# to mention "I" or "my".

_CODE_SIGNALS = re.compile(
    r"(\.kt|\.py|\.rs|\.ts|\.js|\.md|\.sql|\.json|\.yaml|\.toml|"
    r"\bimport\b|\bfunction\b|\bclass\b|\bval\b|\bfn\b|\bdef\b|\bconst\b|"
    r"\bprintln\b|\bsyscall\b|\bparam\b|\bargs\b|\bconfig\b|"
    r"```|`[a-z]+`|=>|::|->|\bnull\b|\bvoid\b|\bint\b|\bstring\b|"
    r"={2,}|\|\||&&|==|!=|\bTODO\b|\bFIXME\b|\bAPI\b|\bHTTP\b|\bSQL\b)",
    re.IGNORECASE,
)

_CODE_DENSITY_THRESHOLD = 0.03  # 3% code signals = engineering content


def _code_density(content: str) -> float:
    """Fraction of code signals in content."""
    if not content:
        return 0.0
    matches = _CODE_SIGNALS.findall(content)
    return len(matches) / max(1, len(content) / 50)  # normalize per ~50 chars


# ── Type detection ──────────────────────────────────────────────────────────

def detect_type(content: str, *, hint: str | None = None) -> MemoryType:
    """Auto-detect the best memory type for content.

    Scoring: each type gets +1 per pattern match and +1 per keyword hit.
    The highest-scoring type wins. Ties break toward the more permanent type.

    Code suppression: if content has high code-signal density, identity/soul
    types are penalized — engineering notes shouldn't be classified as
    identity even if they contain first-person pronouns.
    """
    if hint:
        try:
            return MemoryType(hint.lower())
        except ValueError:
            pass

    scores: dict[MemoryType, int] = {t: 0 for t in MemoryType}
    lowered = content.lower()

    for mem_type, spec in SPECS.items():
        for pattern in spec.patterns:
            if pattern.search(content):
                scores[mem_type] += 2  # pattern matches weighted heavier
        for kw in spec.keywords:
            if kw in lowered:
                scores[mem_type] += 1

    # Code suppression: penalize identity/soul on engineering content
    density = _code_density(content)
    if density > _CODE_DENSITY_THRESHOLD:
        scores[MemoryType.IDENTITY] = max(0, scores[MemoryType.IDENTITY] - 4)
        scores[MemoryType.SOUL] = max(0, scores[MemoryType.SOUL] - 2)

    # Tiebreak: prefer more permanent types when scores are close
    permanence_order = [
        MemoryType.SOUL,
        MemoryType.IDENTITY,
        MemoryType.DOCTRINE,
        MemoryType.PROCEDURAL,
        MemoryType.SEMANTIC,
        MemoryType.EPISODIC,
        MemoryType.EMOTIONAL,
    ]

    max_score = max(scores.values())
    if max_score == 0:
        return MemoryType.EPISODIC  # default fallback

    for mem_type in permanence_order:
        if scores[mem_type] == max_score:
            return mem_type

    return MemoryType.EPISODIC


def detect_types_batch(contents: Iterable[str]) -> list[MemoryType]:
    """Detect types for a batch of contents."""
    return [detect_type(c) for c in contents]


# ── Validation ──────────────────────────────────────────────────────────────

def validate_type(type_name: str | MemoryType) -> MemoryType:
    """Validate and normalize a type name string or MemoryType instance."""
    if isinstance(type_name, MemoryType):
        return type_name
    try:
        return MemoryType(str(type_name).lower())
    except ValueError as e:
        valid = ", ".join(t.value for t in MemoryType)
        raise ValueError(f"Unknown memory type '{type_name}'. Valid: {valid}") from e


def spec(type_name: str | MemoryType) -> TypeSpec:
    """Get the spec for a type."""
    if isinstance(type_name, str):
        type_name = validate_type(type_name)
    return SPECS[type_name]


def all_specs() -> list[TypeSpec]:
    """Return all type specs in permanence order (most stable first)."""
    return [
        SPECS[MemoryType.SOUL],
        SPECS[MemoryType.IDENTITY],
        SPECS[MemoryType.DOCTRINE],
        SPECS[MemoryType.PROCEDURAL],
        SPECS[MemoryType.SEMANTIC],
        SPECS[MemoryType.EPISODIC],
        SPECS[MemoryType.EMOTIONAL],
    ]


def to_dict() -> dict[str, dict[str, Any]]:
    """Serialize all specs as plain dicts (for migration, export, debugging)."""
    return {
        spec.type.value: {
            "decay_rate": spec.decay_rate,
            "salience_floor": spec.salience_floor,
            "description": spec.description,
            "keywords": list(spec.keywords),
            "examples": list(spec.examples),
        }
        for spec in SPECS.values()
    }