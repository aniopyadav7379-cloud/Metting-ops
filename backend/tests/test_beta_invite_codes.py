"""Phase-1 beta invite-code tests.

Mirrors the fixture style of test_stripe_webhook.py: SessionLocal helpers,
the `client` fixture, monkeypatch of env / auth_config.

Covers (per the build spec):
  - REQUIRE_INVITE_CODE on + valid code -> 200, the new user is comped on BOTH
    surfaces: user.tier=='pro' (time-limited) AND their PERSONAL org
    (plan=='pro', max_monthly_hours is None); the code is consumed (is_active
    False, redemption_count 1, redeemed_by/at stamped).
  - No code when required -> 403.
  - Unknown code -> 403; exhausted code -> 403.
  - Double-redeem of a single-use code -> 2nd registration 403.
  - REQUIRE_INVITE_CODE OFF + NO code -> free public signup (no comp).
  - REQUIRE_INVITE_CODE OFF + a valid OPTIONAL code -> still comped (user tier +
    org plan) and the code is consumed.
  - Admin mint -> N codes; non-admin mint -> 403.
  - /mine lists the caller's codes (and only the caller's).
  - /config reflects REQUIRE_INVITE_CODE.
"""
from __future__ import annotations

import uuid

import pytest


def _models():
    from auth.models import BetaInviteCode, Organization, User, UserOrganization
    from database.database import SessionLocal
    return BetaInviteCode, Organization, User, UserOrganization, SessionLocal


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registration_env(monkeypatch):
    """Self-serve register needs ALLOW_REGISTRATION on (no admin caller).
    REQUIRE_INVITE_CODE defaults OFF; individual tests flip it on. Both are
    set on auth_config (read at call time) so we don't depend on the
    import-time env binding. Email sends are stubbed to no-op."""
    from auth.config import auth_config
    from auth import routes as routes_mod

    monkeypatch.setattr(auth_config, "ALLOW_REGISTRATION", True)
    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)
    monkeypatch.setattr(
        routes_mod, "send_verification_email", lambda to, url: True
    )
    yield


def _uname() -> str:
    return "inv" + uuid.uuid4().hex[:8]


def _login_admin(client) -> str:
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    return r.json()["access_token"]


def _admin_headers(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login_admin(client)}"}


def _seed_code(
    *,
    created_by_user_id: int | None = None,
    max_redemptions: int = 1,
    redemption_count: int = 0,
    is_active: bool = True,
    note: str | None = None,
) -> str:
    """Insert a code row directly and return its `code`."""
    BetaInviteCode, _O, _U, _UO, SessionLocal = _models()
    db = SessionLocal()
    try:
        code = "SEED" + uuid.uuid4().hex[:8].upper()
        row = BetaInviteCode(
            code=code,
            created_by_user_id=created_by_user_id,
            max_redemptions=max_redemptions,
            redemption_count=redemption_count,
            is_active=is_active,
            note=note,
        )
        db.add(row)
        db.commit()
        return code
    finally:
        db.close()


def _get_code_row(code: str):
    BetaInviteCode, _O, _U, _UO, SessionLocal = _models()
    db = SessionLocal()
    try:
        return db.query(BetaInviteCode).filter(BetaInviteCode.code == code).first()
    finally:
        db.close()


def _personal_org_for(username: str):
    """Resolve the {username}-personal org row (post-register)."""
    _C, Organization, _U, _UO, SessionLocal = _models()
    db = SessionLocal()
    try:
        return (
            db.query(Organization)
            .filter(Organization.slug == f"{username}-personal")
            .first()
        )
    finally:
        db.close()


def _register(client, *, username: str, invite_code: str | None = None):
    body = {
        "email": f"{username}@example.com",
        "username": username,
        # Unique full_name per user: create_user derives the personal-org
        # *name* from full_name (the slug is per-username), and
        # organizations.name is UNIQUE — a shared name would collide across
        # registrations in the same test DB.
        "password": "Password123",
        "full_name": f"Invitee {username}",
    }
    if invite_code is not None:
        body["invite_code"] = invite_code
    return client.post("/api/auth/register", json=body)


def _create_nonadmin_user(client) -> tuple[int, dict[str, str]]:
    """Seed a genuinely non-admin user (role 'user' in the SHARED default
    org, no personal org) and return (user_id, auth headers).

    NOTE: a self-serve-registered user is admin of their OWN personal org, and
    require_admin resolves the role from the active org, so such a user passes
    require_admin for their own workspace. To exercise the non-admin 403 we
    need a member whose active-org role is NOT admin — hence the shared-default
    membership with role='user'. (See risks: 'admin' is org-scoped here.)"""
    _C, _O, _U, UserOrganization, SessionLocal = _models()
    from auth.service import AuthService

    uname = _uname()
    db = SessionLocal()
    try:
        user = AuthService.create_user(
            db,
            email=f"{uname}@example.com",
            username=uname,
            password="Password123",
            full_name=f"Regular {uname}",
            personal_org=False,  # lands in the shared default org as role 'user'
        )
        uid = user.id
        # Sanity: this user must NOT be an admin of any org.
        assert (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == uid,
                UserOrganization.role == "admin",
            )
            .first()
            is None
        )
    finally:
        db.close()

    tok = client.post(
        "/api/auth/login",
        data={"username": uname, "password": "Password123"},
    ).json()["access_token"]
    return uid, {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# 1. /config + OFF-path behavior
# ---------------------------------------------------------------------------


def test_config_reflects_require_invite_code(client, monkeypatch):
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)
    r = client.get("/api/invite-codes/config")
    assert r.status_code == 200
    assert r.json() == {"require_invite_code": False}

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    r = client.get("/api/invite-codes/config")
    assert r.status_code == 200
    assert r.json() == {"require_invite_code": True}


def test_config_is_anonymous(client):
    """No Authorization header -> still 200 (anonymous-callable)."""
    r = client.get("/api/invite-codes/config")
    assert r.status_code == 200
    assert "require_invite_code" in r.json()


def test_register_off_path_works_without_code_and_does_not_comp(client, monkeypatch):
    """REQUIRE_INVITE_CODE OFF: register with NO code works exactly as before
    and the personal org stays on the free plan (no Pro comp)."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)
    uname = _uname()
    r = _register(client, username=uname)  # no invite_code at all
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "free"

    org = _personal_org_for(uname)
    assert org is not None
    assert (org.plan or "free") == "free"  # NOT comped when the gate is off


def test_register_off_path_with_valid_code_still_comps(client, monkeypatch):
    """REQUIRE_INVITE_CODE OFF: an OPTIONAL code is still honored. A user who
    signs up WITH a valid code gets the time-limited Pro comp on BOTH surfaces
    (user.tier + personal-org plan) and the code is consumed. (A no-code signup
    stays free — see test_register_off_path_works_without_code_and_does_not_comp.)
    """
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)
    code = _seed_code()
    uname = _uname()
    r = _register(client, username=uname, invite_code=code)
    assert r.status_code == 200, r.text
    # USER side: optional code -> time-limited Pro tier.
    assert r.json()["tier"] == "pro"

    # Code consumed (single-use) even though the gate is off.
    row = _get_code_row(code)
    assert row.redemption_count == 1
    assert row.is_active is False
    assert row.redeemed_by_user_id == r.json()["id"]

    # ORG side: personal org comped.
    org = _personal_org_for(uname)
    assert org.plan == "pro"
    assert org.max_monthly_hours is None


def test_register_off_path_invalid_code_is_403(client, monkeypatch):
    """REQUIRE_INVITE_CODE OFF but a NON-EMPTY code is supplied: it's validated
    like the on-path, so a bad optional code 403s (the user learns their promo
    code didn't work rather than silently landing on free)."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)
    r = _register(client, username=_uname(), invite_code="NOPE-DOES-NOT-EXIST")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 2. ON-path: gate + comp + consume
# ---------------------------------------------------------------------------


def test_valid_code_registers_comps_pro_and_consumes(client, monkeypatch):
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    code = _seed_code()
    uname = _uname()

    r = _register(client, username=uname, invite_code=code)
    assert r.status_code == 200, r.text

    # USER side: comped to a TIME-LIMITED Pro tier (both surfaces, not just org).
    assert r.json()["tier"] == "pro"

    # New user's PERSONAL org comped to Pro with the hours override cleared.
    org = _personal_org_for(uname)
    assert org is not None
    assert org.plan == "pro"
    assert org.max_monthly_hours is None

    # Code consumed exactly once + deactivated (single-use).
    row = _get_code_row(code)
    assert row.redemption_count == 1
    assert row.is_active is False
    assert row.redeemed_at is not None
    assert row.redeemed_by_user_id == r.json()["id"]

    # The comp is bounded (time-limited), so the auto-revert can expire it.
    _C, _O, User, _UO, SessionLocal = _models()
    db = SessionLocal()
    try:
        urow = db.query(User).filter(User.id == r.json()["id"]).first()
        assert urow.tier == "pro"
        assert urow.tier_expires_at is not None
    finally:
        db.close()


def test_no_code_when_required_is_403_and_creates_no_user(client, monkeypatch):
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    uname = _uname()
    r = _register(client, username=uname, invite_code=None)
    assert r.status_code == 403
    assert "invite code" in r.json()["detail"].lower()

    # The 403 fired BEFORE create_user -> no user, no personal org.
    _C, _O, User, _UO, SessionLocal = _models()
    db = SessionLocal()
    try:
        assert (
            db.query(User).filter(User.username == uname).first() is None
        )
    finally:
        db.close()
    assert _personal_org_for(uname) is None


def test_empty_string_code_when_required_is_403(client, monkeypatch):
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    r = _register(client, username=_uname(), invite_code="   ")
    assert r.status_code == 403


def test_unknown_code_is_403(client, monkeypatch):
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    r = _register(client, username=_uname(), invite_code="NOPE-DOES-NOT-EXIST")
    assert r.status_code == 403


def test_exhausted_code_is_403(client, monkeypatch):
    """A code already at its redemption cap (and deactivated) -> 403."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    code = _seed_code(max_redemptions=1, redemption_count=1, is_active=False)
    r = _register(client, username=_uname(), invite_code=code)
    assert r.status_code == 403


def test_inactive_code_is_403(client, monkeypatch):
    """Admin-disabled code (is_active False) with redemptions left -> 403."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    code = _seed_code(max_redemptions=5, redemption_count=0, is_active=False)
    r = _register(client, username=_uname(), invite_code=code)
    assert r.status_code == 403


def test_double_redeem_single_use_second_is_403(client, monkeypatch):
    """Single-use code: first register succeeds + comps; the SAME code used
    again -> 403 and the second user is not comped."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    code = _seed_code(max_redemptions=1)

    first = _uname()
    r1 = _register(client, username=first, invite_code=code)
    assert r1.status_code == 200, r1.text
    assert _personal_org_for(first).plan == "pro"

    second = _uname()
    r2 = _register(client, username=second, invite_code=code)
    assert r2.status_code == 403

    # Code consumed exactly once.
    row = _get_code_row(code)
    assert row.redemption_count == 1


def test_multi_use_code_redeems_twice_then_exhausts(client, monkeypatch):
    """A max_redemptions=2 code comps two users, then 403s on the third and
    deactivates only once the cap is reached."""
    from auth.config import auth_config

    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    code = _seed_code(max_redemptions=2)

    u1 = _uname()
    assert _register(client, username=u1, invite_code=code).status_code == 200
    # After first redemption the code is still active (1 < 2).
    assert _get_code_row(code).is_active is True

    u2 = _uname()
    assert _register(client, username=u2, invite_code=code).status_code == 200
    # Now exhausted + deactivated.
    row = _get_code_row(code)
    assert row.redemption_count == 2
    assert row.is_active is False

    u3 = _uname()
    assert _register(client, username=u3, invite_code=code).status_code == 403
    assert _personal_org_for(u1).plan == "pro"
    assert _personal_org_for(u2).plan == "pro"
    assert (_personal_org_for(u3) is None) or (
        (_personal_org_for(u3).plan or "free") == "free"
    )


# ---------------------------------------------------------------------------
# 3. Admin mint + /mine
# ---------------------------------------------------------------------------


def test_admin_mint_returns_n_codes(client):
    headers = _admin_headers(client)
    r = client.post(
        "/api/admin/invite-codes",
        json={"count": 7, "note": "beta wave 1"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    codes = r.json()
    assert len(codes) == 7
    assert all(c["is_active"] for c in codes)
    assert all(c["max_redemptions"] == 1 for c in codes)
    # Codes are distinct.
    assert len({c["code"] for c in codes}) == 7


def test_admin_mint_default_count_is_5(client):
    headers = _admin_headers(client)
    r = client.post("/api/admin/invite-codes", json={}, headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 5


def test_admin_mint_count_over_50_rejected(client):
    headers = _admin_headers(client)
    r = client.post(
        "/api/admin/invite-codes", json={"count": 51}, headers=headers
    )
    # Schema bound (1..50) -> 422 validation error.
    assert r.status_code == 422


def test_non_admin_mint_is_403(client):
    _uid, headers = _create_nonadmin_user(client)
    r = client.post(
        "/api/admin/invite-codes", json={"count": 3}, headers=headers
    )
    assert r.status_code == 403


def test_personal_org_admin_cannot_mint(client):
    """The hole the superuser gate closes: a normal self-serve user is admin of
    their OWN {username}-personal org, so an org-scoped require_admin would let
    them mint their own codes. Minting is PLATFORM-SUPERUSER ONLY, so a
    personal-org admin (not a superuser) is still 403 — preserving Phase-1's
    controlled scarcity (only the platform mints; seed users just share)."""
    _C, _O, _U, UserOrganization, SessionLocal = _models()
    from auth.service import AuthService

    uname = _uname()
    db = SessionLocal()
    try:
        user = AuthService.create_user(
            db,
            email=f"{uname}@example.com",
            username=uname,
            password="Password123",
            full_name=f"Personal {uname}",
            personal_org=True,  # admin of their own personal org, NOT superuser
        )
        assert user.is_superuser is False
        # they genuinely ARE an org admin (of their personal workspace)
        assert (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.role == "admin",
            )
            .first()
            is not None
        )
    finally:
        db.close()

    tok = client.post(
        "/api/auth/login",
        data={"username": uname, "password": "Password123"},
    ).json()["access_token"]
    r = client.post(
        "/api/admin/invite-codes",
        json={"count": 3},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403


def test_mint_requires_auth(client):
    r = client.post("/api/admin/invite-codes", json={"count": 3})
    assert r.status_code == 401


def test_admin_mint_for_user_id_attributes_to_that_user(client):
    """Codes minted with for_user_id show up in THAT user's /mine, not the
    admin's."""
    target_uid, target_headers = _create_nonadmin_user(client)
    admin_headers = _admin_headers(client)

    r = client.post(
        "/api/admin/invite-codes",
        json={"count": 4, "for_user_id": target_uid, "note": "seed user"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    minted = {c["code"] for c in r.json()}

    mine = client.get("/api/invite-codes/mine", headers=target_headers)
    assert mine.status_code == 200
    mine_codes = {item["code"] for item in mine.json()}
    assert minted <= mine_codes
    assert len(mine.json()) == 4


def test_admin_mint_bad_for_user_id_is_400(client):
    headers = _admin_headers(client)
    r = client.post(
        "/api/admin/invite-codes",
        json={"count": 2, "for_user_id": 99999999},
        headers=headers,
    )
    assert r.status_code == 400


def test_mine_lists_only_callers_codes_with_redemption_state(client, monkeypatch):
    """/mine returns the caller's codes in the contract shape and reflects
    redemption (redeemed flag + redeemer email) after a code is used."""
    from auth.config import auth_config

    owner_uid, owner_headers = _create_nonadmin_user(client)
    admin_headers = _admin_headers(client)

    # Mint 2 codes owned by the seed user.
    mint = client.post(
        "/api/admin/invite-codes",
        json={"count": 2, "for_user_id": owner_uid},
        headers=admin_headers,
    )
    assert mint.status_code == 200, mint.text
    codes = [c["code"] for c in mint.json()]

    # Redeem ONE of them via self-serve register (gate on).
    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", True)
    invitee = _uname()
    rr = _register(client, username=invitee, invite_code=codes[0])
    assert rr.status_code == 200, rr.text
    monkeypatch.setattr(auth_config, "REQUIRE_INVITE_CODE", False)

    mine = client.get("/api/invite-codes/mine", headers=owner_headers)
    assert mine.status_code == 200
    by_code = {item["code"]: item for item in mine.json()}
    assert set(codes) <= set(by_code)

    redeemed_item = by_code[codes[0]]
    assert redeemed_item["redeemed"] is True
    assert redeemed_item["is_active"] is False
    assert redeemed_item["redeemed_by_email"] == f"{invitee}@example.com"
    assert redeemed_item["redeemed_at"] is not None

    unused_item = by_code[codes[1]]
    assert unused_item["redeemed"] is False
    assert unused_item["redeemed_by_email"] is None
    assert unused_item["is_active"] is True

    # The admin (a different user) does not see the seed user's codes.
    admin_mine = client.get("/api/invite-codes/mine", headers=admin_headers)
    assert admin_mine.status_code == 200
    admin_codes = {item["code"] for item in admin_mine.json()}
    assert not (set(codes) & admin_codes)


def test_mine_requires_auth(client):
    r = client.get("/api/invite-codes/mine")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 4. Invite-code emailing
# ---------------------------------------------------------------------------


def test_admin_send_invite_code_dry_run_returns_preview_and_sends_nothing(
    client, monkeypatch
):
    headers = _admin_headers(client)
    code = _seed_code(note="cohort=meeting_ops_v1 comp_days=30")
    calls: list[tuple[str, str, str, str]] = []

    def fake_send(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
        calls.append((to_email, subject, html_body, text_body))
        return True

    monkeypatch.setattr("api.invite_codes.send_transactional_email", fake_send)

    resp = client.post(
        "/api/admin/invite-codes/send",
        json={
            "dry_run": True,
            "recipients": [{"email": "preview@example.com", "code": code}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["dry_run"] is True
    assert payload["total"] == 1
    assert payload["sent"] == 0
    assert payload["failed"] == 0
    item = payload["results"][0]
    assert item["status"] == "preview"
    assert item["subject"] == "30 days of Meeting-Ops Pro, on us"
    assert code in item["text_body"]
    assert calls == []

    row = _get_code_row(code)
    assert row.emailed_at is None


def test_admin_send_invite_code_marks_emailed_and_audits(client, monkeypatch):
    headers = _admin_headers(client)
    code = _seed_code(note="cohort=meeting_ops_v1 comp_days=30")
    calls: list[tuple[str, str, str, str]] = []
    audits: list[tuple[int, str, str, str | None, dict | None]] = []

    def fake_send(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
        calls.append((to_email, subject, html_body, text_body))
        return True

    def fake_log_action(db, user_id, action, resource_type=None, resource_id=None, ip_address=None, user_agent=None, details=None, organization_id=None):
        audits.append((user_id, action, resource_type, resource_id, details))

    monkeypatch.setattr("api.invite_codes.send_transactional_email", fake_send)
    monkeypatch.setattr("api.invite_codes.AuthService.log_action", fake_log_action)

    resp = client.post(
        "/api/admin/invite-codes/send",
        json={
            "dry_run": False,
            "recipients": [{"email": "sent@example.com", "code": code}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["dry_run"] is False
    assert payload["sent"] == 1
    assert payload["failed"] == 0
    assert payload["results"][0]["status"] == "sent"
    assert payload["results"][0]["emailed_at"] is not None

    row = _get_code_row(code)
    assert row.emailed_at is not None
    assert len(calls) == 1
    assert calls[0][0] == "sent@example.com"
    assert len(audits) == 1
    assert audits[0][1] == "invite_code_emailed"
    assert audits[0][2] == "invite_code"
    assert audits[0][3] == code
    assert audits[0][4] == {"to": "sent@example.com"}


def test_admin_send_cohort_rejects_overflow(client):
    headers = _admin_headers(client)
    cohort = f"overflow_{uuid.uuid4().hex[:8]}"
    _seed_code(note=f"cohort={cohort} comp_days=30")
    resp = client.post(
        "/api/admin/invite-codes/send-cohort",
        json={
            "dry_run": True,
            "cohort": cohort,
            "recipients": ["one@example.com", "two@example.com"],
        },
        headers=headers,
    )
    assert resp.status_code == 400


def test_admin_send_requires_auth(client):
    resp = client.post(
        "/api/admin/invite-codes/send",
        json={"dry_run": True, "recipients": []},
    )
    assert resp.status_code in (401, 403)
