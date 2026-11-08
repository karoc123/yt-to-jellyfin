-- 005_settings_and_libraries.sql
-- Libraries table and default libraries seed

CREATE TABLE IF NOT EXISTS libraries (
    name TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    icon TEXT NOT NULL DEFAULT '📁',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT OR IGNORE INTO libraries (name, label, icon) VALUES
    ('default', 'Standard', '📁'),
    ('kids', 'Kids', '🧒'),
    ('dokus', 'Dokus', '📚'),
    ('tech', 'Tech', '💻');

INSERT OR IGNORE INTO libraries (name, label, icon)
SELECT DISTINCT library, UPPER(SUBSTR(library, 1, 1)) || SUBSTR(library, 2), '📂'
FROM channels
WHERE library IS NOT NULL AND TRIM(library) != '';
