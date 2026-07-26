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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
