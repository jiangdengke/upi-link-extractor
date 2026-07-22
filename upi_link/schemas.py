from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class CreateJobRequest(BaseModel):
    credential: SecretStr
    email: str = Field(default="", max_length=320)
    proxy_pool: str = Field(default="", max_length=20000)
    login_proxy: str = Field(default="", max_length=2048)
    approve_retries: int = Field(default=30, ge=1, le=60)
    approve_concurrency: int = Field(default=1, ge=1, le=20)
    proxy_from_step: int = Field(default=3, ge=1, le=6)
    authorized: bool = False

