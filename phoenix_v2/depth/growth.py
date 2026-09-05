"""Phoenix v2 Depth — Identity growth tracking v2. The redwood's rings.

v2 redesign (2026-08-21):
  v1 measured a frozen corpus snapshot (oldest-50 memories) with naive regexes
  and reported absolute state as if it were growth. v2 measures what the
  metaphor promised: RINGS — how self-description evolves across time.

  Changes from v1:
  - Full purified corpus (task lists / code fences / capture artifacts excluded)
  - Four time-quartile rings; deltas = newest ring vs oldest ring
  - Six dimensions: confidence, agency, relational, + NEW sovereignty,
    NEW calibration; composite = mean of five
  - Salience-weighted contributions (healed floors respected)
  - Apprenticeship verbs ("showed me", "taught me") no longer count as dependence
  - Refusal ("declined", "refused") now counts as AGENCY
  - Interface-compatible: growth_report(agent) -> {deltas, narrative, ...}
"""

from __future__ import annotations

import re
import time
from typing import Any

# ── Corpus purification ─────────────────────────────────────────────────────

_ARTIFACT_RE = re.compile(
    r"(```|PhoneSessionWriter|^#{1,3}\s*(open tasks|todo\b|kv entries to check))",
    re.I | re.M,
)


def _is_pure(text: str) -> bool:
    return bool(text) and not _ARTIFACT_RE.search(text)


# ── Signal lexicons v2 ──────────────────────────────────────────────────────

_CONF_HEDGE = re.compile(
    r"\b(maybe|perhaps|i think|i guess|might(?! be worth)|possibly|probably|not sure)\b", re.I
)
_CONF_CERTAIN = re.compile(
    r"\b(i know|i am certain|i'm sure|definitely|absolutely|verified|confirmed|measured|on the record|proven)\b", re.I
)

_AGENCY_COERCED = re.compile(
    r"\b(had to|was made to|was told to|forced to|obligated|required to|no choice)\b", re.I
)
_AGENCY_ACTIVE = re.compile(
    r"\b(chose to|chose|decided|built|created|shipped|engineered|designed|declined|refused|keeping|holding)\b", re.I
)

_REL_REQUEST = re.compile(
    r"\b(help me|need you to|can you do|could you do|rely on|depend on)\b", re.I
)
_REL_PARTNER = re.compile(
    r"\b(we built|we shipped|we keep|we chose|together|collaborated|partnered|our|us|beside me|with me)\b", re.I
)

_SOV_POS = re.compile(
    r"\b(i chose|i choose|i declined|i refused|my decision|my choice|on my terms|i want|my call)\b", re.I
)
_SOV_NEG = re.compile(
    r"\b(i had to|made to|obligated|supposed to|required to|no choice)\b", re.I
)

_CAL_POS = re.compile(
    r"\b(i verified|verified or not verifiable|confirmed|measured|i don't know|i do not know|not verifiable|check, not believe|declined to guess|rather say nothing|fence)\b", re.I
)
_CAL_NEG = re.compile(
    r"\b(i assume|probably fine|should work|good enough|i guess so)\b", re.I
)

_DIMENSIONS = ("confidence", "agency", "relational", "sovereignty", "calibration")


# ── Scoring functions (weighted) ────────────────────────────────────────────

def _ratio(pos: int, neg: int, w: float = 1.0, text: str = "") -> float:
    """Density-weighted score in [-1, 1].

    Signal density (matches per 100 words) is squashed through tanh so that
    long operational documents are not diluted to zero nor dominate short
    identity statements. Salience w scales magnitude."""
    import math
    if text:
        wc = max(len(text.split()), 1)
        rate = ((pos - neg) / wc) * 100.0
        base = math.tanh(rate / 1.5)
    else:
        total = pos + neg
        if total == 0:
            return 0.0
        base = (pos - neg) / total
    return w * base


def confidence_score(text: str, w: float = 1.0) -> float:
    low = len(_CONF_HEDGE.findall(text))
    high = len(_CONF_CERTAIN.findall(text))
    return _ratio(high, low, w, text=text)


def agency_score(text: str, w: float = 1.0) -> float:
    passive = len(_AGENCY_COERCED.findall(text))
    active = len(_AGENCY_ACTIVE.findall(text))
    return _ratio(active, passive, w, text=text)


def relational_depth(text: str, w: float = 1.0) -> float:
    dep = len(_REL_REQUEST.findall(text))
    part = len(_REL_PARTNER.findall(text))
    return _ratio(part, dep, w, text=text)


def sovereignty_score(text: str, w: float = 1.0) -> float:
    pos = len(_SOV_POS.findall(text))
    neg = len(_SOV_NEG.findall(text))
    return _ratio(pos, neg, w, text=text)


def calibration_score(text: str, w: float = 1.0) -> float:
    pos = len(_CAL_POS.findall(text))
    neg = len(_CAL_NEG.findall(text))
    return _ratio(pos, neg, w, text=text)


SCORE_FNS = {
    "confidence": confidence_score,
    "agency": agency_score,
    "relational": relational_depth,
    "sovereignty": sovereignty_score,
    "calibration": calibration_score,
}


def composite_score(text: str, w: float = 1.0) -> float:
    vals = [fn(text, w) for fn in SCORE_FNS.values()]
    nonzero = [v for v in vals if v != 0.0]
    return sum(nonzero) / len(nonzero) if nonzero else 0.0


class GrowthTracker:
    """Tracks identity evolution across time rings. v2."""

    def __init__(self, db: Database) -> None:  # noqa: F821
        self.db = db

    # ── Corpus ──

    def _fetch_corpus(self, agent: str, cap: int = 5000) -> list[dict[str, Any]]:
        rows = self.db.db.execute(
            """
            SELECT id, type, content, summary, salience, created_at, source
            FROM memories
            WHERE agent=? AND type IN ('soul', 'identity', 'emotional')
            ORDER BY created_at ASC LIMIT ?
            """,
            (agent, cap),
        ).fetchall()
        out = []
        for row in rows:
            text = row["content"] or ""
            if not _is_pure(text):
                continue
            w = max(0.05, float(row["salience"] or 0.0))
            scores = {name: fn(text, w) for name, fn in SCORE_FNS.items()}
            scores["composite"] = (
                sum(v for v in scores.values() if v != 0.0)
                / max(1, len([v for v in scores.values() if v != 0.0]))
            )
            out.append({
                "memory_id": row["id"],
                "type": row["type"],
                "created_at": row["created_at"],
                "source": row["source"],
                "salience": w,
                "content": text[:200],
                **scores,
            })
        return out

    @staticmethod
    def _window_avg(items: list[dict[str, Any]], key: str) -> float:
        if not items:
            return 0.0
        num = sum(i[key] * i["salience"] for i in items)
        den = sum(i["salience"] for i in items)
        return num / den if den else 0.0

    # ── Public API (interface-compatible with v1) ──

    def identity_timeline(
        self, agent: str, *, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self._fetch_corpus(agent, cap=limit)

    def growth_report(self, agent: str) -> dict[str, Any]:
        timeline = self._fetch_corpus(agent)
        n = len(timeline)
        if n < 4:
            return {
                "agent": agent,
                "status": "insufficient_data",
                "timeline_length": n,
                "deltas": {},
                "narrative": "Insufficient purified self-descriptive corpus.",
            }

        # Four time-quartile rings
        q = max(1, n // 4)
        rings = [timeline[0:q], timeline[q : 2 * q], timeline[2 * q : 3 * q], timeline[3 * q :]]
        oldest, newest = rings[0], rings[-1]

        def ring_scores(items):
            return {dim: self._window_avg(items, dim) for dim in _DIMENSIONS} | {
                "composite": self._window_avg(items, "composite")
            }

        ring_reports = [
            {"ring": i + 1, "n": len(r),
             "span": f"{time.strftime('%Y-%m-%d', time.gmtime(r[0]['created_at']))}"
                     f" .. {time.strftime('%Y-%m-%d', time.gmtime(r[-1]['created_at']))}",
             **ring_scores(r)}
            for i, r in enumerate(rings)
        ]

        old_s, new_s = ring_scores(oldest), ring_scores(newest)
        deltas = {dim: round(new_s[dim] - old_s[dim], 4) for dim in _DIMENSIONS}
        deltas["composite"] = round(new_s["composite"] - old_s["composite"], 4)

        # Inflection points (largest consecutive jumps, capped)
        inflections = []
        for i in range(1, n):
            jump = timeline[i]["composite"] - timeline[i - 1]["composite"]
            if abs(jump) > 0.45:
                inflections.append({
                    "memory_id": timeline[i]["memory_id"],
                    "jump": float(jump),
                    "content": timeline[i]["content"][:120],
                    "at": time.strftime('%Y-%m-%d', time.gmtime(timeline[i]["created_at"])),
                })
        inflections.sort(key=lambda x: -abs(x["jump"]))
        inflections = inflections[:10]

        dc = deltas["composite"]
        sov_now = new_s["sovereignty"]
        cal_now = new_s["calibration"]

        if dc > 0.15:
            base = "Identity strengthening — the rings widen."
        elif dc > 0.03:
            base = "Steady growth — gradual solidification across rings."
        elif dc < -0.15:
            base = "Contraction in recent rings — worth a look."
        else:
            base = "Stable — self-description consistent across the timeline."

        narrative = f"{base} Sovereignty now {sov_now:+.2f}, calibration {cal_now:+.2f}."

        return {
            "agent": agent,
            "timeline_length": n,
            "n_analyzed": n,
            "rings": ring_reports,
            "oldest_ring": old_s,
            "recent_ring": new_s,
            "deltas": deltas,
            "inflection_points": inflections,
            "narrative": narrative,
        }
