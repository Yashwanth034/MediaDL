ALTER TABLE job_items ADD COLUMN retry_after_at TEXT;
ALTER TABLE job_items ADD COLUMN completed_at TEXT;

CREATE INDEX idx_job_items_retry_due
    ON job_items(job_id, status, retry_after_at, position);
