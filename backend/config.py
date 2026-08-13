"""应用配置 - 通过环境变量读取（docker-compose 或 .env 注入）"""

from pydantic import field_validator
from pydantic_settings import BaseSettings

from app_version import is_semver


class Settings(BaseSettings):
    # MySQL（同一实例，八个按业务域划分的数据库）
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "binhu"
    MYSQL_PASSWORD: str
    MYSQL_POOL_SIZE: int = 10
    MYSQL_ONLINE_DATA_DB: str = "OnlineData"
    MYSQL_ARCHIVE_DB: str = "OnlineDataArchive"
    MYSQL_DAILY_REPORT_DB: str = "daily_report"
    MYSQL_PLATFORM_DB: str = "PlatformData"
    MYSQL_VISIT_DB: str = "VisitData"
    MYSQL_DISPATCH_DB: str = "DispatchData"
    MYSQL_REGISTRY_DB: str = "RegistryData"
    MYSQL_WORKFLOW_DB: str = "WorkflowData"
    # New domain schemas may be created during a separate maintenance step.
    # Missing optional schemas must not prevent the legacy platform from booting.
    MYSQL_DOMAIN_DATABASES_ENABLED: bool = True
    PLATFORM_DOMAIN_ACTIVE: bool = False
    VISIT_DOMAIN_ACTIVE: bool = False
    DISPATCH_DOMAIN_ACTIVE: bool = False
    DAILY_DOMAIN_ACTIVE: bool = False
    REGISTRY_ADDRESS_DOMAIN_ACTIVE: bool = False
    REGISTRY_FEATURE_ENABLED: bool = False
    WORKFLOW_FEATURE_ENABLED: bool = False

    # Native client compatibility. Keep 0.0.0 until the first supported
    # Windows/Android releases adopt the version headers.
    WINDOWS_MIN_SUPPORTED_VERSION: str = "0.0.0"
    ANDROID_MIN_SUPPORTED_VERSION: str = "0.0.0"
    CLIENT_WRITE_VERSION_ENFORCEMENT_ENABLED: bool = True
    CLIENT_WRITE_IDENTIFICATION_REQUIRED: bool = False

    @field_validator(
        "WINDOWS_MIN_SUPPORTED_VERSION",
        "ANDROID_MIN_SUPPORTED_VERSION",
    )
    @classmethod
    def validate_minimum_supported_version(cls, value: str) -> str:
        normalized = value.strip()
        if not is_semver(normalized):
            raise ValueError("客户端最低支持版本必须使用 SemVer")
        return normalized

    # Encryption
    ENCRYPTION_KEY: str
    REGISTRY_HMAC_KEY: str = ""

    @property
    def registry_hmac_key(self) -> str:
        """Use a dedicated key when configured, with a staged-deploy fallback."""
        return self.REGISTRY_HMAC_KEY or self.ENCRYPTION_KEY

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
    WORKFLOW_ATTACHMENT_DIR: str = "../workflow-attachments"
    WORKFLOW_PHOTO_IMPORT_DIR: str = "../workflow-photo-imports"
    USER_AVATAR_DIR: str = "../user-avatars"
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
