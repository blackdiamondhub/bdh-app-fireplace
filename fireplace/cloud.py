"""Bridge from Django's sync world to pynapoleon's async client.

One asyncio loop runs on a daemon thread and owns one logged-in
``NapoleonClient``. Signing in is latched: at most one attempt every
``MIN_LOGIN_GAP`` seconds, and after a rejected password the gap doubles up
to ``MAX_LOGIN_GAP`` so a wrong password never hammers the Napoleon cloud.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from bdh_sdk import secrets
from bdh_sdk.settings import app_setting, as_bool

APP = "fireplace"
logger = logging.getLogger(__name__)

#: Seconds to wait for one cloud call (a poll lists and reads every fireplace).
CALL_TIMEOUT = 45.0
#: Never try to sign in more often than this.
MIN_LOGIN_GAP = 60.0
#: Repeated wrong-password backoff stops growing here (15 min).
MAX_LOGIN_GAP = 900.0

AUTH_MESSAGE = "Check Napoleon email/password in settings"
UNREACHABLE_MESSAGE = "Napoleon cloud didn't respond, trying again shortly"

#: Tests replace this with ``async def factory(email, password, europe) -> client``.
client_factory: Callable[[str, str, bool], Awaitable[Any]] | None = None
#: Tests replace this to move time forward.
clock: Callable[[], float] = time.monotonic


class NotConfigured(RuntimeError):
    """No Napoleon email/password has been entered."""


class CloudError(RuntimeError):
    """The Napoleon cloud failed a call."""


class AuthFailed(CloudError):
    """The Napoleon cloud rejected the email/password (or we're backing off)."""

    def __init__(self, msg=AUTH_MESSAGE):
        super().__init__(msg)


class Invalid(ValueError):
    """A value the fireplace would refuse (pynapoleon's range checks)."""


class AppError(Exception):
    """Base for the service's own errors; :func:`call` lets them through as is."""


def credentials() -> tuple[str, str, bool]:
    email = app_setting(APP, "napoleon_email", "") or ""
    password = secrets.get("napoleon_password", app=APP) or ""
    europe = as_bool(app_setting(APP, "napoleon_europe", False) or False)
    return str(email).strip(), password, europe


def configured() -> bool:
    email, password, _ = credentials()
    return bool(email and password)


def is_auth_error(exc: BaseException) -> bool:
    try:
        from pynapoleon import NapoleonAuthError
    except ImportError:  # pragma: no cover
        NapoleonAuthError = ()  # noqa: N806
    if NapoleonAuthError and isinstance(exc, NapoleonAuthError):
        return True
    # fireplaces() can leak the Ayla library's own auth errors.
    return type(exc).__name__ in ("AylaAuthError", "AylaNotAuthedError", "AylaAuthExpiringError")


def _is_value_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError)


async def _default_factory(email: str, password: str, europe: bool):
    import aiohttp
    from pynapoleon import NapoleonClient

    # trust_env so the container's egress proxy (HTTPS_PROXY) is used.
    session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=30))
    client = NapoleonClient(email, password, europe=europe, websession=session)
    try:
        await client.login()
    except BaseException:
        await session.close()
        raise
    client._bdh_session = session  # closed by _close()
    return client


class _Bridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._creds: tuple | None = None
        self.generation = 0
        self._reset_latch()

    def _reset_latch(self):
        self.next_login = 0.0
        self.auth_failures = 0
        self.logins = 0
        self._latch_creds = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run():
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                threading.Thread(target=run, name="fireplace-cloud", daemon=True).start()
                ready.wait(5)
                self._loop = loop
            return self._loop

    def submit(self, coro) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(CALL_TIMEOUT)
        except TimeoutError:
            future.cancel()
            raise CloudError(UNREACHABLE_MESSAGE)

    async def _login(self, creds):
        now = clock()
        if self._latch_creds != creds:
            # New credentials (an admin fixed the password): try right away.
            self.next_login, self.auth_failures, self._latch_creds = 0.0, 0, creds
        if now < self.next_login:
            if self.auth_failures:
                raise AuthFailed()
            raise CloudError(UNREACHABLE_MESSAGE)
        self.next_login = now + MIN_LOGIN_GAP
        self.logins += 1
        factory = client_factory or _default_factory
        try:
            client = await factory(*creds)
        except Exception as exc:  # noqa: BLE001
            if is_auth_error(exc):
                self.auth_failures += 1
                gap = min(MAX_LOGIN_GAP, MIN_LOGIN_GAP * 2 ** (self.auth_failures - 1))
                self.next_login = now + gap
                logger.warning("Napoleon sign-in rejected (%d in a row); next try in %ds",
                               self.auth_failures, gap)
                raise AuthFailed() from exc
            logger.warning("Napoleon sign-in failed: %s", exc)
            raise CloudError(UNREACHABLE_MESSAGE) from exc
        self.auth_failures = 0
        self.generation += 1
        return client

    async def _client_for(self, creds):
        if self._client is not None and self._creds == creds:
            return self._client
        await self._close()
        self._client = await self._login(creds)
        self._creds = creds
        return self._client

    async def _close(self):
        client, self._client, self._creds = self._client, None, None
        if client is None:
            return
        session = getattr(client, "_bdh_session", None)
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    async def _call(self, fn, creds):
        client = await self._client_for(creds)
        try:
            return await fn(client)
        except Exception as exc:  # noqa: BLE001
            if not is_auth_error(exc):
                raise
            # The session went bad (token refresh refused). Sign in again,
            # but only if the latch allows it; otherwise report the error.
            logger.warning("Napoleon session rejected (%s); signing in again", exc)
            await self._close()
            client = await self._client_for(creds)
            return await fn(client)

    def call(self, fn: Callable[[Any], Awaitable[Any]]) -> Any:
        creds = credentials()
        if not (creds[0] and creds[1]):
            raise NotConfigured("Napoleon email and password aren't set")
        try:
            return self.submit(self._call(fn, creds))
        except (NotConfigured, CloudError, Invalid, AppError):
            raise
        except Exception as exc:  # noqa: BLE001
            if is_auth_error(exc):
                raise AuthFailed() from exc
            if _is_value_error(exc):
                raise Invalid(str(exc)) from exc
            logger.warning("Napoleon call failed: %s", exc)
            raise CloudError(UNREACHABLE_MESSAGE) from exc

    def reset(self):
        if self._loop is not None and self._loop.is_running():
            try:
                self.submit(self._close())
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._creds = None
        self._reset_latch()


_bridge = _Bridge()


def call(fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """Run ``fn(client)`` on the cloud loop with a signed-in client."""
    return _bridge.call(fn)


def generation() -> int:
    """Bumped on every new sign-in, so cached ``Fireplace`` objects can be dropped."""
    return _bridge.generation


def latch() -> dict:
    return {"next_login": _bridge.next_login, "auth_failures": _bridge.auth_failures,
            "logins": _bridge.logins}


def reset() -> None:
    """Forget the client and the sign-in latch (tests, new credentials)."""
    _bridge.reset()
