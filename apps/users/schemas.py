from ninja import Schema
from pydantic import EmailStr, Field, field_validator, model_validator
from uuid import UUID
from datetime import datetime
from typing import Optional
import re


# =====================================================
# Reusable Password Validator
# =====================================================

def validate_password_strength(v: str) -> str:
    """Reusable password strength validator."""
    if not any(char.isdigit() for char in v):
        raise ValueError("Password must contain at least one digit")
    if not any(char.isupper() for char in v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not any(char.islower() for char in v):
        raise ValueError("Password must contain at least one lowercase letter")
    return v



# =====================================================
# Auth Schemas
# =====================================================

class RegistrationSchema(Schema):
    email: EmailStr
    password: str = Field(..., min_length=8)
    confirm_password: str
    first_name: str = Field(..., min_length=2, max_length=30)
    last_name: str = Field(..., min_length=2, max_length=30)
    mobile_number: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, v):
        return v.strip().lower()  # lowercase too for consistency
    

    
    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        return validate_password_strength(v)  # Use shared validator

    @field_validator("mobile_number")
    @classmethod
    def validate_mobile(cls, v):
        if v and not re.fullmatch(r"^\+?[1-9]\d{9,14}$", v):
            raise ValueError("Invalid mobile number format. Use international format e.g. +911234567890")
        return v

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_names(cls, v):
        if not v.strip():
            raise ValueError("Name cannot be empty")
        return v.strip()

    @model_validator(mode="after")
    def check_password_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class LoginSchema(Schema):
    email: EmailStr
    password: str

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, v):
        return v.strip().lower()


class ActivationSchema(Schema):
    token: str = Field(..., min_length=1)


class ResendActivationSchema(Schema):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, v):
        return v.strip().lower()


class RefreshSchema(Schema):
    refresh: str = Field(..., min_length=1)


class PasswordChangeSchema(Schema):
    old_password: str
    new_password: str = Field(..., min_length=8)
    confirm_new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v):
        return validate_password_strength(v)  # Use shared validator

    @model_validator(mode="after")
    def check_password_match(self):
        if self.new_password != self.confirm_new_password:
            raise ValueError("Passwords do not match")
        return self


# =====================================================
# Response Schemas
# =====================================================

class UserResponseSchema(Schema):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    mobile_number: Optional[str] = None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class TokenResponseSchema(Schema):
    """Used for /refresh endpoint"""
    access: str
    refresh: str
    token_type: str = "Bearer"


class LoginResponseSchema(Schema):
    """Used for /login endpoint"""
    access: str
    refresh: str
    token_type: str = "Bearer"
    user: UserResponseSchema


class ErrorSchema(Schema):
    detail: str
    field: Optional[str] = None
    status_code: Optional[int] = None

# =====================================================
# Reset Password schema
# =====================================================


class ForgotPasswordSchema(Schema):
    email: EmailStr


class ResetPasswordSchema(Schema):
    token: str
    new_password: str = Field(..., min_length=8)
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v):
        return validate_password_strength(v)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self