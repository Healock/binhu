"""应用配置 - 通过环境变量读取（docker-compose 或 .env 注入）"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MySQL（共享连接，3个数据库）
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "binhu"
    MYSQL_PASSWORD: str
    MYSQL_POOL_SIZE: int = 10
    MYSQL_ONLINE_DATA_DB: str = "OnlineData"
    MYSQL_ARCHIVE_DB: str = "OnlineDataArchive"
    MYSQL_DAILY_REPORT_DB: str = "daily_report"

    # Encryption
    ENCRYPTION_KEY: str

    # API rate limiting
    API_RATE_LIMIT_DELAY_MS: int = 200

    # Tencent Docs API
    TXDOCS_BASE_URL: str = "https://docs.qq.com/openapi/spreadsheet/v3"

    # Pagination
    PAGE_SIZE_DEFAULT: int = 50
    PAGE_SIZE_MAX: int = 200

    # Auth / Session
    SESSION_COOKIE_NAME: str = "binhu_session"
    SESSION_EXPIRE_HOURS: int = 24
    SESSION_COOKIE_SECURE: bool = False
    SESSION_COOKIE_SAMESITE: str = "lax"
    CORS_ALLOWED_ORIGINS: str = ""

    # Fresh databases only: bootstrap one administrator without a built-in password.
    BOOTSTRAP_ADMIN_USERNAME: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""

    # Super-admin operations center
    OPS_AGENT_URL: str = "http://ops-agent:9001"
    OPS_AGENT_TOKEN: str = ""
    BACKUP_DIR: str = "../backups"
    LOG_EXPORT_MAX_BYTES: int = 10 * 1024 * 1024

    @property
    def cors_allowed_origins(self) -> list[str]:
        """Return the explicit CORS allowlist; same-origin deployments leave it empty."""
        origins = [
            origin.strip()
            for origin in self.CORS_ALLOWED_ORIGINS.split(",")
            if origin.strip()
        ]
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS 不允许使用通配符 *")
        return origins

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
