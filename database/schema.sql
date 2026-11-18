CREATE TABLE IF NOT EXISTS server_config (
    guild_id INTEGER PRIMARY KEY,
    setup_completed INTEGER NOT NULL DEFAULT 0
        CHECK (setup_completed IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discord_resources (
    resource_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    resource_type TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    parent_category_id INTEGER,
    UNIQUE (guild_id, resource_type, resource_name),
    FOREIGN KEY (guild_id) REFERENCES server_config (guild_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_discord_resources_guild
    ON discord_resources (guild_id);

-- Additive Phase 2 table: existing Phase 1 server/resource data is preserved.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'notified', 'completed', 'cancelled')),
    notified_at TEXT,
    discord_message_id INTEGER,
    class_name TEXT,
    notification_claimed_at TEXT,
    notification_attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reminders_due_pending
    ON reminders (status, due_at, notification_claimed_at);

CREATE INDEX IF NOT EXISTS idx_reminders_discord_message
    ON reminders (discord_message_id);

-- Additive Phase 3 tables: existing server, resource, and reminder data stays intact.
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    class_name TEXT,
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'completed', 'archived')),
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high')),
    position INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_todos_status_priority_due
    ON todos (status, priority, due_at, created_at);

CREATE INDEX IF NOT EXISTS idx_todos_class_name
    ON todos (class_name);

CREATE TABLE IF NOT EXISTS todo_panels (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    page INTEGER NOT NULL DEFAULT 0 CHECK (page >= 0),
    panel_filter TEXT NOT NULL DEFAULT 'open'
        CHECK (panel_filter IN ('open', 'completed', 'archived', 'all')),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (guild_id) REFERENCES server_config (guild_id)
        ON DELETE CASCADE
);

-- Additive Phase 4 knowledge tables; prior reminders, tasks, and panels remain intact.
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    content TEXT NOT NULL,
    class_name TEXT,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_notes_recent
    ON notes (is_archived, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_notes_class
    ON notes (class_name, is_archived);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    extracted_text TEXT,
    class_name TEXT,
    tags TEXT,
    saved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN ('pending', 'success', 'failed')),
    discord_message_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_links_recent
    ON links (is_archived, saved_at DESC);

CREATE INDEX IF NOT EXISTS idx_links_normalized_url
    ON links (normalized_url);

CREATE INDEX IF NOT EXISTS idx_links_class
    ON links (class_name, is_archived);

-- Additive Phase 5 class/channel mapping; all existing knowledge remains intact.
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    class_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    channel_id INTEGER,
    category_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    archived_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (guild_id, normalized_name),
    FOREIGN KEY (guild_id) REFERENCES server_config (guild_id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_channel
    ON classes (guild_id, channel_id)
    WHERE channel_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_classes_status_name
    ON classes (guild_id, status, normalized_name);

CREATE TABLE IF NOT EXISTS digest_runs (
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('daily', 'weekly')),
    slot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent')),
    claimed_at TEXT,
    sent_at TEXT,
    discord_message_id INTEGER,
    PRIMARY KEY (guild_id, kind, slot)
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_context
    ON conversation_history (guild_id, channel_id, owner_id, id DESC);

CREATE TABLE IF NOT EXISTS assistant_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'executing', 'completed', 'cancelled', 'failed')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    discord_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_assistant_actions_pending
    ON assistant_actions (status, expires_at);
