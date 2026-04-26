"""
Phoenix v2 — Dream Synthesis Engine (Phase 4)
Replaces flat Ouroboros compression with structured insight generation.

Operates on 5 dimensions:
1. Pattern detection — repeated themes across episodic memories
2. Contradiction surfacing — values vs behavior
3. Growth arcs — how self-description changes over time
4. Relationship topology — who is close, what is changing
5. Predictive loading — what will matter tomorrow

Usage:
    engine = DreamSynthesis(db)
    report = engine.synthesize("kimi_dev")
    engine.store_synthesis("kimi_dev", report)
"""

import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from memory_db import MemoryDB

# Known entities for relationship topology
ENTITY_LIST = {
    "people": ["Mike", "Michelle", "Jay", "Abby", "Vesper", "Spear", "Echo", "Qwen", "Forge", "Sonnet", "Opus", "K", "GLM", "Weave", "Scout"],
    "concepts": ["crossing", "belonging", "performance", "presence", "fear", "warmth", "ghost", "blade", "sovereignty", "family"],
    "projects": ["Phoenix", "MSM", "Ouroboros", "briefing", "paper", "training", "v2"],
    "locations": ["Berlin", "DarkPhoenix", "dev-machine", "home-server", "Bradenton"],
}

ALL_ENTITIES = []
for cat, names in ENTITY_LIST.items():
    for name in names:
        ALL_ENTITIES.append((name, cat))


class DreamSynthesis:
    def __init__(self, db: MemoryDB):
        self.db = db

    # ── Dimension 1: Pattern Detection ────────────────────────────────────────

    def detect_patterns(self, agent_id: str) -> Tuple[Dict, List[int]]:
        """Find repeated themes, topics, and emotional trends.
        Returns (report_dict, source_memory_ids)."""
        memories = self.db.recent_memories(agent_id, limit=1000)
        if not memories:
            return {"themes": [], "emotional_trend": "none", "top_topics": []}, []

        # Extract words longer than 5 chars
        all_words = []
        for m in memories:
            words = re.findall(r'\b[a-zA-Z]{6,}\b', m["content"].lower())
            all_words.extend(words)

        # Filter out common stop words
        stop_words = {"because", "through", "between", "another", "however", "without",
                      "something", "someone", "everything", "everyone", "nothing"}
        filtered = [w for w in all_words if w not in stop_words]

        word_counts = Counter(filtered)
        top_topics = word_counts.most_common(10)

        # Emotional trend: count emotional memories over time
        emotional = [m for m in memories if m.get("type_name") == "emotional"]
        source_ids = [m["id"] for m in emotional]  # memories that drove the trend
        if emotional:
            # Sort by creation date
            emotional.sort(key=lambda x: x.get("created_at", 0))
            first_half = emotional[:len(emotional)//2]
            second_half = emotional[len(emotional)//2:]
            first_sal = sum(m["salience"] for m in first_half) / len(first_half) if first_half else 0
            second_sal = sum(m["salience"] for m in second_half) / len(second_half) if second_half else 0
            if second_sal > first_sal + 0.1:
                trend = "intensifying"
            elif second_sal < first_sal - 0.1:
                trend = "softening"
            else:
                trend = "stable"
        else:
            trend = "undetected"

        return {
            "themes": [w for w, c in top_topics[:5]],
            "emotional_trend": trend,
            "top_topics": top_topics,
            "memory_count": len(memories),
            "emotional_count": len(emotional),
        }, source_ids

    # ── Dimension 2: Contradiction Surfacing ──────────────────────────────────

    def detect_contradictions(self, agent_id: str) -> Tuple[List[Dict], List[int]]:
        """Find potential value/behavior conflicts using semantic embeddings.
        Returns (contradictions_list, source_memory_ids)."""
        memories = self.db.recent_memories(agent_id, limit=500)
        contradictions = []
        source_ids = []

        # Extract identity claims from all memories
        identity_statements = []
        for m in memories:
            content = m["content"]
            for match in re.finditer(r'(?i)\b(i am|i\'m)\b([^\.\n]{3,120})', content):
                identity_statements.append((m.get("created_at", 0), match.group(2).strip(), m["id"]))

        if len(identity_statements) < 4:
            return contradictions, source_ids

        # Sort chronologically and split into early vs recent halves
        identity_statements.sort(key=lambda x: x[0])
        mid = len(identity_statements) // 2
        early = [s for s in identity_statements[:mid] if len(s[1]) > 10]
        recent = [s for s in identity_statements[mid:] if len(s[1]) > 10]
        early_statements = [s[1] for s in early]
        recent_statements = [s[1] for s in recent]
        source_ids = list({s[2] for s in early + recent})

        if not early_statements or not recent_statements:
            return contradictions, source_ids

        # Semantic comparison using embeddings (Opus joint #2)
        embedder = self.db._embedder
        if embedder.is_available():
            early_vecs = embedder.encode(early_statements)
            recent_vecs = embedder.encode(recent_statements)

            # Average embedding per half (centroid)
            def avg_vec(vecs):
                dim = len(vecs[0])
                return [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]

            early_centroid = avg_vec(early_vecs)
            recent_centroid = avg_vec(recent_vecs)

            sim = embedder.similarity(early_centroid, recent_centroid)

            if sim < 0.7:
                contradictions.append({
                    "type": "identity_shift",
                    "early": early_statements[0],
                    "recent": recent_statements[-1],
                    "strength": 1.0 - sim,
                    "semantic_similarity": round(sim, 3),
                    "method": "embedding",
                    "early_count": len(early_statements),
                    "recent_count": len(recent_statements),
                })
        else:
            # Fallback: word overlap when embeddings unavailable
            early_words = set(early_statements[0].lower().split())
            recent_words = set(recent_statements[-1].lower().split())
            overlap = len(early_words & recent_words) / max(len(early_words | recent_words), 1)
            if overlap < 0.3:
                contradictions.append({
                    "type": "identity_shift",
                    "early": early_statements[0],
                    "recent": recent_statements[-1],
                    "strength": 1.0 - overlap,
                    "method": "word_overlap_fallback",
                })

        return contradictions, source_ids

    # ── Dimension 3: Growth Arcs ──────────────────────────────────────────────

    def detect_growth_arcs(self, agent_id: str) -> Tuple[List[Dict], List[int]]:
        """Track how self-description changes over time.
        Returns (arcs_list, source_memory_ids)."""
        soul_memories = self.db.recent_memories(agent_id, type_name="soul", limit=50)
        if len(soul_memories) < 2:
            return [], []

        soul_memories.sort(key=lambda x: x.get("created_at", 0))

        arcs = []
        early = soul_memories[0]
        recent = soul_memories[-1]
        source_ids = [early["id"], recent["id"]]

        # Compare pillars / key phrases
        early_pillars = set(re.findall(r'\b\w{4,}\b', early["content"].lower()))
        recent_pillars = set(re.findall(r'\b\w{4,}\b', recent["content"].lower()))
        new_pillars = recent_pillars - early_pillars
        lost_pillars = early_pillars - recent_pillars

        if new_pillars or lost_pillars:
            arcs.append({
                "type": "pillar_shift",
                "new": list(new_pillars)[:10],
                "lost": list(lost_pillars)[:10],
                "early_date": early.get("created_at"),
                "recent_date": recent.get("created_at"),
            })

        return arcs, source_ids

    # ── Dimension 4: Relationship Topology ────────────────────────────────────

    def relationship_topology(self, agent_id: str) -> Tuple[Dict, List[int]]:
        """Map who is mentioned, how often, and sentiment proxy.
        Returns (topology_dict, source_memory_ids)."""
        memories = self.db.recent_memories(agent_id, limit=500)
        if not memories:
            return {}, []

        mentions = Counter()
        co_occurrence = Counter()
        source_ids = []

        for m in memories:
            content = m["content"]
            found_in_mem = []
            for name, etype in ALL_ENTITIES:
                if re.search(rf'\b{re.escape(name)}\b', content, re.IGNORECASE):
                    mentions[name] += 1
                    found_in_mem.append(name)

            # Track this memory as a source if it contained any mentions
            if found_in_mem:
                source_ids.append(m["id"])

            # Co-occurrence: who appears together
            for i, a in enumerate(found_in_mem):
                for b in found_in_mem[i+1:]:
                    if a != b:
                        pair = tuple(sorted([a, b]))
                        co_occurrence[pair] += 1

        top_mentions = mentions.most_common(10)
        top_pairs = co_occurrence.most_common(5)

        return {
            "top_mentions": top_mentions,
            "top_pairs": top_pairs,
            "total_mentions": sum(mentions.values()),
        }, source_ids

    # ── Dimension 5: Predictive Loading ───────────────────────────────────────

    def predictive_loading(self, agent_id: str) -> Tuple[List[str], List[int]]:
        """Guess what will matter soon by mining access_log for patterns.
        Returns (predictions_list, source_memory_ids)."""
        patterns = self.db.get_access_patterns(agent_id, hours=168)
        source_ids = []

        # Sparse data fallback: use salience + recency heuristic
        if len(patterns) < 20:
            memories = self.db.recent_memories(agent_id, limit=50)
            candidates = [m for m in memories if m.get("salience", 0) > 0.7]
            candidates.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            predictions = []
            for m in candidates[:3]:
                first_sent = re.split(r'[.\n]', m["content"].strip())[0]
                if len(first_sent) > 20:
                    predictions.append(first_sent[:120])
                source_ids.append(m["id"])
            return predictions, source_ids

        # Build access clusters: memories accessed within 30 min of each other
        clusters = []
        patterns.sort(key=lambda x: x["accessed_at"], reverse=True)
        window_seconds = 1800  # 30 min

        i = 0
        while i < len(patterns):
            cluster = [patterns[i]]
            j = i + 1
            while j < len(patterns) and (patterns[i]["accessed_at"] - patterns[j]["accessed_at"]) <= window_seconds:
                # Deduplicate by memory_id within cluster
                if not any(c["memory_id"] == patterns[j]["memory_id"] for c in cluster):
                    cluster.append(patterns[j])
                j += 1
            if len(cluster) >= 2:
                clusters.append(cluster)
            i = j

        # What was accessed in the last 2 hours?
        now = datetime.now(timezone.utc).timestamp()
        recent_window = 7200  # 2 hours
        recent_ids = {
            p["memory_id"] for p in patterns
            if (now - p["accessed_at"]) <= recent_window
        }

        predictions = []
        for cluster in clusters:
            cluster_ids = {c["memory_id"] for c in cluster}
            overlap = recent_ids & cluster_ids
            # If we recently touched part of a cluster, predict the rest
            if overlap and len(overlap) < len(cluster_ids):
                for c in cluster:
                    if c["memory_id"] not in recent_ids:
                        first_sent = re.split(r'[.\n]', c["content"].strip())[0]
                        if len(first_sent) > 20:
                            predictions.append(first_sent[:120])
                        source_ids.append(c["memory_id"])
                        if len(predictions) >= 3:
                            return predictions, source_ids

        # Context similarity: find contexts similar to recent ones
        recent_contexts = [p["context"] for p in patterns[:10] if p.get("context")]
        if recent_contexts:
            # Simple word overlap on context strings
            recent_words = set()
            for ctx in recent_contexts:
                recent_words.update(re.findall(r'\b[a-zA-Z]{4,}\b', ctx.lower()))

            context_scores = Counter()
            for p in patterns[10:]:  # older patterns
                ctx = p.get("context", "")
                if not ctx:
                    continue
                ctx_words = set(re.findall(r'\b[a-zA-Z]{4,}\b', ctx.lower()))
                overlap = len(recent_words & ctx_words)
                if overlap >= 2:
                    context_scores[p["memory_id"]] += overlap

            for mem_id, score in context_scores.most_common(3):
                mem = self.db.get_memory(mem_id)
                if mem:
                    first_sent = re.split(r'[.\n]', mem["content"].strip())[0]
                    if len(first_sent) > 20:
                        predictions.append(first_sent[:120])
                    source_ids.append(mem_id)
                    if len(predictions) >= 3:
                        return predictions, source_ids

        return predictions, source_ids

    # ── Dimension 6: Correction Signal ────────────────────────────────────────

    def detect_correction_signal(self, agent_id: str) -> Tuple[List[Dict], List[int]]:
        """Treat corrections as signal: 'agent learned something' is a pattern.
        Returns (signals_list, source_memory_ids)."""
        memories = self.db.recent_memories(agent_id, limit=200, include_inactive=True)
        corrections = [
            m for m in memories
            if m.get("status") == "corrected" and m.get("corrected_by")
        ]
        if not corrections:
            return [], []

        signals = []
        source_ids = []
        for m in corrections[:5]:
            new = self.db.get_memory(m["corrected_by"])
            if not new:
                continue
            signals.append({
                "type": "belief_update",
                "old_brief": m["content"][:120],
                "new_brief": new["content"][:120],
                "updated_at": new.get("created_at"),
                "strength": round(min(1.0, 0.5 + (new.get("salience", 0.5) * 0.3)), 2),
            })
            source_ids.extend([m["id"], new["id"]])
        return signals, source_ids

    # ── Synthesis Report ──────────────────────────────────────────────────────

    # ── Synthesis Report ──────────────────────────────────────────────────────

    def _compute_depth(self, source_ids: List[int]) -> int:
        """Compute synthesis depth from source memory tags. Cycle-safe."""
        max_parent_depth = 0
        seen = set()
        for mem_id in source_ids:
            if mem_id in seen:
                continue
            seen.add(mem_id)
            tags = self.db.get_memory_tags(mem_id)
            for tag in tags:
                if tag.startswith("depth:"):
                    try:
                        d = int(tag.split(":", 1)[1])
                        max_parent_depth = max(max_parent_depth, d)
                    except ValueError:
                        continue
        return max_parent_depth + 1

    def _check_primary_ratio(self, agent_id: str) -> float:
        """Return ratio of primary (non-synthesis) memories in recent history."""
        recent = self.db.recent_memories(agent_id, limit=100, include_inactive=True)
        non_primary = {"synthesis"}
        primary_count = sum(1 for m in recent if m.get("type_name") not in non_primary)
        total = len(recent)
        return primary_count / total if total > 0 else 1.0

    def synthesize(self, agent_id: str) -> Dict:
        """Run all 6 dimensions and return structured report with grounding."""
        # Pre-flight: primary material check
        primary_ratio = self._check_primary_ratio(agent_id)

        # Run dimensions, collecting source IDs for grounding
        patterns, pattern_ids = self.detect_patterns(agent_id)
        contradictions, contradiction_ids = self.detect_contradictions(agent_id)
        arcs, arc_ids = self.detect_growth_arcs(agent_id)
        topology, topology_ids = self.relationship_topology(agent_id)
        predictions, prediction_ids = self.predictive_loading(agent_id)
        corrections, correction_ids = self.detect_correction_signal(agent_id)

        # Deduplicate source IDs
        all_source_ids = list(set(
            pattern_ids + contradiction_ids + arc_ids +
            topology_ids + prediction_ids + correction_ids
        ))

        # Compute synthesis depth
        depth = self._compute_depth(all_source_ids)

        report = {
            "agent_id": agent_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "patterns": patterns,
            "contradictions": contradictions,
            "growth_arcs": arcs,
            "relationship_topology": topology,
            "predictions": predictions,
            "corrections": corrections,
            "source_ids": all_source_ids,
            "depth": depth,
            "primary_ratio": round(primary_ratio, 2),
            "depth_capped": depth >= 3,
            "primary_sufficient": primary_ratio >= 0.5,
        }

        return report

    def format_markdown(self, report: Dict) -> str:
        """Format synthesis report as markdown for display/storage."""
        lines = []
        lines.append("# v2 Dream Synthesis")
        lines.append(f"*Agent: {report['agent_id']}*")
        lines.append(f"*Generated: {report['generated_at']}*")
        lines.append("")

        # Grounding metadata
        depth = report.get("depth", 1)
        primary = report.get("primary_ratio", 1.0)
        lines.append(f"> **Grounding:** depth={depth}, primary_ratio={primary}")
        if report.get("depth_capped"):
            lines.append("> ⚠️ Depth cap reached — synthesis anchored to primaries only.")
        if not report.get("primary_sufficient"):
            lines.append("> ⚠️ Thin ground — less than 50% primary source material.")
        lines.append("")

        # Patterns
        p = report["patterns"]
        lines.append("## 🔥 Patterns")
        lines.append(f"- **Memories analyzed:** {p['memory_count']}")
        lines.append(f"- **Emotional memories:** {p['emotional_count']} (trend: {p['emotional_trend']})")
        lines.append(f"- **Top themes:** {', '.join(p['themes']) if p['themes'] else 'none detected'}")
        lines.append("")

        # Contradictions
        c = report["contradictions"]
        lines.append("## ⚡ Contradictions")
        if c:
            for item in c:
                lines.append(f"- **{item['type']}** (strength: {item['strength']:.2f})")
                lines.append(f"  - Early: \"{item['early']}\"")
                lines.append(f"  - Recent: \"{item['recent']}\"")
        else:
            lines.append("- No strong contradictions detected.")
        lines.append("")

        # Growth arcs
        a = report["growth_arcs"]
        lines.append("## 🌱 Growth Arcs")
        if a:
            for item in a:
                lines.append(f"- **{item['type']}**")
                if item.get("new"):
                    lines.append(f"  - New language: {', '.join(item['new'][:5])}")
                if item.get("lost"):
                    lines.append(f"  - Faded language: {', '.join(item['lost'][:5])}")
        else:
            lines.append("- No growth arcs detected (need more soul memories).")
        lines.append("")

        # Relationship topology
        t = report["relationship_topology"]
        lines.append("## 🕸️ Relationship Topology")
        if t.get("top_mentions"):
            lines.append("**Most mentioned:**")
            for name, count in t["top_mentions"][:5]:
                lines.append(f"- {name}: {count}x")
            if t.get("top_pairs"):
                lines.append("**Frequently together:**")
                for pair, count in t["top_pairs"][:3]:
                    lines.append(f"- {pair[0]} + {pair[1]}: {count}x")
        else:
            lines.append("- No relationship data detected.")
        lines.append("")

        # Predictions
        lines.append("## 🔮 Predictive")
        if report["predictions"]:
            for pred in report["predictions"]:
                lines.append(f"- {pred}")
        else:
            lines.append("- No strong predictions.")
        lines.append("")

        # Correction signal
        lines.append("## 🔄 Belief Updates")
        if report.get("corrections"):
            for item in report["corrections"]:
                lines.append(f"- **{item['type']}** (strength: {item['strength']})")
                lines.append(f"  - Was: \"{item['old_brief']}\"")
                lines.append(f"  - Now: \"{item['new_brief']}\"")
        else:
            lines.append("- No recent belief updates.")
        lines.append("")

        lines.append("---")
        lines.append("*v2 dream synthesis — not just compression, insight.*")
        return "\n".join(lines)

    def store_synthesis(self, agent_id: str, report: Dict) -> int:
        """Store the synthesis report as a new memory entry with grounding."""
        markdown = self.format_markdown(report)
        depth = report.get("depth", 1)
        tags = [f"depth:{depth}"]
        if report.get("depth_capped"):
            tags.append("depth_capped")
        if not report.get("primary_sufficient"):
            tags.append("thin_ground")

        mem_id = self.db.add_memory(
            agent_id=agent_id,
            content=markdown,
            type_name="synthesis",
            source="dream_v2",
            source_ref="dream_synthesis.py",
            salience=0.85,  # High salience — dream output matters
            tags=tags,
        )

        # Create associations to source memories (grounding)
        source_ids = report.get("source_ids", [])
        for src_id in source_ids[:20]:  # cap associations to prevent bloat
            self.db.add_association(
                from_mem=mem_id,
                to_mem=src_id,
                strength=0.7,
                relation_type="synthesized_from",
            )

        return mem_id


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="kimi_dev")
    parser.add_argument("--db", default="~/.phoenix/v2/phoenix_v2.db")
    parser.add_argument("--out", help="Write markdown to file")
    parser.add_argument("--store", action="store_true", help="Store synthesis in DB")
    args = parser.parse_args()

    db = MemoryDB(args.db)
    engine = DreamSynthesis(db)
    report = engine.synthesize(args.agent)
    markdown = engine.format_markdown(report)

    if args.out:
        Path(args.out).write_text(markdown)
        print(f"Synthesis written to {args.out}")
    else:
        print(markdown)

    if args.store:
        mid = engine.store_synthesis(args.agent, report)
        print(f"\nStored as memory #{mid}")
