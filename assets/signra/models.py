"""Auth Pydantic models for signra."""

from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    google_token: str


class TOTPVerifyRequest(BaseModel):
    google_token: str
    code: str


class LoginResponse(BaseModel):
    status: str
    requires_setup: bool = False
    email: str = ""
    qr_code: Optional[str] = None
    message: str = ""


class TokenResponse(BaseModel):
    status: str
    token: str
    email: str
