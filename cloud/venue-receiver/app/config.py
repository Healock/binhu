from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    MYSQL_HOST: str = "mysql"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "binhu_venue"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "BinhuVenueCloud"

    PHOTO_DIR: Path = Path("/data/photos")
    SCHEMA_PATH: Path = Path("/app/schema.sql")
    PHOTO_MAX_BYTES: int = 5 * 1024 * 1024
    PHOTO_MAX_PIXELS: int = 24_000_000
    QUEUED_RETENTION_HOURS: int = 24 * 7
    ACCEPTED_RETENTION_HOURS: int = 24
    AUDIT_RETENTION_DAYS: int = 7
    FORM_TOKEN_TTL_SECONDS: int = 15 * 60
    LEASE_SECONDS: int = 5 * 60

    PUBLIC_TOKEN_HMAC_KEY: str = ""
    FORM_TOKEN_HMAC_KEY: str = ""
    REQUEST_FINGERPRINT_KEY: str = ""
    ACTIVE_ENCRYPTION_KEY_ID: str = ""
    ENCRYPTION_PUBLIC_KEY_DIR: Path = Path("/run/secrets/venue-encryption-public")
    INTERNAL_REQUEST_PUBLIC_KEY_PATH: Path = Path("/run/secrets/local-request-signing.pub")
    INTERNAL_RESPONSE_PRIVATE_KEY_PATH: Path = Path("/run/secrets/cloud-response-signing.key")
    REQUIRE_MTLS_HEADER: bool = True
    ALLOW_INSECURE_INTERNAL_TESTS: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator(
        "PUBLIC_TOKEN_HMAC_KEY",
        "FORM_TOKEN_HMAC_KEY",
        "REQUEST_FINGERPRINT_KEY",
    )
    @classmethod
    def validate_secret_length(cls, value: str) -> str:
        if value and len(value.encode("utf-8")) < 32:
            raise ValueError("场所码云端 HMAC 密钥必须至少 32 字节")
        return value

    def validate_runtime(self) -> None:
        missing = [
            name
            for name in (
                "MYSQL_PASSWORD",
                "PUBLIC_TOKEN_HMAC_KEY",
                "FORM_TOKEN_HMAC_KEY",
                "REQUEST_FINGERPRINT_KEY",
                "ACTIVE_ENCRYPTION_KEY_ID",
            )
            if not str(getattr(self, name, "")).strip()
        ]
        if missing:
            raise RuntimeError(f"云端场所码缺少必要配置: {', '.join(missing)}")
        required_files = {
            "INTERNAL_REQUEST_PUBLIC_KEY_PATH": self.INTERNAL_REQUEST_PUBLIC_KEY_PATH,
            "INTERNAL_RESPONSE_PRIVATE_KEY_PATH": self.INTERNAL_RESPONSE_PRIVATE_KEY_PATH,
        }
        missing_files = [name for name, path in required_files.items() if not Path(path).is_file()]
        if not self.ENCRYPTION_PUBLIC_KEY_DIR.is_dir():
            missing_files.append("ENCRYPTION_PUBLIC_KEY_DIR")
        active_key = self.ENCRYPTION_PUBLIC_KEY_DIR / f"{self.ACTIVE_ENCRYPTION_KEY_ID}.pem"
        if not active_key.is_file():
            missing_files.append(f"encryption_key:{self.ACTIVE_ENCRYPTION_KEY_ID}")
        if missing_files:
            raise RuntimeError(f"云端场所码缺少必要密钥文件: {', '.join(missing_files)}")


settings = Settings()
