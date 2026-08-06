-- Migration: 006_checkpoint_file_links_string_ids.sql
-- Purpose: Preserve LangGraph string and UUID checkpoint identifiers.
-- Prerequisite: 004_consolidate_checkpoint_files.sql has created the table.
--
-- SQLite cannot alter a primary-key column type in place, so rebuild the
-- table and cast legacy integer identifiers to TEXT. Run this migration
-- explicitly against existing databases before starting the upgraded app.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE checkpoint_file_links_new (
    checkpoint_id VARCHAR(128) PRIMARY KEY,
    commit_id VARCHAR(64) NOT NULL,
    linked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_count INTEGER NOT NULL,
    total_size_bytes BIGINT NOT NULL,
    FOREIGN KEY (commit_id)
        REFERENCES file_commits(commit_id)
        ON DELETE CASCADE
);

INSERT INTO checkpoint_file_links_new (
    checkpoint_id,
    commit_id,
    linked_at,
    file_count,
    total_size_bytes
)
SELECT
    CAST(checkpoint_id AS TEXT),
    commit_id,
    linked_at,
    file_count,
    total_size_bytes
FROM checkpoint_file_links;

-- Fail closed before replacing the source table. A duplicate sentinel uses
-- SQLite's ROLLBACK conflict policy so any orphan rolls back this transaction,
-- including creation and population of checkpoint_file_links_new.
CREATE TEMP TABLE checkpoint_file_links_migration_guard (
    sentinel INTEGER PRIMARY KEY ON CONFLICT ROLLBACK
);
INSERT INTO checkpoint_file_links_migration_guard (sentinel) VALUES (1);
INSERT INTO checkpoint_file_links_migration_guard (sentinel)
SELECT 1
FROM pragma_foreign_key_check('checkpoint_file_links_new')
LIMIT 1;
DROP TABLE checkpoint_file_links_migration_guard;

DROP TABLE checkpoint_file_links;
ALTER TABLE checkpoint_file_links_new RENAME TO checkpoint_file_links;

CREATE INDEX ix_checkpoint_file_links_commit_id
    ON checkpoint_file_links(commit_id);

COMMIT;
PRAGMA foreign_keys = ON;

-- Verification:
-- SELECT checkpoint_id, typeof(checkpoint_id) FROM checkpoint_file_links;
