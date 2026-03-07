"""Thin JWT verification client — validates tokens using auth service's JWKS."""

import base64
import os
import threading
import time
from typing import Optional

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.backends import default_backend
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from logger import logger

JWT_ISSUER = "spark-chat"
JWT_ALGORITHM = "RS256"
JWKS_URL = os.getenv(
    "JWKS_URL",
    "http://auth.bytecourier.local/api/svc/.well-known/jwks.json",
)
JWKS_REFRESH_INTERVAL = int(os.getenv("JWKS_REFRESH_INTERVAL", "300"))

security = HTTPBearer()

# --- JWKS cache ---
_public_key = None
_jwks_lock = threading.Lock()
_last_fetch: float = 0


def _b64url_decode(data: str) -> bytes:
    """Base64url-decode (add padding as needed)."""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _fetch_jwks() -> None:
    """Fetch JWKS from auth service and cache the RSA public key."""
    global _public_key, _last_fetch
    try:
        resp = requests.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
        jwks = resp.json()

        keys = jwks.get("keys", [])
        if not keys:
            logger.warning("JWKS response has no keys")
            return

        # Use the first RSA key
        key_data = keys[0]
        n = int.from_bytes(_b64url_decode(key_data["n"]), byteorder="big")
        e = int.from_bytes(_b64url_decode(key_data["e"]), byteorder="big")

        pub_numbers = RSAPublicNumbers(e, n)
        _public_key = pub_numbers.public_key(default_backend())
        _last_fetch = time.time()
        logger.info("JWKS fetched successfully from %s (kid=%s)", JWKS_URL, key_data.get("kid"))
    except Exception as exc:
        logger.error("Failed to fetch JWKS from %s: %s", JWKS_URL, exc)


def _ensure_public_key() -> None:
    """Ensure we have a cached public key, refreshing if stale."""
    global _public_key, _last_fetch
    now = time.time()
    if _public_key is not None and (now - _last_fetch) < JWKS_REFRESH_INTERVAL:
        return
    with _jwks_lock:
        # Double-check after acquiring lock
        if _public_key is not None and (time.time() - _last_fetch) < JWKS_REFRESH_INTERVAL:
            return
        _fetch_jwks()


# JWKS is fetched lazily on first authenticated request via _ensure_public_key()


# --- JWT verification ---
def decode_jwt_token(token: str) -> dict:
    _ensure_public_key()
    if _public_key is None:
        raise HTTPException(status_code=500, detail="JWT verification not configured — JWKS unavailable")
    try:
        return jwt.decode(
            token,
            _public_key,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        # On invalid token, try refreshing JWKS once (key rotation)
        global _last_fetch
        _last_fetch = 0
        _ensure_public_key()
        try:
            return jwt.decode(
                token,
                _public_key,
                algorithms=[JWT_ALGORITHM],
                issuer=JWT_ISSUER,
            )
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")


# --- FastAPI dependency for REST endpoints ---
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Extract and validate JWT from Authorization header. Returns email."""
    payload = decode_jwt_token(credentials.credentials)
    return payload["sub"]


# --- WebSocket auth ---
def verify_websocket_token(token: str) -> Optional[str]:
    """Validate JWT from WebSocket query param. Returns email or None."""
    _ensure_public_key()
    if _public_key is None:
        return None
    try:
        payload = jwt.decode(
            token,
            _public_key,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
        )
        return payload["sub"]
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
