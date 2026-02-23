"""Auth Pydantic models for signra."""

from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    email: str


class TOTPVerifyRequest(BaseModel):
    email: str
    code: str


class LoginResponse(BaseModel):
    status: str
    requires_setup: bool = False
    qr_code: Optional[str] = None
    message: str = ""


class TokenResponse(BaseModel):
    status: str
    token: str
    email: str
