"""Auth Pydantic models for auth service."""

from pydantic import BaseModel, model_validator
from typing import Optional


class LoginRequest(BaseModel):
    google_token: Optional[str] = None
    email: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one_credential(self):
        if bool(self.google_token) == bool(self.email):
            raise ValueError("Provide exactly one of google_token or email")
        return self


class TOTPVerifyRequest(BaseModel):
    google_token: Optional[str] = None
    email: Optional[str] = None
    code: str

    @model_validator(mode="after")
    def exactly_one_credential(self):
        if bool(self.google_token) == bool(self.email):
            raise ValueError("Provide exactly one of google_token or email")
        return self


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
