-- POST-CUTOFF, APPROVAL-GATED OPERATION ONLY.
--
-- Do not run until every application instance is on the hash-aware release,
-- the database backup is verified, and the hash-coverage query in
-- docs/invitation-token-migration.md returns zero. The v1 UUID links remain
-- redeemable through token_hash after this scrub; 2026-10-31 is a separate
-- cutoff for accepting the legacy link transport.
--
-- The application validates token_hash only. New v2 rows contain unrelated
-- compatibility UUIDs in token, so clearing current values cannot expose or
-- invalidate a v2 secret.

BEGIN;

LOCK TABLE session_collaborators IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM session_collaborators
        WHERE token IS NOT NULL
          AND (
              token_hash IS NULL
              OR token_hash !~ '^[0-9a-f]{64}$'
              -- v1 rows must prove their existing UUID was actually hashed;
              -- v2 compatibility UUIDs intentionally do not match the digest.
              OR (
                  token_version = 1
                  AND token_hash <> encode(digest(token::text, 'sha256'), 'hex')
              )
          )
    ) THEN
        RAISE EXCEPTION
            'meeting invitation token hash coverage is incomplete; do not scrub plaintext';
    END IF;
END
$$;

ALTER TABLE session_collaborators
    ALTER COLUMN token DROP NOT NULL;

UPDATE session_collaborators
SET token = NULL
WHERE token IS NOT NULL;

COMMIT;
