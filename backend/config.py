"""应用配置 - 通过环境变量读取（docker-compose 或 .env 注入）"""

from pydantic import field_validator, model_validator
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
    TXDOCS_DAILY_REQUEST_LIMIT: int = 20000

    # Visit/rating source acquisition. Keep disabled until the internal
    # platform contract and server-side credentials are configured.
    VISIT_SOURCE_BASE_URL: str = ""
    VISIT_SOURCE_AUTHORIZATION: str = ""
    VISIT_SOURCE_USERNAME: str = ""
    VISIT_SOURCE_PASSWORD: str = ""
    VISIT_SOURCE_LOGIN_PATH: str = "/api/login"
    VISIT_SOURCE_DETAIL_FIELD_MAP: str = ""
    VISIT_SOURCE_RATING_FIELD_MAP: str = ""
    VISIT_SOURCE_TIMEOUT_SECONDS: int = 30
    VISIT_SOURCE_MOCK: bool = False
    VISIT_SOURCE_POLICE_CODE: str = "320584710000"
    VISIT_SOURCE_POLICE_NAME: str = "滨湖新城派出所"
    VISIT_SOURCE_MAX_PAGES: int = 1000
    VISIT_SOURCE_MAX_RECORDS: int = 100000
    VISIT_SOURCE_AUTO_CONFIRM: bool = False
    # Legacy model-three task acquisition.  Credentials stay server-side;
    # QMF_* values take precedence, with the visit-source values retained as
    # a backwards-compatible fallback for deployments that share them.
    QMF_SOURCE_ACQUISITION_ENABLED: bool = False
    QMF_SOURCE_BASE_URL: str = ""
    QMF_SOURCE_TIMEOUT_SECONDS: int = 30
    QMF_SOURCE_MAX_PAGES: int = 1000
    QMF_SOURCE_MAX_RECORDS: int = 100000
    QMF_SOURCE_AUTO_SYNC: bool = True
    CERTIFICATE_IMAGE_BASE_URL: str = ""
    CERTIFICATE_SOURCE_DAILY_ENABLED: bool = False
    CERTIFICATE_SOURCE_DAILY_TIME: str = "02:30"

    @field_validator("CERTIFICATE_SOURCE_DAILY_TIME")
    @classmethod
    def validate_certificate_daily_time(cls, value: str) -> str:
        normalized = value.strip()
        parts = normalized.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("告知书每日读取时间必须使用 HH:MM")
        hour, minute = (int(part) for part in parts)
        if hour > 23 or minute > 59:
            raise ValueError("告知书每日读取时间超出有效范围")
        return f"{hour:02d}:{minute:02d}"

    # 全民防模型三 API 单条预演与封闭登记。所有敏感值仅在生产环境注入；
    # 两个协议确认和对应开关必须分别完成实测后才可开启。
    QMF_PREVIEW_ENABLED: bool = False
    QMF_REGISTRATION_ENABLED: bool = False
    QMF_API_BASE_URL: str = ""
    QMF_LOGIN_HOST: str = ""
    QMF_LOGIN_PORT: int = 0
    QMF_SOURCE_USERNAME: str = ""
    QMF_SOURCE_PASSWORD: str = ""
    QMF_SOURCE_IMEI: str = ""
    QMF_SOURCE_MACHINE_UID: str = ""
    QMF_EXPECTED_STATION_CODE: str = "320584710000"
    QMF_EXPECTED_STATION_NAME: str = "滨湖新城派出所"
    QMF_TIMEOUT_SECONDS: int = 15
    QMF_STATUS_SCAN_ENABLED: bool = False
    QMF_STATUS_SCAN_TIME: str = ""
    # The observed APK sends a TCP heartbeat roughly every 60 seconds.  The
    # read-only preview does not guess that private heartbeat frame, so one
    # session must finish before this conservative deadline.
    QMF_SESSION_MAX_SECONDS: int = 45
    QMF_PREVIEW_COOLDOWN_SECONDS: int = 45
    QMF_LOGIN_PROTOCOL_VERIFIED: bool = False
    QMF_WRITE_PROTOCOL_VERIFIED: bool = False

    # Pagination
    PAGE_SIZE_DEFAULT: int = 50
    PAGE_SIZE_MAX: int = 200

    # Auth / Session
    SESSION_COOKIE_NAME: str = "binhu_session"
    SESSION_EXPIRE_HOURS: int = 24
    SESSION_COOKIE_SECURE: bool = False
    SESSION_COOKIE_SAMESITE: str = "lax"
    # 允许同一账号分别保留一个电脑端和一个手机端会话；可通过环境变量
    # 暂时关闭以便灰度回退到旧的单会话行为。
    MULTI_DEVICE_SESSION_ENABLED: bool = False
    CORS_ALLOWED_ORIGINS: str = ""

    @field_validator("SESSION_COOKIE_SAMESITE")
    @classmethod
    def validate_session_cookie_samesite(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("SESSION_COOKIE_SAMESITE 只允许 lax、strict 或 none")
        return normalized

    @model_validator(mode="after")
    def validate_cross_site_cookie_security(self):
        if self.SESSION_COOKIE_SAMESITE == "none" and not self.SESSION_COOKIE_SECURE:
            raise ValueError("SameSite=None 必须同时启用 Secure Cookie")
        return self

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
    FULLCHAIN_ARCHIVE_DIR: str = "../fullchain-archives"
    POLICE_DISPATCH_IMPORT_DIR: str = "../police-dispatch-imports"
    FULLCHAIN_ARCHIVE_MAX_ROWS: int = 5000
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
