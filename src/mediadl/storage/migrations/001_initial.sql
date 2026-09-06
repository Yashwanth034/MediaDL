CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, source_type, source_key)
);

CREATE TABLE media_items (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    media_key TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    channel TEXT,
    channel_id TEXT,
    upload_date TEXT,
    duration_seconds REAL,
    media_type TEXT NOT NULL DEFAULT 'video',
    availability TEXT NOT NULL DEFAULT 'unknown',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, media_key)
);

CREATE INDEX idx_media_items_source_id ON media_items(source_id);
CREATE INDEX idx_media_items_upload_date ON media_items(upload_date);
CREATE INDEX idx_media_items_duration ON media_items(duration_seconds);

CREATE TABLE media_stats (
    media_item_id INTEGER PRIMARY KEY REFERENCES media_items(id) ON DELETE CASCADE,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (view_count IS NULL OR view_count >= 0),
    CHECK (like_count IS NULL OR like_count >= 0),
    CHECK (comment_count IS NULL OR comment_count >= 0)
);

CREATE INDEX idx_media_stats_views ON media_stats(view_count);
CREATE INDEX idx_media_stats_likes ON media_stats(like_count);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    selection_json TEXT NOT NULL DEFAULT '{}',
    plan_json TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE INDEX idx_jobs_status_updated ON jobs(status, updated_at);

CREATE TABLE job_items (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    output_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id, media_item_id),
    UNIQUE (job_id, position),
    CHECK (position >= 0),
    CHECK (attempts >= 0)
);

CREATE INDEX idx_job_items_job_status ON job_items(job_id, status, position);

CREATE TABLE downloads (
    id INTEGER PRIMARY KEY,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE RESTRICT,
    job_item_id INTEGER REFERENCES job_items(id) ON DELETE SET NULL,
    format TEXT NOT NULL,
    quality TEXT NOT NULL,
    output_path TEXT,
    source_archive_key TEXT,
    file_size INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    CHECK (file_size IS NULL OR file_size >= 0)
);

CREATE INDEX idx_downloads_media_status ON downloads(media_item_id, status);
CREATE UNIQUE INDEX idx_downloads_archive_key
    ON downloads(source_archive_key)
    WHERE source_archive_key IS NOT NULL AND status = 'completed';

CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    download_id INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    normalized_media_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (size_bytes >= 0)
);

CREATE INDEX idx_files_sha256 ON files(sha256);
CREATE INDEX idx_files_media_hash ON files(normalized_media_hash);

CREATE TABLE fingerprints (
    id INTEGER PRIMARY KEY,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'full',
    value TEXT NOT NULL,
    duration_seconds REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (media_item_id, kind, algorithm, scope),
    CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
);

CREATE INDEX idx_fingerprints_lookup ON fingerprints(kind, algorithm, value);

CREATE TABLE duplicate_groups (
    id INTEGER PRIMARY KEY,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE TABLE duplicate_members (
    group_id INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'candidate',
    PRIMARY KEY (group_id, media_item_id)
);

CREATE INDEX idx_duplicate_members_media ON duplicate_members(media_item_id);

CREATE TABLE failures (
    id INTEGER PRIMARY KEY,
    job_item_id INTEGER NOT NULL REFERENCES job_items(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    CHECK (retryable IN (0, 1))
);

CREATE INDEX idx_failures_unresolved ON failures(job_item_id, resolved_at);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
