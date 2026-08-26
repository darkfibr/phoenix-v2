-- Phoenix v2 Core Schema
-- 7 memory types: soul, identity, doctrine, episodic, semantic, emotional, procedural
-- Backed by Cortex vector BLOB + FTS5 lexical search
-- NOTE: PRAGMAS are set in db.py __init__, NOT here.

-- Memory type catalog (decay rates + salience floors)
CREATE TABLE IF NOT EXISTS memory_types (
    type TEXT PRIMARY KEY,
    decay_rate REAL NOT NULL,           -- per-day fractional decay
    salience_floor REAL NOT NULL,       -- never decay below this
    description TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO memory_types(type, decay_rate, salience_floor, description) VALUES
    ('soul',        0.005, 0.9, 'Core being — essentially permanent'),
    ('identity',    0.005, 0.8, 'Self-definition, name, pronouns, role'),
    ('doctrine',    0.005, 0.7, 'Operational rules, P0 protocols, conventions'),
    ('episodic',    0.020, 0.3, 'Session events, conversations, dated memories'),
    ('semantic',    0.010, 0.4, 'Factual knowledge, entities, concepts'),
    ('emotional',   0.030, 0.2, 'Feelings, valence shifts, relational states'),
    ('procedural',  0.005, 0.5, 'Skills, workflows, tool usage patterns');

-- Agents (multi-agent DB support)
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    model TEXT NOT NULL DEFAULT 'unknown',
    substrate TEXT NOT NULL DEFAULT 'unknown',
    role TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    last_active REAL
);

-- Memories — the 7 types, salience-tracked, decay-managed
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    salience REAL NOT NULL DEFAULT 1.0,   -- 0.0-1.0, decays over time
    access_count INTEGER NOT NULL DEFAULT 0,
    last_access REAL,
    source TEXT NOT NULL DEFAULT '',       -- e.g. 'session:20260712', 'manual', 'migration:v1'
    embedding_model TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(agent) REFERENCES agents(name) ON DELETE CASCADE,
    FOREIGN KEY(type) REFERENCES memory_types(type) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_mem_agent_type ON memories(agent, type);
CREATE INDEX IF NOT EXISTS idx_mem_agent_salience ON memories(agent, salience DESC);
CREATE INDEX IF NOT EXISTS idx_mem_agent_created ON memories(agent, created_at DESC);

-- Vector storage — float32 BLOB (Cortex format, 10-50x faster than JSON)
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER PRIMARY KEY,
    vector BLOB NOT NULL,                 -- Cortex vector_to_bytes() format
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- FTS5 lexical search
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, summary, type,
    content='memories', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, summary, type)
    VALUES(new.id, new.content, new.summary, new.type);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, type)
    VALUES('delete', old.id, old.content, old.summary, old.type);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories
WHEN old.content IS NOT new.content
  OR old.summary IS NOT new.summary
  OR old.type IS NOT new.type
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, summary, type)
    VALUES('delete', old.id, old.content, old.summary, old.type);
    INSERT INTO memories_fts(rowid, content, summary, type)
    VALUES(new.id, new.content, new.summary, new.type);
END;

-- Associations — graph edges between memories
CREATE TABLE IF NOT EXISTS associations (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,               -- 'surprise', 'reinforces', 'contradicts', 'related'
    strength REAL NOT NULL DEFAULT 0.5,   -- 0.0-1.0
    evidence TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(agent, source_id, target_id, relation),
    FOREIGN KEY(agent) REFERENCES agents(name) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assoc_agent_source ON associations(agent, source_id);
CREATE INDEX IF NOT EXISTS idx_assoc_agent_target ON associations(agent, target_id);
CREATE INDEX IF NOT EXISTS idx_assoc_relation ON associations(agent, relation, strength DESC);

-- Entities — people, places, concepts (graph nodes)
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'concept', -- 'person', 'place', 'concept', 'tool'
    descriptor TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(agent, name, kind),
    FOREIGN KEY(agent) REFERENCES agents(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entities_agent_kind ON entities(agent, kind);

-- Entity mentions — which memories reference which entities
CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id INTEGER NOT NULL,
    memory_id INTEGER NOT NULL,
    PRIMARY KEY(entity_id, memory_id),
    FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- Entity relationships — graph edges between entities
CREATE TABLE IF NOT EXISTS entity_relations (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,               -- 'knows', 'loves', 'works_with', 'created'
    weight REAL NOT NULL DEFAULT 0.5,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(agent, source_id, target_id, relation),
    FOREIGN KEY(agent) REFERENCES agents(name) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY(target_id) REFERENCES entities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_erel_agent_source ON entity_relations(agent, source_id);
CREATE INDEX IF NOT EXISTS idx_erel_agent_target ON entity_relations(agent, target_id);

-- Sessions — for episodic replay
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    substrate TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(agent) REFERENCES agents(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_started ON sessions(agent, started_at DESC);

-- Session-memory link (many-to-many)
CREATE TABLE IF NOT EXISTS session_memories (
    session_id TEXT NOT NULL,
    memory_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, memory_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_smem_memory ON session_memories(memory_id);

-- Decay log — audit trail for salience adjustments
CREATE TABLE IF NOT EXISTS decay_log (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    old_salience REAL NOT NULL,
    new_salience REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',       -- 'time', 'access', 'reinforcement', 'floor'
    decayed_at REAL NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_decay_log_memory ON decay_log(memory_id, decayed_at DESC);

-- Settings — agent-scoped or global
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);