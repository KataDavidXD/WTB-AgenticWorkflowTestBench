-- Migration: 006_checkpoint_file_links_string_ids_postgresql.sql
-- Dialect: PostgreSQL only. Do not execute on SQLite.
-- Prerequisite: 004_consolidate_checkpoint_files.sql

BEGIN;

ALTER TABLE checkpoint_file_links
    ALTER COLUMN checkpoint_id TYPE VARCHAR(128)
    USING checkpoint_id::text;

COMMIT;

-- Verification:
-- checkpoint_file_links.checkpoint_id must report character varying(128).
