"""Local patches to minimax2api: the three holes that emptied the account pool.

Background (2026-10-21, real DSH session ``session-a26a2505-…``): every turn
failed with ``minimax llm HTTP 401`` and then
``accounts unusable: minimax-cn, minimax-global``.  Three separate defects
turned one expired token into an hour-long outage:

1. **A dead stdout pipe killed the renewal path.**  ``renew_account`` writes
   the account row *first* and reports the outcome *afterwards*; the report is a
   ``print``.  When stdout is gone (the parent died, the log was rotated, a
   supervisor closed the stream) ``print`` raises ``BrokenPipeError``, so the
   renewal "failed" even though the token had been renewed.  The request path
   reads that as "the credential is dead" and **retires an account whose
   credential was fine**.  The sweep's own error handler printed too, so the
   sweep died with it and no renewal ever happened again.

2. **A renewal that raised was treated as a refusal.**  ``pool._record_failure``
   catches every exception from the renewer and falls through to retirement.
   An exception means "not judged", not "dead": a proxy outage or a logging
   error must never empty the pool.

3. **A retired account had no way back.**  The sweep only renews accounts whose
   token is near expiry, so an account retired for the reason above stayed
   retired until its token expired on its own — and every request in between
   answered ``accounts unusable``.

These tests pin the fixed behaviour.  They are local additions: upstream's own
suite (``tests/test_bridge.py``) must keep passing unchanged.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from app import keepalive as keepalive_mod
from app import pool as pool_mod
from app import upstream
from app.records import KIND_OAUTH, STATUS_ACTIVE, STATUS_COOLDOWN, STATUS_INVALID, Account


# --------------------------------------------------------------------- fakes


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: answers the refresh grant."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, headers=None, timeout=None):
        _FakeAsyncClient.calls.append(url)
        return _FakeResponse(
            {"access_token": "tok-new", "expires_in": 3600, "refresh_token": "ref-new"}
        )


class _FakeKeepaliveDB:
    def __init__(self, accounts):
        self._accounts = {account.id: account for account in accounts}
        self.updates: list[str] = []

    async def list_accounts(self):
        return list(self._accounts.values())

    async def account_by_id(self, account_id):
        return self._accounts.get(account_id)

    async def update_account(self, account_id, apply_fn):
        account = self._accounts[account_id]
        apply_fn(account)
        self.updates.append(account_id)
        return account


def _oauth_account(**overrides) -> Account:
    base = {
        "id": "acc-1",
        "name": "minimax-cn",
        "kind": KIND_OAUTH,
        "region": "cn",
        "token": "tok-old",
        "refresh_token": "ref-old",
        "token_expires_at": time.time() + 3600,
        "enabled": True,
        "status": STATUS_ACTIVE,
        "max_concurrent": 1,
    }
    base.update(overrides)
    return Account(**base)


def _settings_holder():
    from app.config import Settings

    return lambda: Settings()


# ---------------------------------------------------------------------- tests


def test_renewal_survives_a_dead_stdout(monkeypatch):
    """A broken stdout must not turn a successful renewal into a failure.

    The renewal writes the account row before it reports the outcome, so a
    raise in the report used to leave the row renewed and the caller convinced
    the credential was dead.
    """

    class _BrokenStdout:
        def write(self, _text):
            raise BrokenPipeError("stdout is gone")

        def flush(self):
            raise BrokenPipeError("stdout is gone")

    account = _oauth_account()
    db = _FakeKeepaliveDB([account])
    keeper = keepalive_mod.Keeper(db, None, _settings_holder())
    monkeypatch.setattr(keepalive_mod.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())

    result = asyncio.run(keeper.renew_account(account.id))

    assert result.ok is True
    assert account.token == "tok-new"
    assert account.refresh_token == "ref-new"
    assert account.status == STATUS_ACTIVE
    assert account.last_error == ""


class _FakePoolDB:
    """The three things the pool asks the store for (oauth accounts only)."""

    def __init__(self, accounts):
        self._accounts = {account.id: account for account in accounts}
        self.writes = []

    async def list_accounts(self):
        return list(self._accounts.values())

    async def save_account_state(self, account_id, apply_fn):
        account = self._accounts[account_id]
        apply_fn(account)
        self.writes.append(account_id)
        return account

    async def upsert_account(self, account):
        self._accounts[account.id] = account
        return account


def test_pool_cools_instead_of_retiring_when_the_renewal_raises():
    """A renewal that raised means "not judged", not "dead".

    The account must come back to the pool on the next pass instead of being
    retired, which is what emptied the pool for an hour.
    """
    from app.config import RoutingSettings, Settings

    account = _oauth_account()
    db = _FakePoolDB([account])
    routing = RoutingSettings()
    routing.capacity_wait_sec = 0
    settings = Settings(routing=routing)
    engine = pool_mod.Pool(db, lambda: settings)
    asyncio.run(engine.load())

    async def _raising_renewer(_account_id):
        raise RuntimeError("renewal blew up after updating the row")

    engine.renewer = _raising_renewer
    lease = asyncio.run(engine.acquire())
    asyncio.run(engine.release(lease, success=False, error=upstream.InvalidCredential("401")))

    stored = engine.account(lease.account.id)
    assert stored.status != STATUS_INVALID, "a raising renewal must not retire the account"
    assert stored.status == STATUS_COOLDOWN


def test_sweep_revives_a_retired_account():
    """A retired account that can still be renewed comes back on the next sweep.

    Its token is nowhere near expiry, so the old due-check skipped it and the
    pool stayed empty until the token expired on its own.
    """
    account = _oauth_account(status=STATUS_INVALID, token_expires_at=time.time() + 3600)
    db = _FakeKeepaliveDB([account])
    keeper = keepalive_mod.Keeper(db, None, _settings_holder())
    original = keepalive_mod.httpx.AsyncClient
    keepalive_mod.httpx.AsyncClient = _FakeAsyncClient
    try:
        summary = asyncio.run(keeper.sweep())
    finally:
        keepalive_mod.httpx.AsyncClient = original

    assert summary["due"] == 1
    assert summary["renewed"] == 1
    assert account.status == STATUS_ACTIVE
    assert account.token == "tok-new"
    assert account.fail_count == 0
    assert account.last_error == ""


def test_sweep_still_skips_accounts_it_cannot_renew():
    """An account with neither refresh token nor password is not renewable."""
    account = _oauth_account(status=STATUS_INVALID, refresh_token="")
    db = _FakeKeepaliveDB([account])
    keeper = keepalive_mod.Keeper(db, None, _settings_holder())

    summary = asyncio.run(keeper.sweep())

    assert summary["due"] == 0
    assert summary["skipped"] == 1
    assert account.status == STATUS_INVALID
