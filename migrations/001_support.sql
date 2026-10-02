CREATE TABLE users (
    user_id TEXT PRIMARY KEY CHECK (length(user_id) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_demo BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE support_sessions (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    agent_id TEXT NOT NULL CHECK (agent_id = 'support-agent'),
    title TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, user_id)
);
CREATE INDEX support_sessions_user_activity ON support_sessions (user_id, agent_id, updated_at DESC);
CREATE TABLE tickets (
    ticket_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    thread_id TEXT,
    source_thread_id TEXT NOT NULL,
    draft_id TEXT NOT NULL UNIQUE,
    draft_version INTEGER NOT NULL CHECK (draft_version > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    draft JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state = 'open'),
    created_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('postgres', 'sqlite_import')),
    FOREIGN KEY (thread_id, user_id) REFERENCES support_sessions(thread_id, user_id),
    CHECK ((source = 'postgres' AND thread_id IS NOT NULL) OR (source = 'sqlite_import' AND thread_id IS NULL))
);
CREATE INDEX tickets_user_created ON tickets (user_id, created_at DESC);
CREATE INDEX tickets_session ON tickets (thread_id);
