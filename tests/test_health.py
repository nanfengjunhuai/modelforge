"""健康检查接口的烟雾测试。

跑法（项目根目录）：

    .venv/Scripts/python -m pytest
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from modelforge.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "modelforge"
