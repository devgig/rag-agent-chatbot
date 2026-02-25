"""TOTP-based authentication with JWT tokens for Spark Chat."""

import hashlib
import io
import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import pyotp
import qrcode
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from logger import logger

# --- Configuration ---
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "30"))
TOTP_ISSUER = os.getenv("TOTP_ISSUER", "Spark Chat")
JWT_ISSUER = "spark-chat"
JWT_ALGORITHM = "RS256"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

security = HTTPBearer()

# --- RSA key loading ---
_private_key_pem = os.getenv("JWT_PRIVATE_KEY", "")
if _private_key_pem:
    _private_key = serialization.load_pem_private_key(
        _private_key_pem.encode(), password=None
    )
    _public_key = _private_key.public_key()
    # Compute a stable kid from the public key DER
    _pub_der = _public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _kid = hashlib.sha256(_pub_der).hexdigest()[:16]
    logger.info("RSA key pair loaded for JWT signing (kid=%s)", _kid)
else:
    _private_key = None
    _public_key = None
    _kid = None
    logger.warning("JWT_PRIVATE_KEY not set — JWT signing/verification disabled")


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_jwks() -> dict:
    """Return the public key as a JWKS document (RFC 7517)."""
    if _public_key is None:
        return {"keys": []}
    pub_numbers: RSAPublicNumbers = _public_key.public_numbers()
    # Convert int to big-endian bytes, sized to key length
    n_bytes = pub_numbers.n.to_bytes(
        (_public_key.key_size + 7) // 8, byteorder="big"
    )
    e_bytes = pub_numbers.e.to_bytes(
        (pub_numbers.e.bit_length() + 7) // 8, byteorder="big"
    )
    return {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": _kid,
                "n": _b64url(n_bytes),
                "e": _b64url(e_bytes),
            }
        ]
    }


# --- JWT ---
def create_jwt_token(email: str) -> str:
    if _private_key is None:
        raise RuntimeError("JWT_PRIVATE_KEY not configured")
    payload = {
        "sub": email,
        "iss": JWT_ISSUER,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRATION_MINUTES),
    }
    return jwt.encode(
        payload,
        _private_key,
        algorithm=JWT_ALGORITHM,
        headers={"kid": _kid},
    )


def decode_jwt_token(token: str) -> dict:
    if _public_key is None:
        raise HTTPException(status_code=500, detail="JWT verification not configured")
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


# --- Google Sign-In ---
def verify_google_token(token: str) -> str:
    """Verify Google ID token and return the verified email."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google auth not configured")
    try:
        idinfo = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        if not idinfo.get("email_verified"):
            raise HTTPException(status_code=401, detail="Email not verified by Google")
        return idinfo["email"].lower()
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")


# --- TOTP ---
def generate_totp_secret() -> str:
    return pyotp.random_base32()


def generate_qr_code_base64(email: str, secret: str) -> str:
    """Generate a QR code PNG as base64 for authenticator app enrollment."""
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=email, issuer_name=TOTP_ISSUER)

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code. Allows 1 window of clock drift."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


# --- Allowlist loader ---
def load_allowed_emails() -> list[str]:
    """Read colon-delimited emails from AUTH_ALLOWED_EMAILS env var."""
    raw = os.getenv("AUTH_ALLOWED_EMAILS", "")
    if not raw.strip():
        logger.warning("AUTH_ALLOWED_EMAILS is empty — no users can log in")
        return []
    emails = [e.strip().lower() for e in raw.split(":") if e.strip()]
    logger.info(f"Loaded {len(emails)} allowed email(s) from AUTH_ALLOWED_EMAILS")
    return emails
