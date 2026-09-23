"""产物存储 —— 模型跑代码时产生的文件（图、CSV、报告）落到哪、怎么取回来。

模块分工：

    base.py          ArtifactRef / SandboxArtifact 两个模型 + ArtifactStore 协议
    local_store.py   本地文件系统实现

取用方式和其他几层一致：**通过 `get_store()` 拿单例**，测试里
monkeypatch 这个函数就能换掉实现（见 `tests/conftest.py`）。
"""

from __future__ import annotations

from modelforge.artifacts.base import (
    ArtifactKind,
    ArtifactRef,
    ArtifactStore,
    SandboxArtifact,
    guess_kind,
    guess_mime,
)
from modelforge.artifacts.local_store import LocalArtifactStore

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactStore",
    "LocalArtifactStore",
    "SandboxArtifact",
    "get_store",
    "guess_kind",
    "guess_mime",
]

_store: ArtifactStore | None = None


def get_store() -> ArtifactStore:
    """进程级的产物存储单例。

    和 `sessions.get_store()` 同一个套路。为什么是懒加载而不是模块级的
    `_store = LocalArtifactStore()`？因为构造它会在**导入时**就检查产物
    目录、并可能打出「目录落在项目里」的警告 —— 而导入可能发生在 pytest
    的收集阶段，那时候的配置不是这次测试想要的。

    （模块级的 `from ... import LocalArtifactStore` 本身没有这个问题：
    它只执行几个类定义，不碰配置。读配置的是 `__init__`。）
    """
    global _store
    if _store is None:
        _store = LocalArtifactStore()
    return _store
