from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class CreateJobRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=64)
    credential: SecretStr
    email: str = Field(default="", max_length=320)
    proxy_pool: str = Field(default="", max_length=20000)
    login_proxy: str = Field(default="", max_length=2048)
    approve_retries: int = Field(default=30, ge=1, le=60)
    approve_concurrency: int = Field(default=1, ge=1, le=20)
    proxy_from_step: int = Field(default=3, ge=1, le=6)
    authorized: bool = False


class BatchCredentialItem(BaseModel):
    credential: SecretStr
    email: str = Field(default="", max_length=320)


class CreateBatchJobRequest(BaseModel):
    cdk: str = Field(min_length=1, max_length=64)
    items: list[BatchCredentialItem] = Field(min_length=1, max_length=10)
    proxy_pool: str = Field(default="", max_length=20000)
    login_proxy: str = Field(default="", max_length=2048)
    approve_retries: int = Field(default=30, ge=1, le=60)
    approve_concurrency: int = Field(default=1, ge=1, le=20)
    proxy_from_step: int = Field(default=3, ge=1, le=6)
    authorized: bool = False


class CdkVerifyRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class AdminLoginRequest(BaseModel):
    password: SecretStr


class CreateCdkRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    max_uses: int = Field(default=1, ge=1, le=10000)
    expires_in_days: int = Field(default=30, ge=0, le=3650)
    prefix: str = Field(default="UPI", max_length=12)
    note: str = Field(default="", max_length=500)


class CdkRevokeRequest(BaseModel):
    revoked: bool = True
