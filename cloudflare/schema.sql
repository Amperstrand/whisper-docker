-- whisper-docker: D1 database schema
-- Applied via: wrangler d1 execute <db-name> --file=cloudflare/schema.sql

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
    original_filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    file_type TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    worker_id TEXT,
    error_message TEXT,
    options TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
