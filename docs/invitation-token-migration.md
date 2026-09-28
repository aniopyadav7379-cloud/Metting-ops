# Meeting invitation token migration

Revision `053_invitation_hashes_delivery` introduces SHA-256 invitation
digests and delivery lifecycle fields while preserving existing UUID links
during a bounded transition.

## Rollout

1. Back up the database and record `alembic current`.
2. Apply `alembic upgrade head`. In this integrated release the ordered tail is
   `053_invitation_hashes_delivery` → `054_project_ops_action_lifecycle` →
   `055_federation_summary_approval`. Revision 053 hashes every existing UUID
   but deliberately leaves the legacy column populated. All three migrations
   are additive, so the old application can still run at this point.
3. Verify there are no missing hashes:

   ```sql
   SELECT count(*)
   FROM session_collaborators
   WHERE token IS NOT NULL AND token_hash IS NULL;
   ```

   The result must be zero.
4. Deploy the hash-aware application with
   `MEETING_INVITE_V2_ISSUANCE_ENABLED=false` on every API instance. Existing
   v1 redemption remains available, but external create, resend, and email-link
   minting fail with a controlled unavailable response. No v2 secret is minted
   during mixed-version service.
5. Drain every pre-053 API instance before enabling issuance. Do not use a
   rolling flag flip while an old application can still receive traffic.
6. Set `MEETING_INVITE_V2_ISSUANCE_ENABLED=true` only on the fully hash-aware
   fleet. New v2 invitation secrets are never stored: the required legacy
   `token` column receives an unrelated random compatibility UUID, while only
   the real secret's digest is persisted.
7. Confirm an existing v1 link still resolves through its hash and a newly
   created v2 row cannot be redeemed with either its digest or compatibility
   UUID.

### Enable order

For single-instance staging, upgrade through the integrated head, stop the old
API, start the new API with issuance off, verify v1 redemption, then restart
that one API with issuance enabled and exercise one controlled v2 invitation.
There must never be an old API process alongside the enabled process.

For production blue/green, apply 053 first, bring up the green hash-aware
fleet with issuance off, move traffic, and drain/stop every blue API instance.
Verify the old revision has zero serving processes before enabling issuance on
green. New green instances may be restarted with the flag on only after that
drain gate. Workers do not issue invitation secrets and do not satisfy the API
drain check.

Existing UUID links remain usable through their hashes until
`2026-10-31T00:00:00Z`. `MEETING_INVITE_LEGACY_TOKEN_CUTOFF` may shorten but
cannot extend that deadline. That date is the cutoff for accepting the legacy
v1 link format/transport, not a reason to retain plaintext in the database.
Resending a legacy invitation rotates it to a v2 secret immediately. Before
the cutoff, query active legacy rows with:

```sql
SELECT count(*)
FROM session_collaborators
WHERE token_version = 1
  AND revoked_at IS NULL
  AND accepted_at IS NULL
  AND (expires_at IS NULL OR expires_at > now());
```

Issue fresh invitations for any rows that must survive the transport cutoff.

Plaintext removal has a separate, much shorter gate: after every application
instance is running the hash-aware release and the backup is verified,
separately approve `backend/scripts/scrub_legacy_invitation_tokens.sql`. First
verify complete hash coverage (the v1 equality clause proves the migrated UUID
was hashed; v2 compatibility UUIDs deliberately do not equal their digest):

```sql
SELECT count(*)
FROM session_collaborators
WHERE token IS NOT NULL
  AND (
    token_hash IS NULL
    OR token_hash !~ '^[0-9a-f]{64}$'
    OR (
      token_version = 1
      AND token_hash <> encode(digest(token::text, 'sha256'), 'hex')
    )
  );
```

The result must be zero. The script then makes `token` nullable and clears
existing legacy/compatibility UUIDs. It does **not** invalidate a valid v1
invite: the hash-aware application continues to hash the presented UUID and
match `token_hash` until the v1 transport cutoff. The script is intentionally
not an Alembic head revision, so an ordinary upgrade cannot scrub plaintext
early.

## Rollback

Before the separately approved scrub, the integrated release can be downgraded
to `052_beta_invite_codes_emailed_at` and returned to the old application;
original v1 UUIDs are still present. That downgrade also removes the additive
054 Project-Ops lifecycle fields and 055 federation-approval fields, so export
or explicitly accept losing data written to those fields before proceeding.
Any v2 invitations created by the new application use unrelated compatibility
UUIDs. Before an application rollback, turn issuance off and check for active
v2 bearer grants:

```sql
SELECT id, session_id, user_id, delivery_state
FROM session_collaborators
WHERE token_version = 2
  AND token_hash IS NOT NULL
  AND revoked_at IS NULL
  AND (expires_at IS NULL OR expires_at > now());
```

The result must be reviewed. Revoke those v2 invitations and explicitly
reissue required access through the rollback-compatible application; never
send replacements automatically. A v2 compatibility UUID is not the bearer
secret and must not be substituted into a replacement link. Existing direct
user grants and workspace memberships remain intact.

```bash
cd backend
alembic downgrade 052_beta_invite_codes_emailed_at
```

After the scrub script, plaintext destruction is intentionally irreversible.
Do not roll back to a plaintext-token application: restore the verified
pre-scrub database backup only if that rollback is explicitly approved. The
hash-aware application can still redeem valid v1 UUIDs by hash until the
transport cutoff; after that, explicitly reissue required links. Do not send
replacements automatically. Keep live delivery disabled until an operator
approves the rotation and provider configuration.
