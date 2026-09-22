"""系统健康检查。

前端启动时会调这个接口确认后端活着，也是你打通前后端链路时的第一个目标。
"""

from __future__ import annotations

from fastapi import APIRouter

from modelforge import __version__
from modelforge.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """返回服务基本信息。永远不该失败——它唯一的职责就是证明进程活着。"""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "modelforge",
        "version": __version__,
        "default_provider": settings.default_provider,
    }
