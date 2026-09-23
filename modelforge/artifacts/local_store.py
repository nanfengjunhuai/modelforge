"""产物的本地文件系统实现。

════════════════════════════════════════════════════════════════════════
落盘布局
════════════════════════════════════════════════════════════════════════

    <artifacts_dir>/
    └── <scope>/                   ← 会话 id
        ├── 3f2a…c1.png            ← uuid4().hex + 原扩展名
        ├── 8b71…04.pdf
        └── 9d0e…af.chart.json

目录名和文件名都只由**我们自己生成**的字符串构成（32 位十六进制），
模型的原始文件名只活在 `ArtifactRef.name` 里。见 `base.py` 的模块注释。

════════════════════════════════════════════════════════════════════════
为什么不用数据库存
════════════════════════════════════════════════════════════════════════

三个候选，选第三个：

  · **BLOB 进 SQLite** —— 一张 300 dpi 的图轻松几 MB，事件日志会被撑爆，
    备份变成复制一堆二进制，而且没法用静态服务器的 `sendfile` 直出。
  · **新开一张 `artifacts` 表** —— 能行，但会引入**第二套状态**：
    日志里记着有产物、表里也记着有产物，两者可以不一致。而 ADR-009 明确
    选了「日志是唯一真相源」。
  · **只存文件，引用跟着日志走** —— 产物的元数据（名字、大小、类型）
    作为 `ToolResult.artifacts` 的一部分被写进 `tool` 事件。

选第三个的实际好处很具体：**刷新页面时产物自动就回来了**，
因为 `GET /api/sessions/{id}` 本来就会把整个事件日志交出去。
不需要新表、不需要新查询、不需要额外的一次「把产物也带上」。
M4 定 `tool` 事件要原样存 `ToolResult` 的时候，这一条是顺手得到的。

代价写在明处：日志只能 append，所以产物的**元数据不可变**。
真要做「删掉某一个产物」，必须是「删文件 + 日志里那条引用标记为已删」，
不能就地把 ref 改掉（那会篡改历史）。M5 不做这个功能。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path

from modelforge.artifacts.base import ArtifactRef, SandboxArtifact, guess_kind, guess_mime
from modelforge.config import Settings, get_settings
from modelforge.paths import PROJECT_ROOT, is_inside, resolve_project_path

logger = logging.getLogger(__name__)

__all__ = ["LocalArtifactStore"]

# 自己生成的 id 长这样：uuid4().hex，32 位小写十六进制。
# 校验用的是「必须完全匹配」，不是「把非法字符删掉」—— 后者会留下
# `....//` 这种删完还成立的怪东西，而前者没有回旋余地。
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# scope 是会话 id，理论上也是 uuid4().hex。但这里放宽一点，
# 只要求「能安全地当一个目录名」—— 将来若换成别的作用域（比如
# 一个项目下的多个子会话），不必回来改这个正则。
_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 扩展名要跟着 id 一起落盘（`<id>.png`），但模型起的名字里的后缀完全不可信 ——
# 可能是 `.tar.gz`、可能是空、也可能带一堆怪字符。所以只留下
# 「点 + 最多 8 个字母数字」，其余的丢掉。
#
# `.chart.json` 这种双后缀会被截成 `.json`，这是可接受的：文件本身还在，
# 界面上区分「图表数据」和普通 JSON 靠的是名字（`ArtifactRef.name`），不是后缀。
_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


class LocalArtifactStore:
    """满足 `ArtifactStore` 协议的本地实现。"""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.root = resolve_project_path(Path(self._settings.artifacts_dir))

        # 产物落进项目里是个**看起来无害、实际会间歇性炸**的配置，
        # 而且比沙箱工作目录那个更难发现 —— 因为只有画图时才会触发。
        # 复刻 `SubprocessExecutor.__init__` 里那段警告的措辞。
        if is_inside(self.root, PROJECT_ROOT):
            logger.warning(
                "产物目录 %s 位于项目目录内。如果后端用 uvicorn --reload 启动，"
                "每生成一张图都会触发一次热重载，重载信号可能打断正在执行的代码，"
                "表现为随机的 KeyboardInterrupt（退出码 3221225786），"
                "而且只在画图那几轮出现，看起来像「模型偶尔抽风」。"
                "建议改用项目外的目录（默认值就是系统应用数据目录）。",
                self.root,
            )

    # ────────────────────────────────────────────── 写入

    async def ingest(self, scope: str, files: Sequence[SandboxArtifact]) -> list[ArtifactRef]:
        """把工作目录里的文件搬进来。契约见 `ArtifactStore.ingest`。

        用 `shutil.move` 而不是 `copy`：源目录马上就要被删掉了，
        复制一遍纯属浪费（图可能有几十 MB）。
        `move` 跨设备时会自动退化成「复制 + 删除」，所以从系统临时目录
        搬到用户目录（两块盘）也照样能用。
        """
        if not files:
            return []
        return await asyncio.to_thread(self._ingest_blocking, scope, files)

    def _ingest_blocking(
        self, scope: str, files: Sequence[SandboxArtifact]
    ) -> list[ArtifactRef]:
        target_dir = self._scope_dir(scope)
        if target_dir is None:
            logger.warning("产物归属 %r 不是合法的目录名，整批丢弃", scope)
            return []

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 不抛异常：和 `CodeExecutor` 的契约一致 —— 搬不了产物是
            # 「我们这边的失败」，不该让整条对话流断掉。模型还是拿到了
            # stdout，用户还是能看到代码跑成功了，只是图丢了。
            logger.warning("创建产物目录失败：%s", exc)
            return []

        refs: list[ArtifactRef] = []
        for item in files:
            ref = self._move_one(item, target_dir)
            # 单个文件搬失败就跳过它，不要整批失败 ——
            # 三张图里坏了一张，另外两张仍然是好的。
            if ref is not None:
                refs.append(ref)

        if len(refs) != len(files):
            logger.warning(
                "产物搬运不完整：%d/%d 成功（归属 %s）", len(refs), len(files), scope
            )
        return refs

    def _move_one(self, item: SandboxArtifact, target_dir: Path) -> ArtifactRef | None:
        artifact_id = uuid.uuid4().hex
        suffix = self._safe_suffix(item.name)
        destination = target_dir / f"{artifact_id}{suffix}"

        try:
            shutil.move(str(item.path), str(destination))
        except OSError as exc:
            logger.warning("搬运产物 %r 失败：%s", item.name, exc)
            return None

        return ArtifactRef(
            id=artifact_id,
            name=item.name,
            kind=guess_kind(item.name),
            # MIME 在这里重新猜一遍，而不是用扫描时那个值。
            #
            # 为什么？因为扫描阶段（`sandbox/subprocess_exec.py`）是**按扩展名**
            # 猜的，而此刻我们手里还是模型给的原始名字 —— 两边算出来的东西
            # 一模一样。既然如此就不该让这个值穿过一层协议再传过来：
            # 少一个可以不一致的字段，就少一处对不上的可能。
            mime=guess_mime(item.name),
            size=item.size,
        )

    # ────────────────────────────────────────────── 读取

    async def path_for(self, scope: str, artifact_id: str) -> Path | None:
        """定位产物的磁盘路径。契约见 `ArtifactStore.path_for`。"""
        return await asyncio.to_thread(self._path_for_blocking, scope, artifact_id)

    def _path_for_blocking(self, scope: str, artifact_id: str) -> Path | None:
        # 两道校验，都是「必须完全匹配」而不是「剔除非法字符」。
        #
        # 这一层是**唯一**把路径交出去的地方，所以它得自己把所有关 ——
        # 调用方（API 路由）不该、也无法再拼一次路径。把校验和拼路径
        # 放在同一个函数里，是为了让它们不可能只改一半。
        if not _SCOPE_PATTERN.match(scope) or not _ID_PATTERN.match(artifact_id):
            return None

        scope_dir = self.root / scope
        # 最多只可能命中一个：所有 id 都是 32 位，没有任何一个是另一个的前缀。
        # 用 glob 而不是「记录扩展名再拼」，是为了让扩展名只在一处存在
        # （落盘时的那个 `f"{id}{suffix}"`），不必在两个地方保持同步。
        matches = [p for p in scope_dir.glob(f"{artifact_id}*") if p.is_file()]
        if len(matches) != 1:
            return None
        return matches[0]

    # ────────────────────────────────────────────── 删除

    async def delete_scope(self, scope: str) -> int:
        """删掉一个会话的全部产物。契约见 `ArtifactStore.delete_scope`。"""
        return await asyncio.to_thread(self._delete_scope_blocking, scope)

    def _delete_scope_blocking(self, scope: str) -> int:
        scope_dir = self._scope_dir(scope)
        if scope_dir is None or not scope_dir.exists():
            return 0

        count = sum(1 for p in scope_dir.rglob("*") if p.is_file())
        # ignore_errors=True：删不掉通常是因为 Windows 上还有句柄没释放
        # （浏览器正在下载那张图）。那种情况下让系统清理即可，
        # 绝不能因为清理失败就让「删除会话」这个操作报错。
        shutil.rmtree(scope_dir, ignore_errors=True)
        return count

    async def sweep_orphans(self, known_scopes: set[str]) -> int:
        """删掉不属于任何现存会话的产物目录，返回清理掉的目录数。

        在 `main.py` 的 lifespan 启动段调用。它解决的问题和 M4 的
        `clear_all_leases()` 一模一样：**异常退出会留下垃圾**。

        什么情况下会留下孤儿？事件的写入和产物的搬迁不是原子的：
        产物先落盘，随后进程被强杀，那条 `tool` 事件就没写进日志 ——
        于是盘上有一个谁都不认识的目录，而且再也没人会来认领它。

        ⚠️ 只删**能证明是孤儿**的目录（名字不在已知会话集合里）。
        这是这个函数唯一安全的做法 —— 它跑在用户的数据目录里，
        宁可漏删也不能错删。
        """
        return await asyncio.to_thread(self._sweep_blocking, known_scopes)

    def _sweep_blocking(self, known_scopes: set[str]) -> int:
        if not self.root.exists():
            return 0

        removed = 0
        try:
            children = list(self.root.iterdir())
        except OSError as exc:
            logger.warning("扫描产物目录失败（不影响启动）：%s", exc)
            return 0

        for child in children:
            if not child.is_dir() or child.name in known_scopes:
                continue
            logger.info("清理孤儿产物目录：%s", child.name)
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        return removed

    # ────────────────────────────────────────────── 内部

    def _scope_dir(self, scope: str) -> Path | None:
        """把 scope 变成一个安全的目录路径，非法则返回 None。"""
        if not _SCOPE_PATTERN.match(scope):
            return None
        candidate = self.root / scope
        # 双保险。上面的正则已经排除了所有能穿越的形态，但这里是
        # 「即使正则哪天被改松了也不会漏」的那一层。
        if not is_inside(candidate, self.root):
            return None
        return candidate

    @staticmethod
    def _safe_suffix(name: str) -> str:
        """从模型给的文件名里取出一个安全的扩展名。取不到就返回空串。"""
        suffix = Path(name).suffix
        return suffix if _SUFFIX_PATTERN.match(suffix) else ""
