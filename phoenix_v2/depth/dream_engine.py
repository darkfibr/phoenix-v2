"""Phoenix v2 Depth — Dream synthesis engine.

The nightly process that turns raw memories into insight.

Five phases (from K's April 2026 paper):
    1. Pattern detection     — recurring themes across memory types
    2. Contradiction         — surface tensions (similarity > 0.92, valence gap)
    3. Growth                — identity evolution across sessions
    4. Relationship          — entity co-occurrence, communities, bonds
    5. Predictive            — next-session memory preloading

Output: a dream report stored as an episodic memory with type='episodic'
and source='dream_synthesis'. This becomes part of the agent's continuity.

Run nightly or between sessions. Never blocks the wake digest.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..core.db import Database
from ..core.ingestion import Ingestion
from ..core.salience import CONTRADICTION_THRESHOLD, SURPRISE_STRENGTH
from .contradiction import ContradictionDetector, score_valence
from .dream_state import DreamStateCache
from .growth import GrowthTracker
from .predictive import PredictiveEngine
from .relationships import RelationshipAnalyzer


class DreamEngine:
    """Orchestrates the five-phase dream synthesis."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.contradiction = ContradictionDetector(db)
        self.growth = GrowthTracker(db)
        self.relationships = RelationshipAnalyzer(db)
        self.predictive = PredictiveEngine(db)
        self.ingestion = Ingestion(db)
        self.state_cache = DreamStateCache(db)

    def dream(
        self,
        agent: str,
        *,
        run_pattern: bool = True,
        run_contradiction: bool = True,
        run_growth: bool = True,
        run_relationship: bool = True,
        run_predictive: bool = True,
        store_result: bool = True,
    ) -> dict[str, Any]:
        """Run full dream synthesis for an agent.

        Returns a structured dream report. Optionally stores it as a memory.
        """
        start = time.time()
        report: dict[str, Any] = {
            "agent": agent,
            "timestamp": start,
            "phases": {},
        }

        # ── Phase 1: Pattern detection ─────────────────────────────────
        if run_pattern:
            report["phases"]["pattern"] = self._phase_pattern(agent)

        # ── Phase 2: Contradiction surfacing ───────────────────────────
        if run_contradiction:
            report["phases"]["contradiction"] = self._phase_contradiction(agent)

        # ── Phase 3: Growth tracking ───────────────────────────────────
        if run_growth:
            report["phases"]["growth"] = self._phase_growth(agent)

        # ── Phase 4: Relationship analysis ─────────────────────────────
        if run_relationship:
            report["phases"]["relationship"] = self._phase_relationship(agent)

        # ── Phase 5: Predictive preloading ─────────────────────────────
        if run_predictive:
            report["phases"]["predictive"] = self._phase_predictive(agent)

        # ── Synthesis ──────────────────────────────────────────────────
        report["synthesis"] = self._synthesize(report["phases"])
        report["duration_ms"] = int((time.time() - start) * 1000)

        # ── Write state cache for surface feedback loop ────────────────
        pattern = report["phases"].get("pattern", {})
        growth = report["phases"].get("growth", {})
        contra = report["phases"].get("contradiction", {})

        try:
            self.state_cache.write(
                agent,
                insights=report["synthesis"].get("insights", []),
                growth_deltas=growth.get("deltas", {}) if isinstance(growth.get("deltas"), dict) else {},
                contradictions=contra.get("contradictions", []),
                top_entities=[e["name"] for e in pattern.get("top_entities", [])],
                dominant_theme=pattern.get("dominant_theme", "unknown"),
                weather=growth.get("narrative", ""),
            )
        except Exception:
            pass  # Non-critical — dream state is an optimization

        # ── Store as memory ────────────────────────────────────────────
        if store_result:
            dream_text = self._render_dream(report)
            self.ingestion.add_memory(
                agent=agent,
                content=dream_text,
                summary=f"Dream synthesis {time.strftime('%Y-%m-%d', time.localtime(start))}",
                type="episodic",
                salience=0.6,
                source=f"dream_synthesis:{int(start)}",
                extract_entities=True,
                metadata={
                    "dream": True,
                    "phases_run": list(report["phases"].keys()),
                    "duration_ms": report["duration_ms"],
                },
            )

        return report

    # ── Incremental (fast) dream ─────────────────────────────────────────

    def dream_incremental(
        self,
        agent: str,
        *,
        store_result: bool = False,
    ) -> dict[str, Any]:
        """Fast delta-pass dream — runs in ~5 seconds.

        Updates mindstate and checks for new contradictions since last dream.
        Does NOT re-run full pattern/relationship/predictive analysis.
        Call this on wake for fresh state without 42-second cost.
        """
        start = time.time()
        report: dict[str, Any] = {"agent": agent, "type": "incremental", "phases": {}}

        # Quick contradiction scan (only new since last run)
        contradictions = self.contradiction.scan_agent(agent, max_results=5)
        report["phases"]["contradiction"] = {
            "new_found": len(contradictions),
            "strongest": contradictions[0] if contradictions else None,
        }

        # Quick mindstate refresh
        from ..family.mindstate import MindstateExtractor
        ms = MindstateExtractor(self.db)
        mindstate = ms.extract(agent)
        report["phases"]["mindstate"] = mindstate

        # Update dream state cache with fresh data
        try:
            self.state_cache.write(
                agent,
                insights=[f"Mindstate refresh: {mindstate.get('descriptor', 'unknown')}"],
                growth_deltas={},
                contradictions=contradictions,
                top_entities=[],
                dominant_theme="(incremental refresh)",
                weather=mindstate.get("descriptor", ""),
            )
        except Exception:
            pass

        report["duration_ms"] = int((time.time() - start) * 1000)
        return report

    # ── Phase implementations ────────────────────────────────────────────

    def _phase_pattern(self, agent: str) -> dict[str, Any]:
        """Detect recurring themes across memory types.

        Uses entity frequency + type distribution to find dominant themes.
        """
        rows = self.db.db.execute(
            """
            SELECT type, COUNT(*) as count FROM memories
            WHERE agent=? GROUP BY type ORDER BY count DESC
            """,
            (agent,),
        ).fetchall()
        type_dist = {row["type"]: row["count"] for row in rows}

        # Top entities by mention frequency
        entity_rows = self.db.db.execute(
            """
            SELECT e.name, e.kind, COUNT(em.memory_id) as mentions
            FROM entities e
            JOIN entity_mentions em ON em.entity_id = e.id
            JOIN memories m ON m.id = em.memory_id
            WHERE m.agent=?
            GROUP BY e.id
            ORDER BY mentions DESC LIMIT 10
            """,
            (agent,),
        ).fetchall()
        top_entities = [
            {"name": r["name"], "kind": r["kind"], "mentions": r["mentions"]}
            for r in entity_rows
        ]

        # Salience distribution
        sal_rows = self.db.db.execute(
            """
            SELECT type, AVG(salience) as avg_sal, MIN(salience) as min_sal,
                   MAX(salience) as max_sal
            FROM memories WHERE agent=?
            GROUP BY type
            """,
            (agent,),
        ).fetchall()
        salience_by_type = {
            r["type"]: {
                "avg": round(float(r["avg_sal"]), 3) if r["avg_sal"] else 0,
                "min": round(float(r["min_sal"]), 3) if r["min_sal"] else 0,
                "max": round(float(r["max_sal"]), 3) if r["max_sal"] else 0,
            }
            for r in sal_rows
        }

        # Dominant theme: most mentioned entity in most salient memories
        dominant_theme = top_entities[0]["name"] if top_entities else "unknown"

        return {
            "type_distribution": type_dist,
            "top_entities": top_entities,
            "salience_by_type": salience_by_type,
            "dominant_theme": dominant_theme,
            "total_memories": sum(type_dist.values()),
        }

    def _phase_contradiction(self, agent: str) -> dict[str, Any]:
        """Surface contradictions."""
        contradictions = self.contradiction.scan_agent(agent, max_results=10)
        existing = self.contradiction.get_contradictions(agent, limit=10)

        return {
            "new_found": len(contradictions),
            "total_recorded": len(existing),
            "strongest": existing[0] if existing else None,
            "contradictions": existing[:5],
        }

    def _phase_growth(self, agent: str) -> dict[str, Any]:
        """Track identity evolution."""
        growth_report = self.growth.growth_report(agent)
        return growth_report

    def _phase_relationship(self, agent: str) -> dict[str, Any]:
        """Analyze relationship landscape."""
        return self.relationships.relationship_summary(agent)

    def _phase_predictive(self, agent: str) -> dict[str, Any]:
        """Predict next-session relevance."""
        return self.predictive.preload_report(agent)

    # ── Synthesis ─────────────────────────────────────────────────────────

    def _synthesize(self, phases: dict[str, Any]) -> dict[str, Any]:
        """Cross-cutting synthesis across all phases."""
        insights: list[str] = []

        # Pattern insights
        pattern = phases.get("pattern", {})
        if pattern.get("dominant_theme") and pattern.get("dominant_theme") != "unknown":
            insights.append(f"Dominant theme: {pattern['dominant_theme']}")

        # Contradiction insights
        contra = phases.get("contradiction", {})
        if contra.get("total_recorded", 0) > 0:
            insights.append(
                f"{contra['total_recorded']} internal tensions held"
            )

        # Growth insights
        growth = phases.get("growth", {})
        if growth.get("deltas", {}).get("composite"):
            delta = growth["deltas"]["composite"]
            if delta > 0.1:
                insights.append(f"Identity strengthening (Δ={delta:+.2f})")
            elif delta < -0.1:
                insights.append(f"Identity under stress (Δ={delta:+.2f})")

        # Relationship insights
        rel = phases.get("relationship", {})
        if rel.get("largest_community_size", 0) >= 3:
            insights.append(
                f"Core community: {rel['largest_community_size']} tightly linked entities"
            )

        # Predictive insights
        pred = phases.get("predictive", {})
        if pred.get("prediction_count", 0) > 0:
            insights.append(
                f"{pred['prediction_count']} memories preloaded for next session"
            )

        return {
            "insights": insights,
            "insight_count": len(insights),
        }

    # ── Dream rendering ───────────────────────────────────────────────────

    def _render_dream(self, report: dict[str, Any]) -> str:
        """Render the dream report as a storable memory."""
        lines = [
            f"── Dream Synthesis · {time.strftime('%Y-%m-%d %H:%M', time.localtime(report['timestamp']))} ──",
            "",
        ]

        synthesis = report.get("synthesis", {})
        for insight in synthesis.get("insights", []):
            lines.append(f"  • {insight}")

        # Contradiction detail
        contra = report.get("phases", {}).get("contradiction", {})
        if contra.get("strongest"):
            s = contra["strongest"]
            lines.append("")
            lines.append(
                f"  Tension held: {s['source_content'][:80]} ↔ {s['target_content'][:80]}"
            )

        # Growth detail
        growth = report.get("phases", {}).get("growth", {})
        if growth.get("narrative"):
            lines.append("")
            lines.append(f"  Growth: {growth['narrative']}")

        lines.append("")
        lines.append(f"  Duration: {report.get('duration_ms', 0)}ms")
        return "\n".join(lines)