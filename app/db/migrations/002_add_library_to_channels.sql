-- 002_add_library_to_channels.sql
-- Add library column to channels table with default 'default'

ALTER TABLE channels ADD COLUMN library TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_channels_library ON channels(library);
