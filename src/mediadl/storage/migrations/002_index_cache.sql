CREATE TABLE source_media (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    position INTEGER,
    is_present INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, media_item_id),
    CHECK (position IS NULL OR position >= 0),
    CHECK (is_present IN (0, 1))
);

CREATE INDEX idx_source_media_source_position
    ON source_media(source_id, is_present, position);
CREATE INDEX idx_source_media_media
    ON source_media(media_item_id);

CREATE TABLE source_scans (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    complete_scan INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    reported_count INTEGER,
    skipped_entries INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    metadata_updated_count INTEGER NOT NULL DEFAULT 0,
    stats_updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    CHECK (complete_scan IN (0, 1)),
    CHECK (item_count >= 0),
    CHECK (reported_count IS NULL OR reported_count >= 0),
    CHECK (skipped_entries >= 0),
    CHECK (added_count >= 0),
    CHECK (metadata_updated_count >= 0),
    CHECK (stats_updated_count >= 0),
    CHECK (unchanged_count >= 0),
    CHECK (missing_count >= 0)
);

CREATE INDEX idx_source_scans_source_time
    ON source_scans(source_id, scanned_at DESC);

CREATE TABLE metadata_refreshes (
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    group_name TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    PRIMARY KEY (media_item_id, group_name)
);

CREATE INDEX idx_metadata_refreshes_expiry
    ON metadata_refreshes(group_name, expires_at);
