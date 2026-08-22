"""HTTP 边界安全配置。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = [
    "Accept",
    "Authorization",
    "Content-Type",
    "X-User-Activity",
    "X-Binhu-Client-Platform",
    "X-Binhu-Client-Version",
    "X-Binhu-Device-Id",
]


def add_cors_middleware(app: FastAPI, allowed_origins: list[str]) -> None:
    """只为明确列出的外部客户端来源开启带凭据 CORS。"""
    if not allowed_origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
    )
