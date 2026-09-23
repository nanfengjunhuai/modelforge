"""产物的数据模型与存储协议。

════════════════════════════════════════════════════════════════════════
「产物」是什么
════════════════════════════════════════════════════════════════════════

模型跑代码时会**产生文件** —— 一张论文插图、一份算好的 CSV、一个 `.chart.json`。
这些文件是用户真正要拿走的东西（数模论文里最要紧的就是那些图），
所以它们必须能留住、能看见、能下载。

M3 的时候留不住：`SubprocessExecutor` 每次执行结束都会把工作目录整个删掉。
M5 把这条链路补上。

════════════════════════════════════════════════════════════════════════
为什么要两个模型，而不是一个
════════════════════════════════════════════════════════════════════════

    SandboxArtifact   刚跑完、还在沙箱工作目录里的文件 —— **带 path**
    ArtifactRef       已经落盘、要发给前端的引用 —— **不带 path**

分开的唯一理由是**让类型系统替我们守住一条安全边界**：
「带文件系统路径的东西」不可能流到浏览器。合成一个模型的话，
那条边界就只剩注释在守了 —— 而注释不会在有人随手 `model_dump_json()`
的时候跳出来拦一下。

这和 M4 把 `ProviderEvent` / `StreamEvent` 拆成两个联合是同一个套路：
用类型把「谁能拿到什么」划清楚，而不是靠约定。

════════════════════════════════════════════════════════════════════════
路径穿越是怎么防的
════════════════════════════════════════════════════════════════════════

模型给的文件名完全不可信 —— 它可以叫 `../../../../.ssh/id_rsa`，
也可以叫 `CON`（Windows 的保留设备名）。

这里不去**过滤**这些名字，而是**根本不用它们当文件名**：

    落盘名 = f"{uuid4().hex}{后缀}"      ← 32 位十六进制，结构上不可能穿越

原始文件名只作为**显示名**存在日志里（`ArtifactRef.name`），
下载时作为 `Content-Disposition` 的文件名。它从头到尾没有碰过文件系统。

「消毒」类代码的麻烦在于它永远有漏网之鱼（Unicode 归一化、Windows 的
8.3 短名、ADS 的 `:`、结尾的点和空格……），而**不使用不可信输入**
不需要考虑这些。这也是 ADR-005 那条「环境变量用白名单不用黑名单」
的同一个思路，只是换了个层面。
"""

from __future__ import annotations

import mimetypes
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactStore",
    "SandboxArtifact",
    "guess_kind",
    "guess_mime",
]

ArtifactKind = Literal["image", "data", "document", "other"]
"""产物的大类。界面据此决定「画成缩略图」还是「画成一个下载链接」。

分这四个而不是直接用 MIME，是因为 MIME 太细了 —— 前端要为
`image/svg+xml` 和 `image/png` 写两遍分支，而它们要做的事完全一样。
"""

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif"})
_DATA_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".npy", ".npz", ".xlsx", ".parquet"})
_DOCUMENT_SUFFIXES = frozenset({".pdf", ".md", ".txt", ".html", ".tex", ".docx"})


def guess_kind(name: str) -> ArtifactKind:
    """按扩展名猜一个产物的大类。猜不出就归到 other。

    注意这是**给界面看的提示**，不是安全判断 —— 猜错了只会让某张图
    显示成一个下载链接，不会让什么东西变得危险。
    """
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _DATA_SUFFIXES:
        return "data"
    if suffix in _DOCUMENT_SUFFIXES:
        return "document"
    return "other"


def guess_mime(name: str) -> str:
    """按扩展名猜 MIME 类型。

    兜底是 `application/octet-stream` 而不是 `text/plain`：
    猜不出来的时候让浏览器**下载**它，而不是当文本渲染出来。
    一个猜不出类型的文件被当成文本显示，是「把一个未知的东西
    塞进当前页面的渲染上下文」，方向反了。
    """
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


class SandboxArtifact(BaseModel):
    """沙箱工作目录里一个**已经产出、还没搬运**的文件。

    ⚠️ `path` 指向工作目录内部，而工作目录在执行结束后会被删除。
    所以这个模型的寿命非常短 —— 从「扫描完」到「搬进 ArtifactStore」
    为止。搬运完就该被丢掉，不要往上层传。
    """

    name: str
    """模型给的文件名（相对于沙箱的 `artifacts/` 目录），可能是中文。
    只用于显示和下载命名，**不参与任何文件系统操作**。"""

    path: Path
    """文件此刻的真实位置。搬运之后就失效了。"""

    size: int
    mime: str


class ArtifactRef(BaseModel):
    """一个已落盘的产物 —— **唯一会流到前端的形状**。

    它会经由 `ToolResult.artifacts` 被写进事件日志，所以刷新页面之后
    它是从日志里读回来的，不是重新扫盘得到的。这一点很重要：

      · 日志是唯一真相源（ADR-009），产物引用跟着它走，不会出现
        「日志里说有一张图，但盘上找不到」这种两套状态不一致的情况；
      · 代价是**它不可变**。产物一旦记进日志，就不能再改名、不能再改
        大小 —— 想在下载时统计「实际字节数」之类的，只能另存一份。
    """

    id: str
    """`uuid4().hex`，32 位十六进制。路由和文件名都由它构成。"""

    name: str
    """给用户看的名字（模型起的原名），可能是中文。"""

    kind: ArtifactKind
    mime: str
    size: int


@runtime_checkable
class ArtifactStore(Protocol):
    """产物存取的契约。

    和 `CodeExecutor` / `ChatProvider` / `TurnRecorder` 一样保持极小 ——
    换个存储后端（对象存储、S3、远端）不需要改沙箱和 Agent 循环。
    """

    async def ingest(self, scope: str, files: Sequence[SandboxArtifact]) -> list[ArtifactRef]:
        """把刚产出的一批文件搬进存储，返回可以持久化的引用。

        Args:
            scope: 归属（会话 id）。存储只把它当**不透明标签**用 ——
                它不知道会话是什么，也不该知道。
            files: 待搬运的文件。实现有责任在**搬运完成后**让源文件消失。

        Returns:
            与 `files` 一一对应的引用。某个文件搬运失败时**跳过它**而不是
            整批失败 —— 三张图里坏了一张，另外两张仍然是好的，不该一起丢。
        """
        ...

    async def delete_scope(self, scope: str) -> int:
        """删掉某个归属下的全部产物，返回删除的**文件**数。"""
        ...

    async def path_for(self, scope: str, artifact_id: str) -> Path | None:
        """定位一个产物在磁盘上的位置。找不到返回 None。

        这是唯一一个「把路径交出去」的方法，所以它必须自己完成
        全部合法性校验（id 是不是 uuid 的形态、解析出来的路径有没有
        跑出存储根目录）。调用方不该、也不能再自己拼路径。
        """
        ...

    async def sweep_orphans(self, known_scopes: set[str]) -> int:
        """清掉不属于任何现存归属的产物，返回清理掉的个数。

        在 `main.py` 的 lifespan 启动段调用。它存在的理由和 M4 启动时
        `clear_all_leases()` 一模一样：**异常退出会留下垃圾**。

        产物落盘和那条日志事件的写入不是原子的 —— 文件先搬进去，随后进程
        被强杀，事件就没记进日志。于是盘上多了一个谁都不认识的归属，
        而且再也没人会来认领它。

        实现必须**只删能证明是孤儿的**（名字不在 `known_scopes` 里）。
        这个方法跑在用户的数据目录里，宁可漏删也不能错删 ——
        漏删的代价是一点磁盘空间，错删的代价是用户的图没了。
        """
        ...
