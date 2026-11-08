-- 004_video_episode.sql
-- Stable Jellyfin episode number per video: int(MMDD + per-channel/per-day counter),
-- e.g. 15 March, first video of the day -> 31501. Assigned once on the first
-- successful download and never renumbered.

ALTER TABLE videos ADD COLUMN episode INTEGER;
