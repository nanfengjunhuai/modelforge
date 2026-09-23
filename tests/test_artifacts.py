"""产物存储与扫描的测试。

这个文件里有两类测试值得单独说明：

    test_ingested_files_are_renamed_to_uuids
    test_path_traversal_is_structurally_impossible
        路径穿越**不是被拦住的，是不成立的**。磁盘上的文件名完全由
        `uuid4().hex` 构成，模型给的名字从头到尾没碰过文件系统。
        所以这两条测的不是「过滤得对不对」，而是「那条设计还在不在」。

    test_oversized_batches_are_reported_not_silently_dropped
        截断必须说出来。项目里每一处截断都遵守这条
        （对比 `_read_capped` 的措辞）—— 静默丢弃会让用户看到
        「方案里说有三张图，界面上只有两张」而完全不知道为什么。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modelforge.artifacts.base import ArtifactStore, SandboxArtifact, guess_kind, guess_mime
from modelforge.artifacts.local_store import LocalArtifactStore
from modelforge.config import Settings
from modelforge.sandbox.subprocess_exec import _scan_artifacts


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(settings=Settings(artifacts_dir=tmp_path / "artifacts"))


def stage(tmp_path: Path, name: str, content: bytes = b"x") -> SandboxArtifact:
    """在临时目录里造一个「刚跑完、还没搬走」的文件。"""
    path = tmp_path / "staging" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return SandboxArtifact(name=name, path=path, size=len(content), mime=guess_mime(name))


def stored(store: LocalArtifactStore, scope: str) -> list[str]:
    """某个归属下磁盘上实际躺着哪些文件。

    刻意**直接看文件系统**，而不是给 store 加一个 `list_scope()`：
    被删干净了没有，只有看盘才算数。加一个查询方法就等于用一个可能
    自己也有 bug 的东西去验证自己。
    """
    directory = store.root / scope
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# ══════════════════════════════════════════════════════ 搬运


async def test_ingest_moves_files_and_returns_refs(store: LocalArtifactStore, tmp_path: Path):
    files = [stage(tmp_path, "权重对比.png", b"png-bytes"), stage(tmp_path, "数据.csv", b"a,b")]

    refs = await store.ingest("s" * 32, files)

    assert [r.name for r in refs] == ["权重对比.png", "数据.csv"]
    assert [r.kind for r in refs] == ["image", "data"]
    assert [r.size for r in refs] == [9, 3]
    assert all(len(r.id) == 32 for r in refs)


async def test_ingest_removes_the_source(store: LocalArtifactStore, tmp_path: Path):
    """搬走之后源文件必须消失 —— 否则工作目录里会留一份永远不会被清理的副本。

    注意执行器随后会把整个工作目录删掉，所以「源文件还在」本身不致命；
    但 `ArtifactStore.ingest` 的契约里写的是「实现有责任让源文件消失」，
    而契约不测就等于没有。
    """
    item = stage(tmp_path, "图.png", b"x")

    await store.ingest("s" * 32, [item])

    assert not item.path.exists()


async def test_ingested_files_are_renamed_to_uuids(store: LocalArtifactStore, tmp_path: Path):
    """**磁盘上的文件名由我们生成，不是模型给的。**

    这条和下面那条路径穿越测试是一回事的两面：因为落盘名是 uuid，
    所以「模型起的名字」根本没有机会参与文件系统操作。
    模型给的名字只活在 `ArtifactRef.name` 里（显示用）和日志里。
    """
    await store.ingest("s" * 32, [stage(tmp_path, "三种赋权方法的权重对比.png")])

    on_disk = list((store.root / ("s" * 32)).iterdir())

    assert len(on_disk) == 1
    assert "权重" not in on_disk[0].name
    # 后缀要保留，否则取回来时没法猜 MIME，浏览器也不知道怎么显示
    assert on_disk[0].suffix == ".png"


async def test_path_traversal_is_structurally_impossible(store: LocalArtifactStore, tmp_path: Path):
    """模型给的名字可以很恶意，但落盘名是 uuid，所以穿越**没有发生的余地**。

    这里刻意**不去断言「过滤得对不对」** —— 那种测试只能证明
    「我今天想到的这几种攻击被挡住了」，而攻击面是开放的。
    改成断言**结果的位置**：不管输入多离谱，文件都落在自己的 scope 目录里，
    而磁盘上的名字必须严格是「32 位十六进制 + 可选后缀」。

    这才是这条设计的真正内容：**不是把坏输入挡住，是让坏输入没有参与的机会。**
    """
    evil = [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "正常名字但带/斜杠/在里面.png",
        "CON",  # Windows 保留设备名，写文件会失败或写到奇怪的地方
        "名字里带\r\n换行.png",  # 响应头注入的原料
        "名字很长" * 60 + ".png",  # 超过 Windows 的 255 字符上限
    ]

    for index, name in enumerate(evil):
        item = stage(tmp_path, f"staged-{index}", b"x")
        item = item.model_copy(update={"name": name})
        (await store.ingest("s" * 32, [item]))[0]

    on_disk = stored(store, "s" * 32)
    assert len(on_disk) == len(evil)

    for filename in on_disk:
        stem, _, suffix = filename.partition(".")
        assert len(stem) == 32, f"{filename} 的 uuid 部分长度不对"
        assert all(c in "0123456789abcdef" for c in stem), f"{filename} 不是纯十六进制"
        # 后缀要么没有，要么是一个短字母数字串。
        # 注意 `名字很长…` 那一项的长后缀会被 `_safe_suffix` 丢掉 —— 那是对的。
        assert suffix == "" or (len(suffix) <= 8 and suffix.isalnum())

    # 最要紧的一条：**没有任何东西被写到存储根目录外面去**
    assert not (store.root.parent / "etc").exists()
    assert not (store.root.parent / "windows").exists()
    assert sorted(p.name for p in store.root.iterdir()) == ["s" * 32]


async def test_one_failure_does_not_lose_the_whole_batch(store: LocalArtifactStore, tmp_path: Path):
    """三张图里坏了一张，另外两张仍然是好的。

    这条对应 `ingest` 契约里的「某个文件搬运失败时跳过它而不是整批失败」。
    做法是让其中一个源文件在执行到它之前就消失。
    """
    good = stage(tmp_path, "好图.png", b"x")
    missing = stage(tmp_path, "丢了.png", b"x")
    missing.path.unlink()
    other = stage(tmp_path, "另一张.png", b"x")

    refs = await store.ingest("s" * 32, [good, missing, other])

    assert [r.name for r in refs] == ["好图.png", "另一张.png"]


async def test_empty_batch_is_a_no_op(store: LocalArtifactStore):
    """大多数执行不产生产物（只是算个数），这条路径要便宜且安全。"""
    assert await store.ingest("s" * 32, []) == []
    # 连目录都不该建 —— 免得用户目录里堆一堆空目录
    assert not store.root.exists()


# ══════════════════════════════════════════════════════ 定位


async def test_path_for_finds_the_file(store: LocalArtifactStore, tmp_path: Path):
    ref = (await store.ingest("s" * 32, [stage(tmp_path, "图.png", b"png")]))[0]

    found = await store.path_for("s" * 32, ref.id)

    assert found is not None
    assert found.read_bytes() == b"png"


@pytest.mark.parametrize(
    "scope,artifact_id",
    [
        ("../../etc", "a" * 32),  # 越界的 scope
        ("s" * 32, "../../../etc/passwd"),  # 越界的 id
        ("s" * 32, "a" * 31),  # 长度不对
        ("s" * 32, "A" * 32),  # 大写不算（我们只生成小写）
        ("s" * 32, "z" * 32),  # 不是十六进制
        ("", "a" * 32),
        ("s" * 32, ""),
        ("s" * 32, "a" * 32 + "\x00"),  # 空字节截断
    ],
)
async def test_path_for_rejects_anything_that_is_not_exactly_a_uuid(
    store: LocalArtifactStore, scope: str, artifact_id: str
):
    """**校验是「必须完全匹配」，不是「把非法字符删掉」。**

    后者会留下 `....//` 这种删完还成立的怪东西，而前者没有回旋余地。
    这是唯一一个把路径交出去的方法，所以所有关都得在这一层过。
    """
    assert await store.path_for(scope, artifact_id) is None


async def test_path_for_returns_none_for_an_unknown_id(store: LocalArtifactStore):
    assert await store.path_for("s" * 32, "f" * 32) is None


# ══════════════════════════════════════════════════════ 删除与清理


async def test_delete_scope_removes_everything_under_it(store: LocalArtifactStore, tmp_path: Path):
    await store.ingest("s" * 32, [stage(tmp_path, "一.png"), stage(tmp_path, "二.png")])
    await store.ingest("t" * 32, [stage(tmp_path, "别人的.png")])

    removed = await store.delete_scope("s" * 32)

    assert removed == 2
    assert stored(store, "s" * 32) == []
    # 别人的不能被误伤 —— 这是这条测试真正的重点
    assert len(stored(store, "t" * 32)) == 1


async def test_delete_scope_is_safe_on_a_missing_scope(store: LocalArtifactStore):
    assert await store.delete_scope("s" * 32) == 0


async def test_sweep_removes_only_provably_orphaned_scopes(
    store: LocalArtifactStore, tmp_path: Path
):
    """**只删能证明是孤儿的。**

    这个函数跑在用户的数据目录里。漏删的代价是一点磁盘空间，
    错删的代价是用户的图没了 —— 所以它的判据必须保守。
    """
    await store.ingest("a" * 32, [stage(tmp_path, "活的.png")])
    await store.ingest("b" * 32, [stage(tmp_path, "孤儿.png")])

    removed = await store.sweep_orphans({"a" * 32})

    assert removed == 1
    assert len(stored(store, "a" * 32)) == 1
    assert stored(store, "b" * 32) == []


async def test_sweep_on_a_missing_root_is_a_no_op(store: LocalArtifactStore):
    """第一次启动时产物目录还不存在，这不该算错误。"""
    assert await store.sweep_orphans(set()) == 0


async def test_it_satisfies_the_protocol(store: LocalArtifactStore):
    """运行时的协议自检 —— 拦住「实现里漏了一个方法」。

    和 `test_session_store.py` 里那条同样的理由：`@runtime_checkable`
    的 isinstance 不看签名，但能拦住新写一个存储后端时最常见的疏漏。
    """
    assert isinstance(store, ArtifactStore)


# ══════════════════════════════════════════════════════ 扫描


def make_work_dir(tmp_path: Path, files: dict[str, bytes]) -> Path:
    work = tmp_path / "work"
    (work / "artifacts").mkdir(parents=True)
    for name, content in files.items():
        path = work / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return work


def test_scan_finds_files_written_into_the_artifacts_dir(tmp_path: Path):
    work = make_work_dir(tmp_path, {"图.png": b"a" * 10, "数据.csv": b"b" * 5})

    found, notes = _scan_artifacts(work)

    # 用集合比而不是列表：这条测的是「有没有都找到」，不是顺序。
    # 顺序由 `test_scan_order_is_deterministic` 单独盯着。
    assert {f.name for f in found} == {"图.png", "数据.csv"}
    assert notes == []


def test_scan_recurses_into_subdirectories(tmp_path: Path):
    """模型可能自己建子目录（`artifacts/图/权重.png`）。

    名字用相对路径而不是纯文件名，是为了让两张同名文件在界面上
    不会长得一模一样。
    """
    work = make_work_dir(tmp_path, {"图/权重.png": b"x"})

    found, _ = _scan_artifacts(work)

    # 用正斜杠：同样的东西在 Windows 和 Linux 上要显示成一个样子
    assert found[0].name == "图/权重.png"


def test_scan_ignores_files_outside_the_artifacts_dir(tmp_path: Path):
    """**产物收集靠的是约定目录，不是「工作目录里的一切」。**

    不加这条限制的话，`solution.py`、`_stdout.txt` 会被当成产物收走，
    用户的产物面板里会出现一堆没人要的中间文件。
    """
    work = make_work_dir(tmp_path, {"真图.png": b"x"})
    (work / "solution.py").write_text("print(1)")
    (work / "_stdout.txt").write_text("输出")
    (work / "随手写的.txt").write_text("不在 artifacts 里")

    found, _ = _scan_artifacts(work)

    assert [f.name for f in found] == ["真图.png"]


def test_scan_skips_pycache_and_hidden_files(tmp_path: Path):
    """Python 和编辑器自己造的东西不是产物。"""
    work = make_work_dir(
        tmp_path,
        {
            "图.png": b"x",
            "__pycache__/图.cpython-312.pyc": b"junk",
            ".隐藏的.png": b"junk",
        },
    )

    found, _ = _scan_artifacts(work)

    assert [f.name for f in found] == ["图.png"]


def test_scan_skips_empty_files(tmp_path: Path):
    """空文件几乎总是「代码跑到一半崩了」留下的，不是有意的产物。"""
    work = make_work_dir(tmp_path, {"真图.png": b"x", "空的.png": b""})

    found, _ = _scan_artifacts(work)

    assert [f.name for f in found] == ["真图.png"]


def test_scan_is_a_no_op_without_the_directory(tmp_path: Path):
    """绝大多数执行不产出文件（只是算个数）。这条路径要快。"""
    work = tmp_path / "plain"
    work.mkdir()

    assert _scan_artifacts(work) == ([], [])
    assert not (work / "artifacts").exists()


def test_oversized_batches_are_reported_not_silently_dropped(tmp_path: Path):
    """**截断必须说出来。**

    静默丢弃的后果很具体：用户在方案里看到「我画了三张图」，
    界面上只找到两张，然后开始怀疑是软件坏了。
    所以扫描函数返回一个「取舍说明」，执行器会把它拼进 stderr
    —— 那段文字会经由 `format_for_model` 原样到达模型手上。
    """
    from modelforge.sandbox import subprocess_exec

    work = make_work_dir(tmp_path, {f"图{i:02d}.png": b"x" for i in range(25)})

    found, notes = _scan_artifacts(work)

    assert len(found) == subprocess_exec._MAX_ARTIFACTS
    assert len(notes) == 1
    assert "没有被保存" in notes[0]
    assert "5" in notes[0]  # 说清楚丢了多少个


def test_scan_order_is_deterministic(tmp_path: Path):
    """留下的那 20 个必须是可复现的。

    不按文件名排序的话，「留下哪些」取决于文件系统返回目录项的顺序 ——
    而一个随机的好行为比一个确定的坏行为难查得多。
    """
    work = make_work_dir(tmp_path, {f"图{i:02d}.png": b"x" for i in range(25)})

    first = [f.name for f in _scan_artifacts(work)[0]]
    second = [f.name for f in _scan_artifacts(work)[0]]

    assert first == second == [f"图{i:02d}.png" for i in range(20)]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("正常名字.png", "正常名字.png"),
        ("带\r\n换行的.png", "带??换行的.png"),
        ("带\x00空字节.png", "带?空字节.png"),
        ("  两边有空格  ", "两边有空格"),
        # 行尾的换行直接去掉，而不是留两个问号 —— 见 `_sanitize_name` 里
        # 「先 strip 再替换」那条
        ("结尾有换行\n", "结尾有换行"),
        ("全是控制字符\r\n", "全是控制字符"),
        ("", "未命名产物"),
        ("\r\n", "未命名产物"),
        ("   ", "未命名产物"),
    ],
)
def test_control_characters_are_stripped_from_names(raw: str, expected: str):
    """名字最终会进 HTTP 响应头，控制字符必须在源头清掉。

    ⚠️ 这里直接测那个**函数**，而不是走文件系统。原因是这条测试在
    Windows 上根本建不出「名字带 `\\r\\n` 的文件」—— 系统不允许。
    而「测不了」不等于「不需要防」：POSIX 上是建得出来的，
    而 ModelForge 明确支持 Linux/macOS。

    这正是「用一个只在一部分平台上跑得通的测试去覆盖跨平台行为」的陷阱：
    写的时候就该问一句「这个测试在我的机器上通过，说明了什么」。
    直接测函数的话，两种平台上跑的断言是一样的。

    为什么换成 `?` 而不是删掉：删掉会让两个不同的文件名变成同一个，
    而 `?` 让「这里原来有个怪字符」这件事在界面上看得见。
    """
    from modelforge.sandbox.subprocess_exec import _sanitize_name

    assert _sanitize_name(raw) == expected


# ══════════════════════════════════════════════════════ 分类


@pytest.mark.parametrize(
    "name,expected",
    [
        ("图.png", "image"),
        ("图.SVG", "image"),
        ("数据.csv", "data"),
        ("权重.chart.json", "data"),
        ("论文.pdf", "document"),
        ("说明.md", "document"),
        ("没见过的.xyz", "other"),
        ("没有后缀", "other"),
    ],
)
def test_kind_is_guessed_from_the_suffix(name: str, expected: str):
    """分类只是**给界面看的提示**，不是安全判断。

    猜错了的后果是某张图显示成一个下载链接，不会让什么东西变得危险 ——
    所以这里用简单的扩展名匹配就够了，不必去读文件头。
    """
    assert guess_kind(name) == expected


def test_unknown_mime_type_means_download_not_display():
    """猜不出类型时兜底成 `application/octet-stream`。

    兜底成 `text/plain` 的话，一个未知文件会被当成文本渲染进当前页面 ——
    那是「把一个未知的东西塞进渲染上下文」，方向反了。
    让浏览器下载它才是安全的默认。
    """
    assert guess_mime("没见过的.xyz") == "application/octet-stream"
    assert guess_mime("图.png") == "image/png"
