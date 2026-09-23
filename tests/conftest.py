"""所有测试共享的夹具。

两件事，都是「默认安全」：

  ① 会话存储指到临时目录（否则测试会写进项目里的真实数据库）
  ② 每个测试前后清空 Provider 缓存（否则假 Provider 会在测试之间泄漏）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modelforge.api import chat, sessions
from modelforge.config import Settings
from modelforge.sessions.sqlite_store import SqliteSessionStore


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """每个测试前后都清空 Provider 缓存。

    `chat.acquire_provider` 用的是**进程级**缓存（因为 `AsyncOpenAI` 内部
    维护着 httpx 连接池，每个请求重建一次会让首字延迟翻倍）。
    测试之间不清理的话，上一个测试塞进去的假 Provider 会泄漏到下一个测试里 ——
    症状是某个测试突然拿到了另一个测试的剧本。
    """
    chat._providers.clear()
    yield
    chat._providers.clear()


@pytest.fixture(autouse=True)
def isolated_session_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SqliteSessionStore:
    """会话存储落到 `tmp_path`，绝不碰项目里的 `data/modelforge.db`。

    ════════════════════════════════════════════════════════════════
    为什么必须这么做
    ════════════════════════════════════════════════════════════════

    ① **测试会写进真实数据库。** `main.py` 的 lifespan 在启动时会
       `clear_all_leases()`（防止热重载把租约留给下一个进程），
       而 `test_stream.py` 的 client 夹具会**真的跑一遍 lifespan**。
       没有这个夹具的话，跑一次测试就会动到项目里的数据库文件。

    ② **它会触发 uvicorn 的热重载。** `--reload` 盯着整个项目目录，
       测试写文件会让正在跑的后端反复重载 —— 就是 M3 那个
       「沙箱随机 KeyboardInterrupt」的同一个机制。

    ③ **测试之间会互相看见对方的数据。** 会话是持久化的，
       上一个测试建的会话会出现在下一个测试的列表里。

    ⚠️ `autouse=True` 是刻意的：这件事不该要求每个测试作者都记得。
       忘了写 = 测试悄悄污染真实数据，而**测试依然全绿**。
       「默认安全」比「记得做对」可靠。
    """
    store = SqliteSessionStore(settings=Settings(database_path=tmp_path / "test.db"))

    # 替换模块属性而不是实例方法 —— main.py 里写的是 `sessions.get_store()`，
    # 也就是「运行时去那个模块上取这个名字」。所以换掉模块属性就能生效。
    monkeypatch.setattr(sessions, "get_store", lambda: store)
    return store
