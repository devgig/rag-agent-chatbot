"""Auth — authentication microservice for Spark Chat."""

import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from auth import (
    create_jwt_token,
    generate_qr_code_base64,
    generate_totp_secret,
    get_current_user,
    get_jwks,
    load_allowed_emails,
    verify_google_token,
    verify_totp_code,
)
from db import AuthDB
from logger import logger
from models import LoginRequest, LoginResponse, TOTPVerifyRequest, TokenResponse

POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", 5432))
POSTGRES_DB = os.getenv("POSTGRES_DB", "chatbot")
POSTGRES_USER = os.getenv("POSTGRES_USER", "chatbot_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "chatbot_password")

auth_db = AuthDB(
    host=POSTGRES_HOST,
    port=POSTGRES_PORT,
    database=POSTGRES_DB,
    user=POSTGRES_USER,
    password=POSTGRES_PASSWORD,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init DB pool and sync allowlist."""
    try:
        await auth_db.init_pool()
        logger.info("Auth DB initialized successfully")

        allowed_emails = load_allowed_emails()
        if allowed_emails:
            await auth_db.sync_allowed_users(allowed_emails)
    except Exception as e:
        logger.error(f"Failed to initialize Auth DB: {e}")
        raise

    yield

    try:
        await auth_db.close()
    except Exception as e:
        logger.error(f"Error closing Auth DB: {e}")


app = FastAPI(
    title="Auth API",
    description="Authentication microservice for Spark Chat",
    version="1.0.0",
    lifespan=lifespan,
)

_default_origins = [
    "http://localhost:3000",
    "http://sparkchat.bytecourier.local",
    "http://sparkchat.bytecourier.com",
    "https://sparkchat.bytecourier.com",
]
_env_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _env_origins.split(",") if o.strip()]
    if _env_origins
    else _default_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app)


def resolve_email(body, http_request: Request) -> str:
    """Return the authenticated email from either a Google token or a direct email field.

    Direct email is only accepted from .bytecourier.local origins.
    """
    if body.google_token:
        return verify_google_token(body.google_token)

    origin = http_request.headers.get("origin", "")
    if not origin:
        raise HTTPException(status_code=403, detail="Origin header required for email login")

    hostname = urlparse(origin).hostname or ""
    if not hostname.endswith(".bytecourier.local"):
        raise HTTPException(status_code=403, detail="Email login only allowed from local network")

    return body.email


@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes probes."""
    return {"status": "healthy"}


@app.get("/.well-known/jwks.json")
async def jwks_endpoint():
    """Public JWKS endpoint for Istio waypoint JWT validation."""
    return get_jwks()


# --- Auth endpoints (unprotected) ---


@app.post("/auth/login", response_model=LoginResponse)
async def auth_login(request: LoginRequest, http_request: Request):
    """Step 1: Verify identity, check allowlist, initiate TOTP setup or prompt."""
    email = resolve_email(request, http_request)
    user = await auth_db.get_auth_user(email)

    if not user or not user["is_allowed"]:
        raise HTTPException(status_code=403, detail="Email not authorized")

    if user["is_totp_setup"]:
        return LoginResponse(
            status="code_required",
            requires_setup=False,
            email=email,
            message="Enter your authenticator code",
        )

    # First-time setup: generate TOTP secret and QR code
    secret = generate_totp_secret()
    await auth_db.create_auth_user_totp(email, secret)
    qr_b64 = generate_qr_code_base64(email, secret)

    return LoginResponse(
        status="setup_required",
        requires_setup=True,
        email=email,
        qr_code=qr_b64,
        message="Scan the QR code with your authenticator app, then enter the 6-digit code",
    )


@app.post("/auth/verify", response_model=TokenResponse)
async def auth_verify(request: TOTPVerifyRequest, http_request: Request):
    """Step 2: Verify identity + TOTP code and issue JWT."""
    email = resolve_email(request, http_request)
    user = await auth_db.get_auth_user(email)

    if not user or not user["is_allowed"] or not user["totp_secret"]:
        raise HTTPException(
            status_code=403, detail="Email not authorized or TOTP not set up"
        )

    if not verify_totp_code(user["totp_secret"], request.code):
        raise HTTPException(status_code=401, detail="Invalid code")

    if not user["is_totp_setup"]:
        await auth_db.mark_totp_setup_complete(email)

    token = create_jwt_token(email)
    return TokenResponse(status="success", token=token, email=email)


@app.get("/auth/me")
async def auth_me(current_user: str = Depends(get_current_user)):
    """Check token validity. Returns the authenticated user's email."""
    return {"email": current_user}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
