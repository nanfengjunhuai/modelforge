"""FastAPI 应用入口。

开发时启动（在项目根目录）：

    .venv/Scripts/python -m modelforge.main

或者：

    .venv/Scripts/uvicorn modelforge.main:app --reload
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from modelforge import __version__
from modelforge.api import chat, health, sessions
from modelforge.config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用的生命周期钩子：yield 之前是启动，之后是关闭。

    为什么需要它？因为 Provider 内部持有一个 httpx 连接池，而连接池必须在
    进程退出时显式关闭。FastAPI 里没有别的地方适合放这段清理逻辑 ——
    写 `atexit` 不行（它跑在同步上下文里，没法 await 异步的关闭）。

    开发时用 `--reload` 会频繁重启，不关的话未释放的连接会越积越多，
    最后在日志里看到一堆 "Unclosed client session" 之类的告警。

    启动时还多做一件事：**清空所有会话租约**。见下面那段注释。
    """
    logger.info("ModelForge %s 启动中……", __version__)

    # ── 清理上一次进程留下的租约 ──────────────────────────────
    #
    # 会话靠数据库租约做并发保护（api/sessions.py）。正常情况下流结束时
    # 自己会还，但**进程被强杀**（Ctrl+C 来不及、任务管理器结束进程、
    # --reload 把旧进程干掉）时来不及还，那条租约就会一直挂到 TTL 过期。
    #
    # 开发期这个问题尤其刺眼：改一行代码触发热重载 → 正在跑的会话带着租约
    # 被掐死 → 用户刷新页面，拿到一个莫名其妙的 409，而且要等三分钟。
    #
    # 进程启动时清一遍是安全的：**这个进程刚刚开始，它不可能持有任何租约。**
    # 所以这一行清掉的必然是「死人的租约」。
    try:
        cleared = await sessions.get_store().clear_all_leases()
        if cleared:
            logger.info("已清空 %d 条上次遗留的会话租约", cleared)
    except Exception:
        # 数据库还没建（第一次启动）、或者文件权限有问题……
        # 这不该阻止服务起来：租约本来就有 TTL 兜底。
        logger.warning("清空会话租约失败（不影响启动）", exc_info=True)

    yield

    await chat.close_providers()
    logger.info("ModelForge 已关闭，连接池已释放")


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
        lifespan=lifespan,
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
    app.include_router(chat.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")

    logger.info(
        "ModelForge %s 已装配，默认模型 provider: %s", __version__, settings.default_provider
    )
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
