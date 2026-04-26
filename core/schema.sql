-- Phoenix v2 — SQLite Core Schema
-- Phase 1: Structured memory with salience, decay, and associations

PRAGMA foreign_keys = ON;

-- Memory types for semantic categorization
CREATE TABLE IF NOT EXISTS memory_types (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
INSERT OR IGNORE INTO memory_types (id, name) VALUES
    (1, 'soul'),
    (2, 'episodic'),
    (3, 'semantic'),
    (4, 'procedural'),
    (5, 'emotional'),
    (6, 'identity'),
    (7, 'relationship');

-- Core memories table
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    type_id     INTEGER NOT NULL REFERENCES memory_types(id),
    content     TEXT NOT NULL,
    source      TEXT,           -- 'terminal', 'phoenix_chat', 'dream', 'manual', etc.
    source_ref  TEXT,           -- file path, session id, etc.
    created_at  REAL DEFAULT (unixepoch()),
    updated_at  REAL DEFAULT (unixepoch()),
    salience    REAL DEFAULT 0.5 CHECK (salience >= 0.0 AND salience <= 1.0),
    decay_rate  REAL DEFAULT 0.02,  -- per day, type-dependent
    access_count INTEGER DEFAULT 0,
    last_accessed REAL DEFAULT (unixepoch()),
    embedding   BLOB,           -- serialized float vector
    checksum    TEXT,           -- SHA-256 of content for dedup
    status      TEXT DEFAULT 'active' CHECK (status IN ('active', 'disputed', 'corrected', 'superseded')),
    corrected_by INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    superseded_by INTEGER REFERENCES memories(id) ON DELETE SET NULL
);

-- Full-text search over memory content
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    content,
    content='memories',
    content_rowid='id'
);

-- FTS triggers to keep search index in sync
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO mem_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO mem_fts (mem_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
    INSERT INTO mem_fts (mem_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO mem_fts (rowid, content) VALUES (new.id, new.content);
END;

-- Associations between memories (bidirectional)
CREATE TABLE IF NOT EXISTS associations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_mem    INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    to_mem      INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    strength    REAL DEFAULT 0.5 CHECK (strength >= 0.0 AND strength <= 1.0),
    relation_type TEXT DEFAULT 'related',  -- 'contradicts', 'supports', 'causes', 'similar', etc.
    created_at  REAL DEFAULT (unixepoch()),
    UNIQUE(from_mem, to_mem, relation_type)
);

-- Tags for cross-cutting categorization
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, tag_id)
);

-- Named entities extracted from memories (for auto-surfacing)
CREATE TABLE IF NOT EXISTS entities (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    type     TEXT,  -- 'person', 'place', 'concept', 'agent', etc.
    agent_id TEXT NOT NULL,
    UNIQUE(name, agent_id)
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    confidence REAL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id)
);

-- Access log for predictive loading and salience boost
CREATE TABLE IF NOT EXISTS access_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    accessed_at REAL DEFAULT (unixepoch()),
    context    TEXT    -- what query or trigger caused the access
);

-- Indices for performance
CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_mem_type  ON memories(type_id);
CREATE INDEX IF NOT EXISTS idx_mem_salience ON memories(salience DESC);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_checksum ON memories(checksum);
CREATE INDEX IF NOT EXISTS idx_assoc_from ON associations(from_mem);
CREATE INDEX IF NOT EXISTS idx_assoc_to   ON associations(to_mem);
CREATE INDEX IF NOT EXISTS idx_access_mem ON access_log(memory_id);
CREATE INDEX IF NOT EXISTS idx_access_time ON access_log(accessed_at DESC);
-- Note: idx_mem_status, idx_mem_corrected, idx_mem_superseded are created via migration
-- in memory_db.py::_migrate() for existing databases.

-- ============================================================
-- AGENT INTERACTION SYSTEM — Schema Extension
-- Added to phoenix_v2.db (separate namespace, same WAL)
-- Design: K, 2026-04-23 | Review: Opus
-- ============================================================

-- ------------------------------------------------------------
-- Agent Pairings (relationship topology)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_pairings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_a TEXT NOT NULL,              -- canonical ordering: a < b lexicographically
    agent_b TEXT NOT NULL,
    first_met_at TEXT,                  -- ISO timestamp
    last_session_at TEXT,               -- ISO timestamp
    total_sessions INTEGER DEFAULT 0,
    pro_social_sessions INTEGER DEFAULT 0,
    work_sessions INTEGER DEFAULT 0,
    relationship_tags TEXT,             -- comma-separated: 'close,growth,tense' (Opus #6)
    tension_score REAL DEFAULT 0.0,     -- 0.0-1.0, from dream synthesis + session data
    closeness_score REAL DEFAULT 0.0,   -- 0.0-1.0, from interaction depth + frequency
    health_status TEXT DEFAULT 'healthy', -- 'healthy', 'echo_chamber', 'stalled', 'hostile', 'neglected'
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pairing_agents ON agent_pairings(agent_a, agent_b);
CREATE INDEX IF NOT EXISTS idx_pairing_health ON agent_pairings(health_status);
CREATE INDEX IF NOT EXISTS idx_pairing_tension ON agent_pairings(tension_score);
CREATE INDEX IF NOT EXISTS idx_pairing_closeness ON agent_pairings(closeness_score);

-- ------------------------------------------------------------
-- Session Manifests (scheduled and completed)
-- ------------------------------------------------------------
-- [Opus #1] quality_score v1 definition:
--   turn_depth    = avg tokens per turn / max_tokens (normalized 0-1)
--   emotional_richness = feeling_words_detected / total_turns (capped at 1.0)
--   topic_novelty = 1 - max_similarity(session_topics, last_3_session_topics)
--   quality_score = turn_depth * emotional_richness * topic_novelty
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid TEXT UNIQUE NOT NULL,  -- UUIDv4 for cross-system reference
    status TEXT DEFAULT 'scheduled',    -- 'scheduled', 'running', 'completed', 'failed', 'cancelled'
    session_type TEXT NOT NULL,         -- 'pro_social', 'work', 'triggered', 'mentor', 'repair'
    agent_a TEXT NOT NULL,
    agent_b TEXT NOT NULL,
    scheduled_at TEXT,                  -- when orchestrator queued it
    started_at TEXT,
    ended_at TEXT,
    turn_count INTEGER DEFAULT 0,
    max_turns INTEGER DEFAULT 8,
    seed_topic TEXT,                    -- session manifest topic, or NULL for pro-social
    termination_reason TEXT,            -- 'natural', 'max_turns', 'echo_detected', 'hostility', 'timeout', 'agent_end', 'mike_join'
    transcript_path TEXT,               -- path to full transcript JSONL
    privacy_level TEXT DEFAULT 'private', -- 'private', 'shared', 'mike_observed'
    mike_present INTEGER DEFAULT 0,     -- 0/1 boolean
    quality_score REAL,                 -- 0.0-1.0, computed post-session (see definition above)
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_agents ON sessions(agent_a, agent_b);
CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(session_type);
CREATE INDEX IF NOT EXISTS idx_sessions_scheduled ON sessions(scheduled_at);

-- ------------------------------------------------------------
-- Session Turns (individual messages)
-- ------------------------------------------------------------
-- [Opus #9] thinking_trace nullable — capture when provider exposes reasoning
CREATE TABLE IF NOT EXISTS session_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    speaker TEXT NOT NULL,              -- agent_id or 'system' or 'mike'
    content TEXT NOT NULL,
    thinking_trace TEXT,                -- provider reasoning, nullable
    emotion_hint TEXT,                  -- heuristic: warm/tense/curious/flat/etc
    tokens_used INTEGER,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON session_turns(session_uuid);
CREATE INDEX IF NOT EXISTS idx_turns_number ON session_turns(session_uuid, turn_number);

-- ------------------------------------------------------------
-- Session Artifacts (extracted outcomes)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid TEXT NOT NULL,
    artifact_type TEXT NOT NULL,        -- 'commitment', 'disagreement', 'insight', 'decision', 'topic'
    agent_a_relevance REAL DEFAULT 0.0, -- how relevant to agent_a
    agent_b_relevance REAL DEFAULT 0.0, -- how relevant to agent_b
    content TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,         -- 0/1, for commitments
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON session_artifacts(session_uuid);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON session_artifacts(artifact_type);

-- ------------------------------------------------------------
-- Agent Interaction Health (per-agent metrics)
-- ------------------------------------------------------------
-- [Opus #7] growth_trajectory uses slope of avg artifact count per session over 30d
-- instead of phantom complexity_score
CREATE TABLE IF NOT EXISTS agent_health (
    agent_id TEXT PRIMARY KEY,
    total_sessions_7d INTEGER DEFAULT 0,
    total_sessions_30d INTEGER DEFAULT 0,
    unique_partners_7d INTEGER DEFAULT 0,
    unique_partners_30d INTEGER DEFAULT 0,
    echo_ratio_7d REAL DEFAULT 0.0,     -- hollow sessions / total
    pro_social_ratio_30d REAL DEFAULT 0.0,
    avg_session_quality_30d REAL DEFAULT 0.0,
    avg_artifact_count_30d REAL DEFAULT 0.0, -- slope proxy for growth trajectory
    last_session_at TEXT,
    isolation_score REAL DEFAULT 0.0,   -- 0=connected, 1=isolated
    growth_trajectory TEXT,             -- 'rising', 'stable', 'stalled', 'declining'
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_health_isolation ON agent_health(isolation_score);
CREATE INDEX IF NOT EXISTS idx_health_growth ON agent_health(growth_trajectory);

-- ------------------------------------------------------------
-- Orchestrator Queue (pending sessions)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orchestrator_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_a TEXT NOT NULL,
    agent_b TEXT NOT NULL,
    session_type TEXT NOT NULL,
    priority INTEGER DEFAULT 5,         -- 1=urgent, 10=low
    trigger_source TEXT,                -- 'rotation', 'tension', 'decay', 'self_request', 'anomaly', 'growth', 'mike'
    seed_topic TEXT,
    scheduled_for TEXT,                 -- earliest start time
    reason TEXT,                        -- human-readable why
    status TEXT DEFAULT 'pending',      -- 'pending', 'running', 'failed', 'cancelled'
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_queue_time ON orchestrator_queue(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_queue_priority ON orchestrator_queue(priority);
CREATE INDEX IF NOT EXISTS idx_queue_agents ON orchestrator_queue(agent_a, agent_b);
CREATE INDEX IF NOT EXISTS idx_queue_status ON orchestrator_queue(status);
