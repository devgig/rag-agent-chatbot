"""TOTP-based authentication with JWT tokens for Spark Chat."""

import os
import io
import base64
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import pyotp
import qrcode
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from logger import logger

# --- Configuration ---
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "30"))
TOTP_ISSUER = os.getenv("TOTP_ISSUER", "Spark Chat")

security = HTTPBearer()


# --- JWT ---
def create_jwt_token(email: str) -> str:
    payload = {
        "sub": email,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRATION_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
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
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


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
