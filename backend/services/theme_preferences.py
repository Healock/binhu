"""账号外观偏好。"""

from typing import Any


def normalize_theme_mode(value: Any) -> str:
    """数据库旧值或异常值统一回退到浅色，保持升级前的显示。"""
    return str(value) if value in {"light", "dark", "system"} else "light"
