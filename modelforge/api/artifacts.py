"""产物端点 —— 把沙箱里生成的图取回给浏览器。

════════════════════════════════════════════════════════════════════════
为什么是一个新模块，而不是塞进 sessions.py
════════════════════════════════════════════════════════════════════════
`sessions.py` 已经有四百多行，管的是「会话的生命周期」：建、删、发消息、
提交决策、恢复。而产物是一条**旁路** —— 它不改变会话状态，只是把文件取回去。

两者共用的只有「会话存不存在」这一件事，而那件事这里不需要（见下面
「为什么不做 404 检查」）。

════════════════════════════════════════════════════════════════════════
为什么不用 StaticFiles 挂一个目录出去
════════════════════════════════════════════════════════════════════════
看起来更省事：`app.mount("/artifacts", StaticFiles(directory=...))`，一行搞定。
但那样就把**整个产物根目录**暴露成了可枚举的静态资源：

  · 任何人猜到路径就能读别人的图 —— 现在只有一个用户，但 M6 要加鉴权，
    而鉴权加在 `mount` 上的做法是「改用中间件拦路径」，比拦一个路由难得多；
  · 静态目录服务会做目录列表（取决于实现），把「有哪些会话」这个信息也漏出去；
  · 文件名是 uuid，看起来猜不到 —— 但那是**靠不可预测性当安全措施**，
    而不是靠访问控制。前者一旦泄漏就完全失效，后者不会。

所以走一个显式路由，将来加鉴权就是在它上面加一层依赖，改动面是一个函数。

════════════════════════════════════════════════════════════════════════
为什么不做「这个会话存不存在」的校验
════════════════════════════════════════════════════════════════════════
因为**查不到就是 404**，两种 404 对客户端没有区别，对我们却多一次数据库读。

产物能不能被取到，取决于三件事同时成立：id 形态合法、文件在盘上、
以及路径没有跑出存储根目录 —— 这三条都由
`ArtifactStore.path_for()` 一个函数负责（校验和拼路径放在一起，
是为了让它们不可能只改一半）。会话删了的话，它的产物目录也跟着删了
（见 `DELETE /api/sessions/{id}`），所以那一条自动被覆盖。

════════════════════════════════════════════════════════════════════════
中文文件名与跨域下载
════════════════════════════════════════════════════════════════════════
两个容易踩的点：

① **`<a download>` 属性在跨域时会被浏览器忽略。**
   前端跑在 :3000，后端在 :8000 —— 这是个跨域请求。所以「点了链接要下载
   而不是跳转」这件事**只能靠服务端的 `Content-Disposition`**，
   前端那个属性是个摆设。别把它删了，也别指望它。
   （HTML 规范：`download` 只对同源 URL 生效。）

② **中文文件名要走 RFC 5987 的 `filename*=utf-8''…` 形式。**
   好在这一层不用自己实现 —— `FileResponse` 在检测到文件名含非 ASCII 时
   会自动换成那种形式。但它**顺带**也是一道安全边界：正因为
   `quote()` 会改掉 `"`、`\\r`、`\\n` 这些字符，响应头注入才不成立。
   所以别自己拼 `Content-Disposition` 字符串。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

# ⚠️ 两个存储都用**模块属性**取（`pkg.get_store()`），不要写成
# `from modelforge.artifacts import get_store`。
#
# 后者是「导入时把函数对象绑进本模块的命名空间」，于是 `tests/conftest.py`
# 里 `monkeypatch.setattr(包, "get_store", ...)` 就**不会生效** ——
# 测试会悄悄地去读写真实目录，而测试依然全绿。
# 这和 M4 那个「测试替身存引用而不是快照」是同一类 bug：绑定时机太早。
#
# `main.py` 里早就写着同样的注释（「运行时去那个模块上取这个名字」），
# 这里遵守同一条规矩。
from modelforge import artifacts as artifact_store_pkg
from modelforge.api import sessions as sessions_api
from modelforge.artifacts.base import guess_mime
from modelforge.sessions.project import find_artifact

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{session_id}/{artifact_id}")
async def get_artifact(
    session_id: str,
    artifact_id: str,
    download: bool = Query(False, description="true 时下载，false 时内联显示（给 <img> 用）"),
) -> FileResponse:
    """取一个产物。

    默认**内联**返回，这样前端可以直接 `<img src="...">` 显示缩略图。
    带 `?download=1` 时改成附件，浏览器会弹保存框。

    为什么要两个模式而不是统一成附件：同一个图片 URL 既要用在 `<img>` 里
    （必须内联，否则显示不出来），又要用在「下载」链接里（必须附件，
    否则会跳转到一个新标签页）。一个 URL 干两件事的时候，
    内容协商只会让两边都不好用。
    """
    store = artifact_store_pkg.get_store()
    path = await store.path_for(session_id, artifact_id)
    if path is None:
        raise HTTPException(status_code=404, detail="找不到这个产物")

    # 文件名从**日志**里查，而不是从磁盘上那个 uuid 文件名推。
    # 磁盘上叫 `3f2a…c1.png`，用户想拿到的是「三种赋权方法的权重对比.png」。
    # 人看的名字只存在日志里 —— 这正是「日志是唯一真相源」的顺带好处：
    # 不需要为它单独存一份。
    display_name = await _display_name(session_id, artifact_id) or path.name

    return FileResponse(
        path,
        media_type=guess_mime(display_name),
        # filename 只在下载时给。内联时给了的话，有些浏览器会转而下载 ——
        # 那 `<img>` 就显示不出来了。
        filename=display_name if download else None,
        content_disposition_type="attachment",
        headers=_security_headers(display_name),
    )


async def _display_name(session_id: str, artifact_id: str) -> str | None:
    """回日志里查出这个产物当初叫什么名字。查不到返回 None。

    查不到是**允许**的（降级成用磁盘上的 uuid 文件名），不报错 ——
    文件名难看总好过整张图取不回来。
    """
    try:
        events = await sessions_api.get_store().events(session_id)
    except Exception:
        logger.warning("查产物文件名时读日志失败，降级用磁盘文件名", exc_info=True)
        return None
    ref = find_artifact(events, artifact_id)
    return ref.name if ref else None


def _security_headers(display_name: str) -> dict[str, str]:
    """给产物响应加几个安全头。

    `X-Content-Type-Options: nosniff` 是**必须**的：没有它，浏览器可能
    无视我们给的 MIME 去猜类型。模型能往 `artifacts/` 里写任意扩展名的文件，
    而这个响应又是从**后端自己的源**发出的 —— 让浏览器对这里的内容
    自由发挥，等于把「后端源」的信任级别借给了模型生成的文件。

    SVG 再额外加一条 CSP：SVG 里面可以带 `<script>`，直接在新标签页打开时
    会执行。前端把它放进 `<img>` 是安全的（`<img>` 里的 SVG 不跑脚本），
    但用户完全可能右键「在新标签页中打开」—— 那就跑了。禁掉脚本，
    这一类就没得玩了。
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        # 产物是不可变的（文件名带 uuid，内容不会变），可以放心让浏览器缓存。
        # 不缓存的话，每次翻聊天记录都会把图重新拉一遍。
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if display_name.lower().endswith(".svg"):
        headers["Content-Security-Policy"] = "script-src 'none'; object-src 'none'"
    return headers
