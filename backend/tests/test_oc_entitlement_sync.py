"""Tests for the optional Ops-Center entitlement sync at UC SSO login.

Coverage:
  - dormant by default: no URL means no HTTP and no local mutation
  - paid entitlement + meeting_ops_access upgrades locally
  - free tier never upgrades, even with meeting_ops_access
  - HTTP failures stay fail-open
  - local pro comps are never downgraded
  - the UC SSO callback invokes the sync before minting the session JWT
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

import httpx
import pytest


def _seed_user(db, *, tier: str = "free", org_plan: str = "free"):
    from auth.models import Organization, User, UserOrganization
    from auth.utils import get_password_hash

    suffix = uuid.uuid4().hex[:8]
    org = Organization(
        name=f"Entitlement Test Org {suffix}",
        slug=f"entitlement-test-org-{suffix}-{tier}-{org_plan}",
        is_active=True,
        plan=org_plan,
    )
    user = User(
        email=f"entitlements-{suffix}@example.com",
        username=f"entitlements-{suffix}-{tier}-{org_plan}",
        hashed_password=get_password_hash("unused-password"),
        is_active=True,
        is_verified=True,
        tier=tier,
    )
    db.add_all([org, user])
    db.commit()
    db.add(
        UserOrganization(
            user_id=user.id,
            organization_id=org.id,
            role="admin",
        )
    )
    db.commit()
    db.refresh(user)
    db.refresh(org)
    return user, org


@pytest.mark.asyncio
async def test_dormant_no_http_call_no_local_mutation(app, monkeypatch):
    from auth.oc_entitlements import sync_oc_entitlement_grant
    from database.database import SessionLocal

    monkeypatch.delenv("MEETING_OPS_ENTITLEMENT_URL", raising=False)

    db = SessionLocal()
    try:
        user, org = _seed_user(db, tier="free", org_plan="free")
        http_calls = {"n": 0}

        def boom(*args, **kwargs):
            http_calls["n"] += 1
            raise AssertionError("httpx.AsyncClient should not be created while dormant")

        import auth.oc_entitlements as oc

        monkeypatch.setattr(oc.httpx, "AsyncClient", boom)
        await sync_oc_entitlement_grant(db, user, "kc-access-token")

        db.refresh(user)
        db.refresh(org)
        assert http_calls["n"] == 0
        assert user.tier == "free"
        assert org.plan == "free"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_paid_tier_with_access_grants_pro(app, monkeypatch):
    from auth.oc_entitlements import sync_oc_entitlement_grant
    from database.database import SessionLocal

    monkeypatch.setenv("MEETING_OPS_ENTITLEMENT_URL", "https://oc.test/api/v1/entitlements")
    monkeypatch.setenv("MEETING_OPS_ENTITLEMENT_COMP_DAYS", "41")

    db = SessionLocal()
    try:
        user, org = _seed_user(db, tier="free", org_plan="free")

        async def fake_fetch(access_token):
            assert access_token == "kc-access-token"
            return {
                "org_id": "org-123",
                "tier": "suite",
                "role": "admin",
                "entitlements": ["meeting_ops_access", "other_feature"],
            }

        comp_calls = []

        def fake_comp(db_, user_, *, tier="pro", days=30):
            comp_calls.append({"tier": tier, "days": days, "user_id": user_.id})
            user_.tier = tier
            user_.tier_expires_at = datetime.now(timezone.utc)
            org.plan = "pro"
            org.max_monthly_hours = None
            db_.commit()
            return True

        import auth.oc_entitlements as oc

        monkeypatch.setattr(oc, "fetch_oc_entitlements", fake_fetch)
        monkeypatch.setattr(oc, "comp_personal_org_to_pro", fake_comp)

        await sync_oc_entitlement_grant(db, user, "kc-access-token")
        db.refresh(user)
        db.refresh(org)

        assert comp_calls == [{"tier": "pro", "days": 41, "user_id": user.id}]
        assert user.tier == "pro"
        assert org.plan == "pro"
        assert org.max_monthly_hours is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_free_tier_with_access_does_not_upgrade(app, monkeypatch):
    from auth.oc_entitlements import sync_oc_entitlement_grant
    from database.database import SessionLocal

    monkeypatch.setenv("MEETING_OPS_ENTITLEMENT_URL", "https://oc.test/api/v1/entitlements")

    db = SessionLocal()
    try:
        user, org = _seed_user(db, tier="free", org_plan="free")

        async def fake_fetch(access_token):
            return {
                "org_id": "org-123",
                "tier": "free",
                "role": "user",
                "entitlements": ["meeting_ops_access"],
            }

        called = {"n": 0}

        def fake_comp(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("comp helper must not run for free tier")

        import auth.oc_entitlements as oc

        monkeypatch.setattr(oc, "fetch_oc_entitlements", fake_fetch)
        monkeypatch.setattr(oc, "comp_personal_org_to_pro", fake_comp)

        await sync_oc_entitlement_grant(db, user, "kc-access-token")
        db.refresh(user)
        db.refresh(org)

        assert called["n"] == 0
        assert user.tier == "free"
        assert org.plan == "free"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fetch_http_failures_return_none(monkeypatch):
    import auth.oc_entitlements as oc

    monkeypatch.setenv("MEETING_OPS_ENTITLEMENT_URL", "https://oc.test/api/v1/entitlements")

    class _Client:
        def __init__(self, *args, **kwargs):
            self.requests = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            self.requests.append((url, kwargs))
            request = httpx.Request("GET", url)
            return httpx.Response(
                500,
                request=request,
                content=b'{"error":"boom"}',
            )

    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)

    assert await oc.fetch_oc_entitlements("kc-access-token") is None

    class _TimeoutClient(_Client):
        async def get(self, url, **kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(oc.httpx, "AsyncClient", _TimeoutClient)
    assert await oc.fetch_oc_entitlements("kc-access-token") is None

    class _401Client(_Client):
        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(
                401,
                request=request,
                content=b'{"error":"unauthorized"}',
            )

    monkeypatch.setattr(oc.httpx, "AsyncClient", _401Client)
    assert await oc.fetch_oc_entitlements("kc-access-token") is None


@pytest.mark.asyncio
async def test_never_downgrade_local_pro_when_oc_is_free(app, monkeypatch):
    from auth.oc_entitlements import sync_oc_entitlement_grant
    from database.database import SessionLocal

    monkeypatch.setenv("MEETING_OPS_ENTITLEMENT_URL", "https://oc.test/api/v1/entitlements")

    db = SessionLocal()
    try:
        user, org = _seed_user(db, tier="pro", org_plan="pro")
        user.tier_expires_at = datetime.now(timezone.utc)
        db.commit()

        async def fake_fetch(access_token):
            return {
                "org_id": "org-123",
                "tier": "free",
                "role": "user",
                "entitlements": ["meeting_ops_access"],
            }

        called = {"n": 0}

        def fake_comp(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("comp helper must not run on free-tier OC payloads")

        import auth.oc_entitlements as oc

        monkeypatch.setattr(oc, "fetch_oc_entitlements", fake_fetch)
        monkeypatch.setattr(oc, "comp_personal_org_to_pro", fake_comp)

        await sync_oc_entitlement_grant(db, user, "kc-access-token")
        db.refresh(user)
        db.refresh(org)

        assert called["n"] == 0
        assert user.tier == "pro"
        assert org.plan == "pro"
        assert user.tier_expires_at is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_uc_sso_callback_sync_runs_before_session_mint(client, monkeypatch):
    import auth.oidc_sso as sso
    from auth.models import Organization, User, UserOrganization
    from auth.utils import get_password_hash
    from database.database import SessionLocal

    monkeypatch.setattr(sso, "KC_CLIENT_SECRET", "secret")

    db = SessionLocal()
    try:
        org = Organization(
            name="UC Login Org",
            slug="uc-login-org",
            is_active=True,
            plan="free",
        )
        user = User(
            email="login-order@example.com",
            username="login-order",
            hashed_password=get_password_hash("unused"),
            is_active=True,
            is_verified=True,
            tier="free",
        )
        db.add_all([org, user])
        db.commit()
        db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
        db.commit()
        db.refresh(user)
        db.refresh(org)

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "access_token": "kc-access-token",
                    "id_token": "kc-id-token",
                }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, **kwargs):
                return _Resp()

        monkeypatch.setattr(sso.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
        monkeypatch.setattr(
            sso,
            "_verify_id_token",
            lambda token: {
                "email": "login-order@example.com",
                "preferred_username": "login-order",
                "name": "Login Order",
                "groups": [],
            },
        )
        monkeypatch.setattr(
            sso.AuthService,
            "get_or_create_sso_user",
            lambda *args, **kwargs: db.query(User).filter(User.username == "login-order").one(),
        )

        sync_seen = {"n": 0}
        mint_seen = {"n": 0}

        async def fake_sync(db_, user_, access_token):
            assert mint_seen["n"] == 0
            assert access_token == "kc-access-token"
            sync_seen["n"] += 1

        def fake_mint(*args, **kwargs):
            mint_seen["n"] += 1
            return "session.jwt"

        monkeypatch.setattr(sso, "sync_oc_entitlement_grant", fake_sync)
        monkeypatch.setattr(sso, "create_access_token", fake_mint)

        resp = client.get(
            "/api/auth/sso/uc/callback?code=abc&state=state-123",
            headers={"Cookie": "mo_uc_oidc_state=state-123; mo_uc_oidc_rt=/"},
            follow_redirects=False,
        )

        assert resp.status_code == 302
        assert sync_seen["n"] == 1
        assert mint_seen["n"] == 1
    finally:
        db.close()
