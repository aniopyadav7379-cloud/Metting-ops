"""In-flight lock guard for reprocess enqueue (v3.29.1, item #4).

`enqueue_reprocess` previously deduped via a STABLE Arq `_job_id`
(`reprocess-{pk}`). That prevents concurrent double-runs but also blocks a
legitimate LATER reprocess of the same session for the whole keep_result
window, and a hard-crashed worker can leave the job key behind so recovery's
re-enqueue is silently refused.

v3.29.1 replaces that with an explicit per-session in-flight lock + a UNIQUE
per-attempt job_id. `enqueue_reprocess` must:
  * run the first enqueue (acquire the lock, enqueue a job),
  * treat a SECOND enqueue while one is in flight as a NO-OP (return None, no
    duplicate job) — NOT an in-process fallback,
  * allow a later enqueue once the lock is released (job finished),
  * force=True clears a stale lock so recovery is never blocked,
  * the job releases ONLY its own lock (compare-and-delete by token).

Exercised against a tiny fake Arq pool with NX-set / delete / Lua-CAD eval
semantics — no Redis. Uses asyncio.run (py3.14-safe).
"""
from __future__ import annotations

import asyncio


class _FakePool:
    """Minimal Redis-ish stand-in: dict-backed NX set, delete, Lua
    compare-and-delete eval, plus an enqueue_job that records calls."""

    def __init__(self):
        self.store: dict = {}
        self.enqueued: list = []

    async def delete(self, key):
        self.store.pop(key, None)
        return 1

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def eval(self, script, numkeys, *args):
        # compare-and-delete: KEYS[1]=args[0], ARGV[1]=args[1]
        key, token = args[0], args[1]
        if self.store.get(key) == token:
            self.store.pop(key, None)
            return 1
        return 0

    async def incrbyfloat(self, key, amt):
        return 0.0

    async def expire(self, key, ttl):
        return True

    async def enqueue_job(self, fn, *args, **kwargs):
        jid = kwargs.get("_job_id", "j")
        self.enqueued.append((jid, args))

        class _J:
            job_id = jid

        return _J()


def _patch(monkeypatch, pool):
    import workers.bulk_import_worker as biw
    import workers.reprocess_workers as rw

    monkeypatch.setenv("REPROCESS_ARQ_ENABLED", "true")

    async def _get_pool():
        return pool

    monkeypatch.setattr(biw, "get_arq_pool", _get_pool)
    # No real DB read for org/hours metering.
    monkeypatch.setattr(rw, "_resolve_org_and_hours", lambda pk: (None, 0.0))
    return rw


def test_duplicate_enqueue_is_noop_while_in_flight(monkeypatch):
    pool = _FakePool()
    rw = _patch(monkeypatch, pool)

    jid1 = asyncio.run(rw.enqueue_reprocess(101))
    assert jid1 is not None
    assert len(pool.enqueued) == 1
    assert rw._reprocess_lock_key(101) in pool.store  # lock held

    # Second enqueue while the lock is held -> no-op, no duplicate job.
    jid2 = asyncio.run(rw.enqueue_reprocess(101))
    assert jid2 is None
    assert len(pool.enqueued) == 1


def test_enqueue_runs_again_after_lock_release(monkeypatch):
    pool = _FakePool()
    rw = _patch(monkeypatch, pool)

    asyncio.run(rw.enqueue_reprocess(202))
    assert len(pool.enqueued) == 1
    key = rw._reprocess_lock_key(202)
    token = pool.store[key]

    # Simulate the job finishing: compare-and-delete releases the lock.
    asyncio.run(pool.eval(rw._RELEASE_LOCK_LUA, 1, key, token))
    assert key not in pool.store

    # A new enqueue now succeeds (a UNIQUE job_id means keep_result can't
    # swallow it).
    jid = asyncio.run(rw.enqueue_reprocess(202))
    assert jid is not None
    assert len(pool.enqueued) == 2
    assert pool.enqueued[0][0] != pool.enqueued[1][0]  # distinct job_ids


def test_force_clears_stale_lock(monkeypatch):
    pool = _FakePool()
    rw = _patch(monkeypatch, pool)

    asyncio.run(rw.enqueue_reprocess(303))
    assert len(pool.enqueued) == 1
    # A duplicate without force is a no-op...
    assert asyncio.run(rw.enqueue_reprocess(303)) is None
    assert len(pool.enqueued) == 1
    # ...but force=True clears the stale lock and re-enqueues (recovery path).
    jid = asyncio.run(rw.enqueue_reprocess(303, force=True))
    assert jid is not None
    assert len(pool.enqueued) == 2


def test_job_releases_only_its_own_lock(monkeypatch):
    """reprocess_session_job's finally must compare-and-delete: an older job
    finishing must NOT clear a lock a newer attempt now holds."""
    pool = _FakePool()
    rw = _patch(monkeypatch, pool)

    import api.recording as rec

    async def _noop(pk):
        return None

    monkeypatch.setattr(rec, "_run_session_reprocess", _noop)

    key = rw._reprocess_lock_key(404)
    asyncio.run(pool.set(key, "new"))  # a newer attempt holds the lock

    from workers.reprocess_workers import reprocess_session_job

    # Older job finishes carrying token "old" -> must not delete "new".
    asyncio.run(reprocess_session_job({"redis": pool}, 404, None, 0.0, "old"))
    assert pool.store.get(key) == "new"
