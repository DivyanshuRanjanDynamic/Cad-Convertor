"""Access control and quotas for the browser-facing /convert endpoint.

STEP → STL previews are uploaded straight from the browser (files are large, the
conversion is async), so the web app can't proxy them. Instead the web app's
server — after checking the customer is signed in — mints a short-lived ticket:

    v1.<base64url(JSON {"sub": uid, "scope": "convert", "exp": unix_seconds})>.<base64url(HMAC-SHA256)>

signed with CONVERTER_SERVICE_TOKEN. The browser sends it as `Authorization:
Bearer <ticket>`. This service checks the signature and expiry *before the upload
body is read* (ASGI middleware), then applies per-user quotas so one account
can't monopolise the CPU or fill S3.
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, Optional

from fastapi import HTTPException
from starlette.responses import JSONResponse

from service_auth import _configured_token

TICKET_VERSION = "v1"
CONVERT_SCOPE = "convert"
MAX_TICKET_TTL_S = 15 * 60  # reject tickets minted with a longer life
SERVICE_CALLER = "service"


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_ticket(payload: Dict, secret: str) -> str:
    """Mint a ticket (the studio does this in TypeScript; used here by tests)."""
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signed = f"{TICKET_VERSION}.{body}"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{_b64url_encode(sig)}"


def verify_ticket(ticket: str, secret: str, scope: str, now: Optional[float] = None) -> str:
    """Return the ticket's user id, or raise HTTPException(401)."""
    now = time.time() if now is None else now
    try:
        version, body, sig = ticket.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid upload ticket.")
    if version != TICKET_VERSION:
        raise HTTPException(status_code=401, detail="Invalid upload ticket.")

    expected = hmac.new(secret.encode(), f"{version}.{body}".encode(), hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(sig)
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid upload ticket.")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid upload ticket.")

    sub, exp = payload.get("sub"), payload.get("exp")
    if payload.get("scope") != scope or not isinstance(sub, str) or not sub:
        raise HTTPException(status_code=401, detail="Invalid upload ticket.")
    if not isinstance(exp, (int, float)) or exp <= now or exp > now + MAX_TICKET_TTL_S:
        raise HTTPException(status_code=401, detail="Upload ticket expired. Please try again.")
    return sub


def authorize_convert(authorization: str) -> str:
    """Caller id for /convert: the web app server (service token) or a signed-in user (ticket)."""
    secret = _configured_token()
    if secret is None:
        raise HTTPException(status_code=503, detail="Service token not configured.")

    scheme, _, credential = (authorization or "").partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        raise HTTPException(status_code=401, detail="Sign in to convert CAD files.")
    if hmac.compare_digest(credential.encode(), secret.encode()):
        return SERVICE_CALLER
    return "user:" + verify_ticket(credential, secret, CONVERT_SCOPE)


class RequireCallerMiddleware:
    """Rejects unauthorised requests to `paths` before the (up to 100 MB) body is read.

    The authorised caller id is left in `request.state.caller`. CORS preflights
    pass through; register this *before* CORSMiddleware so errors get CORS headers.
    """

    def __init__(self, app, paths: Dict[str, Callable[[str], str]]):
        self.app = app
        self.paths = paths

    async def __call__(self, scope, receive, send):
        checker = self.paths.get(scope.get("path", "")) if scope["type"] == "http" else None
        if checker is not None and scope.get("method") != "OPTIONS":
            headers = dict(scope.get("headers") or [])
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            try:
                caller = checker(authorization)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["caller"] = caller
        await self.app(scope, receive, send)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class ConvertQuota:
    """Per-caller hourly limit + per-caller and global concurrency caps.

    In-memory: correct for the single Railway replica (numReplicas = 1). With
    more replicas, each enforces its own share.
    """

    def __init__(self, per_hour: int, per_caller_active: int, total_active: int):
        self.per_hour = per_hour
        self.per_caller_active = per_caller_active
        self.total_active = total_active
        self._lock = threading.Lock()
        self._starts: Dict[str, Deque[float]] = defaultdict(deque)
        self._active: Dict[str, int] = defaultdict(int)

    @classmethod
    def from_env(cls) -> "ConvertQuota":
        return cls(
            per_hour=_env_int("CONVERT_MAX_PER_HOUR", 30),
            per_caller_active=_env_int("CONVERT_MAX_ACTIVE_PER_USER", 2),
            total_active=_env_int("CONVERT_MAX_ACTIVE_JOBS", 6),
        )

    def acquire(self, caller: str, now: Optional[float] = None) -> None:
        """Reserve a conversion slot or raise 429. Pair every success with release()."""
        now = time.time() if now is None else now
        with self._lock:
            starts = self._starts[caller]
            while starts and starts[0] <= now - 3600:
                starts.popleft()
            if caller != SERVICE_CALLER and len(starts) >= self.per_hour:
                raise HTTPException(
                    status_code=429, detail="Conversion limit reached. Please try again later."
                )
            if self._active[caller] >= self.per_caller_active:
                raise HTTPException(
                    status_code=429, detail="Wait for your current conversion to finish."
                )
            if sum(self._active.values()) >= self.total_active:
                raise HTTPException(
                    status_code=503, detail="The converter is busy. Please try again shortly."
                )
            starts.append(now)
            self._active[caller] += 1

    def release(self, caller: str) -> None:
        with self._lock:
            if self._active.get(caller, 0) > 0:
                self._active[caller] -= 1
                if self._active[caller] == 0:
                    del self._active[caller]
