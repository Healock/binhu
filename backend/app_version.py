"""应用版本号的唯一读取入口。"""

import os
import re
from pathlib import Path


_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def read_app_version() -> str:
    """从环境变量或仓库根目录 VERSION 文件读取 SemVer 版本号。"""
    configured = os.environ.get("APP_VERSION", "").strip()
    candidates = [
        configured,
        *(
            path.read_text(encoding="utf-8").strip()
            for path in (
                Path("/app/VERSION"),
                Path(__file__).resolve().parent.parent / "VERSION",
            )
            if path.is_file()
        ),
    ]
    for value in candidates:
        if value and _SEMVER_PATTERN.fullmatch(value):
            return value
    return "0.0.0"


APP_VERSION = read_app_version()
