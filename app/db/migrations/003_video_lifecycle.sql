-- 003_video_lifecycle.sql
-- Video lifecycle: videos.status is one of
--   'completed' (files on disk, shown in UI, subject to retention),
--   'deleted'   (tombstone: files removed, row kept so the poller never re-downloads),
--   'failed'    (download failed; see attempts / last_error / next_attempt_at),
--   'skipped'   (deliberately not downloaded).
-- next_attempt_at NULL on a failed row means: no automatic retry.

UPDATE videos SET status = 'completed'
WHERE status NOT IN ('completed', 'deleted', 'failed', 'skipped');

ALTER TABLE videos ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN last_error TEXT;
ALTER TABLE videos ADD COLUMN next_attempt_at TEXT;

CREATE INDEX IF NOT EXISTS idx_videos_channel_status ON videos(channel_id, status);
