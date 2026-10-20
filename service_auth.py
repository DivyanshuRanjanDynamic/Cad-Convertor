"""Service-to-service access for the costing endpoints.

Only the MechHub web app's server calls /analyze-cnc and /internal/analyze-cnc:
it authenticates the customer, then calls here with a shared bearer token
(CONVERTER_SERVICE_TOKEN). Browsers never hold the token, so a direct request
from the internet is rejected.

The internal endpoint fetches the STEP file itself from a short-lived presigned
S3 URL, so uploads never pass through the web app. Fetches are limited to
HTTPS hosts in FILE_URL_ALLOWED_HOSTS (default: AWS S3) so a leaked token cannot
be used to make this service request arbitrary URLs.
"""

import hmac
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple
from urllib.parse import urlparse

from fastapi import Header, HTTPException

DEFAULT_ALLOWED_HOST_SUFFIXES = ".amazonaws.com"
FETCH_TIMEOUT_S = 60
_CHUNK = 1024 * 1024


def _configured_token() -> Optional[str]:
    token = os.getenv("CONVERTER_SERVICE_TOKEN", "").strip()
    return token or None


def require_service_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless the request carries the shared service token.

    Fails closed — with no token configured the endpoint is unavailable (503)
    rather than open.
    """
    expected = _configured_token()
    if expected is None:
        raise HTTPException(status_code=503, detail="Service token not configured.")

    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status_code=401, detail="Missing service credentials.")
    if not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid service credentials.")


def _allowed_host_suffixes() -> Tuple[str, ...]:
    raw = os.getenv("FILE_URL_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOST_SUFFIXES)
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def validate_file_url(url: str) -> None:
    """Reject anything but HTTPS URLs on an allowed host (exact or subdomain)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise HTTPException(status_code=400, detail="fileUrl must be an https URL.")
    for suffix in _allowed_host_suffixes():
        bare = suffix.lstrip(".")
        if host == bare or host.endswith("." + bare):
            return
    raise HTTPException(status_code=400, detail="fileUrl host is not allowed.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Presigned URLs never redirect; following one could leave the allowed hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch_file(url: str, max_bytes: int) -> bytes:
    """Download `url` (already validated) with a size cap. Blocking — run in a threadpool."""
    try:
        with _opener.open(url, timeout=FETCH_TIMEOUT_S) as res:
            declared = res.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise HTTPException(status_code=413, detail="File too large.")
            chunks = []
            total = 0
            while True:
                chunk = res.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="File too large.")
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        # 403/404 from S3: expired link or missing object
        raise HTTPException(
            status_code=422, detail=f"Could not download the uploaded file ({exc.code})."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail="Could not reach file storage.") from exc
