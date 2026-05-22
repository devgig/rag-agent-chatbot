"""JWT verification client — validates tokens against the auth service's JWKS.

PR 3 hardening:

- **Async I/O.** The JWKS fetch uses ``httpx.AsyncClient`` so it doesn't block
  the event loop during a refresh. The legacy ``requests.get`` could stall the
  whole pod for up to 10s on the first request after a stale JWKS hit.

- **Key-id aware.** Public keys are indexed by their ``kid`` claim and the JWT
  header's ``kid`` selects the right one. The previous implementation used
  ``keys[0]`` unconditionally and broke during auth-service key rotation
  whenever the new key wasn't first in the JWKS response.

- **Proactive refresh.** A background task refreshes JWKS every
  ``JWKS_REFRESH_INTERVAL`` (default 300s) so we don't pay the fetch latency on
  the request path. First fetch happens during FastAPI lifespan startup; if
  that fails (auth backend down), subsequent requests trigger lazy retries.

- **One-shot rotation recovery.** If a token verification fails with an
  invalid-signature error, we re-fetch JWKS once and retry — covers the
  window where the auth service has rotated to a new key we haven't picked
  up yet.
"""

import asyncio
import base64
import os
import time
from typing import Dict, Optional

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers, RSAPublicKey
from cryptography.hazmat.backends import default_backend
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from logger import logger

JWT_ISSUER = os.getenv("JWT_ISSUER", "bytecourier")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "sparkchat.bytecourier.com")
JWT_ALGORITHM = "RS256"
JWKS_URL = os.getenv(
    "JWKS_URL",
    "http://auth-backend.auth-service.svc.cluster.local:8000/.well-known/jwks.json",
)
JWKS_REFRESH_INTERVAL = int(os.getenv("JWKS_REFRESH_INTERVAL", "300"))
JWKS_FETCH_TIMEOUT = float(os.getenv("JWKS_FETCH_TIMEOUT", "10"))

security = HTTPBearer()

# --- JWKS cache ---
# Indexed by kid (key id) so JWTs signed with any advertised key verify
# without depending on JWKS ordering.
_keys_by_kid: Dict[str, RSAPublicKey] = {}
_jwks_lock = asyncio.Lock()
_last_fetch: float = 0.0
_refresh_task: Optional[asyncio.Task] = None
_http_client: Optional[httpx.AsyncClient] = None


def _b64url_decode(data: str) -> bytes:
    """Base64url-decode (add padding as needed)."""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _jwk_to_public_key(key_data: dict) -> RSAPublicKey:
    """Build an RSA public key from a JWK entry."""
    n = int.from_bytes(_b64url_decode(key_data["n"]), byteorder="big")
    e = int.from_bytes(_b64url_decode(key_data["e"]), byteorder="big")
    return RSAPublicNumbers(e, n).public_key(default_backend())


async def _fetch_jwks() -> None:
    """Fetch JWKS and replace the in-memory key cache.

    Filters to RSA signature keys and indexes by ``kid``. Keys with missing
    or empty ``kid`` are stored under the empty string so tokens without a
    ``kid`` header still verify (some auth servers omit it for single-key
    deployments).
    """
    global _last_fetch, _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=JWKS_FETCH_TIMEOUT)
    try:
        resp = await _http_client.get(JWKS_URL)
        resp.raise_for_status()
        jwks = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch JWKS from %s: %s", JWKS_URL, exc)
        return

    keys = jwks.get("keys", [])
    if not keys:
        logger.warning("JWKS response has no keys (url=%s)", JWKS_URL)
        return

    new_cache: Dict[str, RSAPublicKey] = {}
    for key_data in keys:
        if key_data.get("kty") != "RSA":
            continue
        # Some JWKS responses omit ``use``; if present, require ``sig``.
        if "use" in key_data and key_data["use"] != "sig":
            continue
        try:
            pub = _jwk_to_public_key(key_data)
        except Exception as exc:
            logger.warning(
                "Skipping malformed JWK (kid=%s): %s", key_data.get("kid"), exc
            )
            continue
        kid = key_data.get("kid") or ""
        new_cache[kid] = pub

    if not new_cache:
        logger.warning("JWKS fetched but no usable RSA signing keys found")
        return

    _keys_by_kid.clear()
    _keys_by_kid.update(new_cache)
    _last_fetch = time.time()
    logger.info(
        "JWKS refreshed: %d keys (kids=%s)",
        len(new_cache),
        sorted(new_cache.keys()),
    )


async def _ensure_keys(force: bool = False) -> None:
    """Ensure we have at least one cached key, refreshing if stale or forced."""
    now = time.time()
    if not force and _keys_by_kid and (now - _last_fetch) < JWKS_REFRESH_INTERVAL:
        return
    async with _jwks_lock:
        if not force and _keys_by_kid and (time.time() - _last_fetch) < JWKS_REFRESH_INTERVAL:
            return
        await _fetch_jwks()


async def _refresh_loop() -> None:
    """Background task: refresh JWKS on the regular interval.

    Decoupled from the request path so a slow JWKS fetch never adds latency
    to a chat query. Sleeps for the full interval between refreshes; if the
    fetch itself fails it logs and retries on the next tick.
    """
    while True:
        try:
            await asyncio.sleep(JWKS_REFRESH_INTERVAL)
            await _fetch_jwks()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("JWKS background refresh errored: %s", exc)


async def init_jwks() -> None:
    """Eager fetch + start the background refresh task. Call from lifespan."""
    global _refresh_task
    await _ensure_keys(force=True)
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info(
        "JWKS subsystem ready (cached_keys=%d, refresh_interval=%ds)",
        len(_keys_by_kid),
        JWKS_REFRESH_INTERVAL,
    )


async def close_jwks() -> None:
    """Shut down the refresh task and httpx client. Call from lifespan."""
    global _refresh_task, _http_client
    if _refresh_task is not None:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
        _refresh_task = None
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception as exc:
            logger.warning("Error closing JWKS httpx client: %s", exc)
        _http_client = None


def _pick_key_for_token(token: str) -> Optional[RSAPublicKey]:
    """Return the cached public key that should verify ``token``.

    Looks up by the JWT's ``kid`` header. Falls back to the empty-kid
    entry, then to the single cached key (if only one exists) to remain
    compatible with auth servers that omit ``kid``.
    """
    if not _keys_by_kid:
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return None
    kid = header.get("kid") or ""
    if kid in _keys_by_kid:
        return _keys_by_kid[kid]
    if "" in _keys_by_kid:
        return _keys_by_kid[""]
    if len(_keys_by_kid) == 1:
        return next(iter(_keys_by_kid.values()))
    return None


def _decode_with_key(token: str, key: RSAPublicKey) -> dict:
    return jwt.decode(
        token,
        key,
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )


async def decode_jwt_token(token: str) -> dict:
    """Decode + verify a JWT against the cached JWKS."""
    await _ensure_keys()
    if not _keys_by_kid:
        raise HTTPException(
            status_code=500,
            detail="JWT verification not configured — JWKS unavailable",
        )

    key = _pick_key_for_token(token)
    if key is None:
        # Possibly a token signed with a freshly-rotated key we haven't seen
        # yet. Force a refresh once before giving up.
        await _ensure_keys(force=True)
        key = _pick_key_for_token(token)
        if key is None:
            raise HTTPException(status_code=401, detail="Invalid token")

    try:
        return _decode_with_key(token, key)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        # Could be the auth server just rotated. One-shot recovery: refresh
        # JWKS and retry once before failing the request.
        await _ensure_keys(force=True)
        key = _pick_key_for_token(token)
        if key is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        try:
            return _decode_with_key(token, key)
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Extract and validate JWT from Authorization header. Returns the
    ``sub`` claim (an email in this system).

    Defends against the (unlikely) case of an issued token missing ``sub`` —
    without this check the raw ``KeyError`` would surface as a 500 instead of
    a 401, masking the real auth failure.
    """
    payload = await decode_jwt_token(credentials.credentials)
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(
            status_code=401, detail="Token missing or invalid sub claim"
        )
    return sub
