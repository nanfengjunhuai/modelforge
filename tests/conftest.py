"""所有测试共享的夹具。

三件事，都是「默认安全」：

  ① 会话存储指到临时目录（否则测试会写进项目里的真实数据库）
  ② 产物存储指到临时目录（否则测试会在你的用户目录里堆一堆图）
  ③ 每个测试前后清空 Provider 缓存（否则假 Provider 会在测试之间泄漏）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modelforge import artifacts as artifacts_package
from modelforge.api import chat, sessions
from modelforge.artifacts.local_store import LocalArtifactStore
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


@pytest.fixture(autouse=True)
def isolated_artifact_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalArtifactStore:
    """产物存储落到 `tmp_path`，绝不碰你的用户目录。

    ════════════════════════════════════════════════════════════════
    为什么这条比以前那条（会话存储）更要紧
    ════════════════════════════════════════════════════════════════
    M4 之前，测试最坏也就是写脏 `data/modelforge.db` —— 那好歹在项目里，
    删掉就行。产物的默认位置是**系统应用数据目录**
    （`%LOCALAPPDATA%\\modelforge\\artifacts`），也就是你自己的用户目录。

    没有这个夹具的话，最坏情况不是「堆一堆没用的 PNG」，而是**删掉你真实的图**：

        main.py 的 lifespan 启动时会调 `sweep_orphans()`，把「不属于任何
        现存会话」的产物目录删掉。而测试里的会话存储是空的 ——
        于是一次测试运行，会把用户**所有**真实的产物目录当成孤儿删光。

    这条路径只有在「测试真的跑了 lifespan」时才走到，也就是
    `TestClient(create_app())` 那几个夹具 —— 而它们恰恰是端到端测试用的。
    这正是 M4 那条「测试会跑 lifespan，而启动钩子会清租约」的同款问题：
    **测试的副作用发生在它正在测的那条路径之外，而且测试依然全绿。**

    ⚠️ 另有一条约束配套：`main.py` / `api/artifacts.py` / `api/sessions.py`
       取产物存储时必须写 `artifact_store_pkg.get_store()`（运行时查属性），
       不能写 `from modelforge.artifacts import get_store`（导入时绑定）——
       后者是绑死的函数对象，这里换不掉。三个文件里都写了注释说明。
    """
    store = LocalArtifactStore(settings=Settings(artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(artifacts_package, "get_store", lambda: store)
    return store
