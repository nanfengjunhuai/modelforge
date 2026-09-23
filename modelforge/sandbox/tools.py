"""工具定义与派发。

════════════════════════════════════════════════════════════════════════
这个文件有一半是「写给模型看的」，不是「写给计算机看的」
════════════════════════════════════════════════════════════════════════
`RUN_PYTHON_SPEC` 里的 `description` 会被原样塞进每一次 API 请求，模型就是
靠读它才知道这个工具能干什么、不能干什么。所以它的措辞和代码逻辑一样重要 ——
写错了模型不会报错，它只会**用错**，而且用得很自信。

三个必须写进描述里的事实（都是实现决定的，不是可选的客气话）：
  · 只有 print 出来的东西会回来（返回值不会）
  · 每次调用都是全新的空目录（上次写的文件不在了）
  · plt.show() 没有窗口（Agg 后端）

这三条不写清楚，模型会反复写出「看起来对但拿不到结果」的代码，
然后你会以为是沙箱坏了。

════════════════════════════════════════════════════════════════════════
为什么参数解析放在这一层，而不是 M1 的 Provider 层
════════════════════════════════════════════════════════════════════════
M1 的 `ToolCall.arguments` 刻意保持为**未解析的 JSON 字符串**，理由是：
解析前要做校验和白名单，而过早解析等于把「不可信数据」偷偷变成
「看起来正常的对象」，丢掉了一次安全检查的机会。

这里就是那道检查。顺序是：先确认工具名在白名单里 → 再解析 JSON →
再逐字段验证类型 → 最后才执行。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from modelforge.providers.base import ToolSpec
from modelforge.providers.events import ToolCall, ToolResult
from modelforge.sandbox.base import CodeExecutor, ExecutionResult

logger = logging.getLogger(__name__)

__all__ = ["TOOL_SPECS", "dispatch", "format_for_model"]


# ══════════════════════════════════════════════════════ 工具声明


RUN_PYTHON_SPEC: ToolSpec = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "在隔离环境里执行一段 Python 代码，取回它的输出。\n"
            "\n"
            "环境里预装了 numpy、scipy、matplotlib 和标准库。"
            "不要假设能 pip install 新包，也不要依赖网络访问。\n"
            "\n"
            "三条必须记住的规则：\n"
            "1. **只有 print() 输出的内容会回到你手上**，表达式的值和 return 都不会。"
            "所以每一步中间结果都要显式打印出来，并带上标签（比如 print('均值:', mu)）。\n"
            "2. **每次调用都是一个全新的空目录**，上一次调用写的文件读不到了。"
            "需要跨调用保留的数据，就把它打印出来，从对话里带过去。\n"
            "3. matplotlib 用的是 Agg 后端，**没有窗口，plt.show() 不会显示任何东西**。"
            "要出图就 plt.savefig('chart.png')，然后告诉我文件名。\n"
            "\n"
            "代码写错了没关系，报错信息（traceback）会完整回到你手上，你可以据此改。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "完整的、可以独立运行的 Python 源码。",
                }
            },
            "required": ["code"],
        },
    },
}

TOOL_SPECS: list[ToolSpec] = [RUN_PYTHON_SPEC]

# 合法工具名集合。既用于派发，也用于提前告诉模型「你只有这些工具」。
_TOOL_NAMES = frozenset(spec["function"]["name"] for spec in TOOL_SPECS)


# ══════════════════════════════════════════════════════ 参数校验


class _BadArguments(Exception):
    """参数不合法 —— 是**模型**写错了，不是代码写错了。

    这类错误的特点是：把原因原样告诉模型，它下次就能改对。
    所以要让它流到模型的上下文里，而不是只记进日志。
    """


def _require_code(args: dict[str, Any]) -> str:
    code = args.get("code")
    if not isinstance(code, str):
        raise _BadArguments(f"参数 code 必须是字符串，实际收到 {type(code).__name__}")
    if not code.strip():
        raise _BadArguments("参数 code 是空的，没有可执行的代码")
    return code


# 工具名 → (参数校验与执行)。加新工具时：写一个这样的函数，
# 再往 RUN_PYTHON_SPEC 旁边加一份 ToolSpec，注册到这个表里。
_HANDLERS: dict[str, Callable[[dict[str, Any], CodeExecutor], Awaitable[ExecutionResult]]] = {}


async def _handle_run_python(args: dict[str, Any], executor: CodeExecutor) -> ExecutionResult:
    return await executor.run(_require_code(args))


_HANDLERS["run_python"] = _handle_run_python


# ══════════════════════════════════════════════════════ 派发


async def dispatch(call: ToolCall, *, executor: CodeExecutor) -> ToolResult:
    """执行一个工具调用，**永不抛异常**。

    任何失败都被折叠成一个 `ok=False` 的 ToolResult —— 包括「工具名不认识」
    和「参数是坏 JSON」这类本该是编程错误的情况。理由是：这两件事在真实运行中
    都会发生（模型幻觉出一个工具名、模型生成的 JSON 被截断），
    而且**模型有能力根据错误信息自我修复**。把它变成异常会直接掐断整条流，
    用户看到的是「出错了」，而本来只需要让模型重试一次。

    Args:
        call: M1 拼装好的工具调用，`arguments` 是未解析的 JSON 字符串。
        executor: 代码执行后端。
    """
    started = time.monotonic()

    def fail(message: str) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=False,
            error=message,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ① 工具名白名单。必须在解析参数之前 —— 对不认识的工具，
    #    连它的参数长什么样都不该去碰。
    if call.name not in _TOOL_NAMES:
        logger.warning("模型请求了不存在的工具：%r", call.name)
        return fail(f"没有名为 {call.name!r} 的工具。可用的工具：{sorted(_TOOL_NAMES)}")

    # ② 解析参数。M1 特意留给这一层做的事。
    try:
        args = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        # 最常见的原因是模型输出被 max_tokens 截断，JSON 只写了一半。
        logger.warning("工具参数不是合法 JSON：%s", exc)
        return fail(f"参数不是合法的 JSON：{exc}\n收到的原文：{call.arguments[:200]}")

    if not isinstance(args, dict):
        return fail(f"参数必须是一个 JSON 对象，实际收到 {type(args).__name__}")

    # ③ 逐字段校验 + 执行
    try:
        result = await _HANDLERS[call.name](args, executor)
    except _BadArguments as exc:
        return fail(str(exc))
    except Exception as exc:
        # 执行器自己炸了。契约要求它不抛异常，但这里是最后一道防线 ——
        # 一道没被遵守的契约不该让整条对话流断掉。
        logger.exception("执行工具 %s 时发生未预期的异常", call.name)
        return fail(f"执行工具时发生内部错误：{type(exc).__name__}: {exc}")

    return ToolResult(
        call_id=call.id,
        name=call.name,
        # 「成功」的定义：代码真的跑起来了，而且退出码是 0。
        # 注意 stderr 有内容**不算**失败 —— 很多库会往 stderr 写警告。
        ok=result.launched and result.exit_code == 0,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        error=result.error,
    )


# ══════════════════════════════════════════════════════ 回填给模型


def format_for_model(result: ToolResult) -> str:
    """把执行结果渲染成一段**给模型读**的文本。

    这是整个 M3 里最容易被忽视、但对效果影响最大的一段代码 ——
    它是模型唯一的「眼睛」。模型看不到 ToolResult 对象，只看得到这串字符。

    几条刻意的取舍：

      · **成功的输出不加任何包装。** 加了「执行成功：」之类的前缀只是浪费
        token，模型从上下文就知道这是工具返回。
      · **报错时把 stderr 完整给出。** traceback 是模型自我修复的全部依据，
        截断它等于砍掉模型的调试能力。
      · **超时要说清楚是超时，而不是「失败」。** 这两者对模型意味着不同的
        修法：超时该去查死循环或降低规模，失败该去看具体的报错行。
      · **我们这边的错误（`error` 字段）单独一条分支。** 这种情况模型改代码
        也没用，得让它知道「换个思路，别重试这个工具了」。
    """
    if result.error:
        # 沙箱层面的问题：解释器不存在、临时目录建不了……
        # 明确告诉模型「这不是你的代码的问题」，免得它反复改代码重试。
        return f"【沙箱未能执行代码】{result.error}\n这不是你的代码的问题，不要靠修改代码来重试。"

    parts: list[str] = []

    if result.stdout.strip():
        parts.append(result.stdout.rstrip())
    else:
        # 空输出是个高频困惑点 —— 模型经常忘了 print，然后对着空白发呆。
        parts.append("（没有任何输出。记得用 print() 把结果打出来。）")

    if result.stderr.strip():
        label = "运行出错（stderr）" if not result.ok else "stderr 输出（有内容但不影响执行成功）"
        parts.append(f"--- {label} ---\n{result.stderr.rstrip()}")

    if result.timed_out:
        parts.append("【执行超时被强制终止】检查是否有死循环，或者把问题规模调小一点。")
    elif result.exit_code not in (0, None):
        parts.append(f"【进程以非零退出码 {result.exit_code} 结束】")

    return "\n\n".join(parts)
