"""FastAPI 应用入口。

开发时启动（在项目根目录）：

    .venv/Scripts/python -m modelforge.main

或者：

    .venv/Scripts/uvicorn modelforge.main:app --reload
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from modelforge import __version__
from modelforge.api import health
from modelforge.config import get_settings

logger = logging.getLogger(__name__)


def _configure_logging(debug: bool) -> None:
    """配置日志，并修掉 Windows 控制台的编码问题。

    Windows 的 stdout 默认是 GBK 编码。这个项目到处都会打印中文（Agent 推理流、
    报告、错误信息），不强制 UTF-8 的话第一句中文日志就会抛 UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        # reconfigure 只在 Python 3.7+ 的真实 TextIOWrapper 上存在
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def create_app() -> FastAPI:
    """构造 FastAPI 应用。

    写成工厂函数而不是模块级单例，是为了让测试能拿到干净的实例
    （见 ``tests/test_health.py``）。
    """
    settings = get_settings()
    _configure_logging(settings.debug)

    app = FastAPI(
        title="ModelForge",
        description="人在环中的数学建模 Agent 工作台",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        # 前端跑在 :3000，后端跑在 :8000 —— 浏览器会当作跨域请求拦截，
        # 必须在这里显式放行，否则前端 fetch 会失败。
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api")

    logger.info("ModelForge %s 已装配，默认模型 provider: %s", __version__, settings.default_provider)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "modelforge.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
