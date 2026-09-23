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
from modelforge.providers.events import DecisionRequest, ToolCall, ToolResult
from modelforge.sandbox.base import CodeExecutor, ExecutionResult

logger = logging.getLogger(__name__)

__all__ = [
    "ASK_USER_NAME",
    "ASK_USER_SPEC",
    "TOOL_SPECS",
    "BadArguments",
    "build_decision_request",
    "dispatch",
    "format_for_model",
]


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
            "【三条必须记住的规则】\n"
            "1. **只有 print() 输出的内容会回到你手上**，表达式的值和 return 都不会。"
            "所以每一步中间结果都要显式打印出来，并带上标签（比如 print('均值:', mu)）。\n"
            "2. **每次调用都是一个全新的空目录**，上一次调用写的文件读不到了。"
            "需要跨调用保留的数据，就把它打印出来，从对话里带过去。\n"
            "3. matplotlib 用的是 Agg 后端，**没有窗口，plt.show() 不会显示任何东西**。"
            "出图看下一节。\n"
            "\n"
            "【出图：用户要的是能直接贴进论文的图】\n"
            "\n"
            "工作目录里已经放好了一个绘图模块，**出图一律用它**：\n"
            "\n"
            "    import modelforge_plot as mp\n"
            "\n"
            "    fig, ax = plt.subplots()\n"
            "    ...画你的图...\n"
            "    mp.save(fig, \"三种赋权方法的权重对比\")\n"
            "\n"
            "**不要用 plt.savefig()。** 自己存的文件会落在工作目录根下，"
            "而那个目录在这次执行结束时会被整个删掉 —— 图就没了，用户什么也看不到。"
            "只有存进 `artifacts/` 目录（`mp.save()` 替你做了这件事）的文件才会被保留。\n"
            "\n"
            "`mp.save()` 会自动做好论文需要的一切：\n"
            "  · 中文黑体、去掉顶部和右侧边框、淡网格、无框图例 —— 科研风格已经默认生效，"
            "你不用手动调 rcParams\n"
            "  · 同时输出 300 dpi 的 PNG（贴 Word 用）和矢量 PDF（投期刊用，字体已嵌入）\n"
            "  · 颜色自动取设计好的分类色板，**不要自己指定十六进制色值**\n"
            "\n"
            "**多条曲线时，画完调一次 `mp.style_lines(ax)`。** 它会按系列分配不同的"
            "线型和标记形状 —— 数模论文经常被黑白打印，那时颜色区分度归零，"
            "只有线型和标记还能把曲线分开。\n"
            "\n"
            "**能出图的时候一定要出图。** 数模论文里图是最要紧的产出之一，"
            "把结果画出来比在正文里堆一段数字有用得多。\n"
            "\n"
            "【顺手把图的数据也交出来】\n"
            "\n"
            "`mp.save()` 还有一个可选的 `chart=` 参数，用来存**这张图背后的数据**，"
            "用户可以在界面上悬停查看具体数值：\n"
            "\n"
            "    mp.save(fig, \"权重对比\", chart={\n"
            "        \"kind\": \"bar\",\n"
            "        \"series\": [{\"name\": \"熵权法\", \"data\": w} for ...],\n"
            "        \"categories\": labels,\n"
            "    })\n"
            "\n"
            "⚠️ `data` 里必须是你**算出来的真实数组**，不能凭印象重新打一遍数字。"
            "这个参数存在的全部意义就是让图和数字同源，手打就失去意义了。"
            "写歪了不影响图片本身，只会少一份可交互数据 —— 但那就白画了。\n"
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

ASK_USER_NAME = "ask_user"

ASK_USER_SPEC: ToolSpec = {
    "type": "function",
    "function": {
        "name": ASK_USER_NAME,
        "description": (
            "把决策权交给用户。调用它会**立刻暂停本次生成**，"
            "等用户在界面上点选之后才继续。\n"
            "\n"
            "**什么时候该用它** —— 走到一个会影响后续全部工作的分岔口，"
            "几条路各有优劣、没有客观最优解的时候。典型场景：\n"
            "  · 选模型：熵权法客观但要求数据完整，AHP 能纳入主观判断但要用户给判断矩阵\n"
            "  · 数据口径：异常值剔除策略会直接改变结论\n"
            "  · 参数取值：用户手里可能有赛题之外的信息（队伍擅长什么、有没有现成软件）\n"
            "\n"
            "**什么时候不该用它**：\n"
            "  · 有客观答案的自己去算，别问\n"
            "  · **还没做功课的时候不要问。** 先把几个方案各自跑出数字，"
            "带着具体结果来问 —— 「A 法算出来 0.42，B 法 0.38，差别主要在样本量」"
            "是真的在帮忙；空着手问「你想怎么办」只是把工作推回给用户。\n"
            "  · 一次只问一件，不要把所有问题攒成一张问卷\n"
            "\n"
            "**options 每一项都要写出「选它的代价」**，不能只写好处 ——"
            "用户是在做取舍，不是在挑一个全面更好的。"
            "「熵权法（客观，但要求数据完整）」比光写「熵权法」有用得多。\n"
            "\n"
            "**调用它时这一轮不要同时调用别的工具，让它独占一轮。**"
            "这样用户能立刻看到问题，不用先等一段代码跑完。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "要问用户的问题，一句话说清楚**在什么之间做选择**。"
                        "先说结论再说理由，不要写成一段背景介绍。"
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "description": (
                        "候选方案，每项一句话，格式是「方案名（代价或适用条件）」。"
                        "至少两个 —— 只有一个选项不叫选择。"
                    ),
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": (
                        "是否允许用户用自由文本补充。默认为真。"
                        "只有在选项已经穷尽了可能性时才设为假。"
                    ),
                },
            },
            "required": ["question", "options"],
        },
    },
}

# ⚠️ 注意这里**只有 run_python**。
#
# ask_user 不在 TOOL_SPECS 里，是刻意的：TOOL_SPECS 是**无状态端点**
# (`POST /api/chat/stream`) 用的工具集。那个端点没有会话、没有事件日志、
# 也没有任何地方能让用户回答 —— 给了它 ask_user 只会得到一个死胡同：
# 模型停下来问，然后永远等不到答案。
#
# 有会话的端点显式地传 `TOOL_SPECS + [ASK_USER_SPEC]`。
TOOL_SPECS: list[ToolSpec] = [RUN_PYTHON_SPEC]

# 合法工具名集合。既用于派发，也用于告诉模型「你只有这些工具」
# —— 模型幻觉出一个不存在的工具时，报错信息会列出这个集合。
#
# ask_user 必须在里面，哪怕它永远不会走到派发那一步：
# 不在的话，模型会被告知「没有 ask_user 这个工具」，而它明明在工具列表里看到了。
_TOOL_NAMES = frozenset({RUN_PYTHON_SPEC["function"]["name"], ASK_USER_NAME})


# ══════════════════════════════════════════════════════ 参数校验


class BadArguments(Exception):
    """工具参数不合法 —— 是**模型**写错了，不是代码写错了。

    这类错误的特点是：把原因原样告诉模型，它下次就能改对。
    所以要让它流到模型的上下文里，而不是只记进日志。

    名字没有下划线，因为 M4 起 Agent 循环也要 catch 它（`ask_user` 的参数
    在循环里就解析了，不走 `dispatch`）。这个类名是模块对外契约的一部分。
    """


def _require_code(args: dict[str, Any]) -> str:
    code = args.get("code")
    if not isinstance(code, str):
        raise BadArguments(f"参数 code 必须是字符串，实际收到 {type(code).__name__}")
    if not code.strip():
        raise BadArguments("参数 code 是空的，没有可执行的代码")
    return code


# 工具名 → (参数校验与执行)。加新工具时：写一个这样的函数，
# 再往 RUN_PYTHON_SPEC 旁边加一份 ToolSpec，注册到这个表里。
#
# 第三个参数是 `scope`（产物归属，M5 加）。它从 `dispatch` 一路传下来，
# 最终交给 `executor.run()` —— 无状态端点传 None，表示「产物不用留」。
_Handler = Callable[[dict[str, Any], CodeExecutor, "str | None"], Awaitable[ExecutionResult]]
_HANDLERS: dict[str, _Handler] = {}


async def _handle_run_python(
    args: dict[str, Any], executor: CodeExecutor, scope: str | None
) -> ExecutionResult:
    return await executor.run(_require_code(args), scope=scope)


_HANDLERS["run_python"] = _handle_run_python


async def _handle_ask_user(
    args: dict[str, Any], executor: CodeExecutor, scope: str | None
) -> ExecutionResult:
    """永远不会被正常调用到的处理器。

    存在两个理由，都不是「以防万一」这么含糊：

      ① `_HANDLERS[name]` 的查表在 `dispatch` 里。如果 ask_user 没有注册，
         某天有人绕过循环的拦截逻辑直接调 dispatch，会撞一个 KeyError，
         被兜成一句「执行工具时发生内部错误：KeyError」—— 完全找不到北。
      ② 更常见的：**忘了在循环里拦截它**。那时这行报错会直白地说出问题所在，
         而不是让一个「工具执行失败」的假象把人引向沙箱。

    「让失败自己开口说话」是这个文件里反复出现的做法 —— 见 `format_for_model`
    里那句「这不是你的代码的问题，不要靠修改代码来重试」。
    """
    raise BadArguments(
        "ask_user 不是一个能被执行的工具，它由 Agent 循环拦截并转成一次用户决策。"
        "走到这里说明循环里少了拦截逻辑。"
    )


_HANDLERS[ASK_USER_NAME] = _handle_ask_user


# ══════════════════════════════════════════════════════ ask_user 的参数


def build_decision_request(call: ToolCall) -> DecisionRequest:
    """把一次 `ask_user` 调用翻译成一个 `DecisionRequest`，顺带校验参数。

    为什么校验放在这里、而不是在 Agent 循环里？因为**这是参数校验**，
    和 `_require_code` 是同一件事，应该和它待在一起。循环只该做「要不要中断」
    这个判断，不该知道 question 是不是字符串。

    Raises:
        BadArguments: 模型把参数写歪了。调用方（Agent 循环）会把它变成一条
            `ok=False` 的 ToolResult 回填给模型 —— 和 `run_python` 参数写错
            时的待遇完全一样，因为这是同一类错误：**模型能自己修**。
    """
    try:
        args = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        raise BadArguments(
            f"参数不是合法的 JSON：{exc}\n收到的原文：{call.arguments[:200]}"
        ) from exc

    if not isinstance(args, dict):
        raise BadArguments(f"参数必须是一个 JSON 对象，实际收到 {type(args).__name__}")

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        raise BadArguments("参数 question 必须是非空字符串")

    raw_options = args.get("options")
    if not isinstance(raw_options, list):
        raise BadArguments(f"参数 options 必须是数组，实际收到 {type(raw_options).__name__}")

    # 只留下非空字符串。
    #
    # ⚠️ 注意这里**没有**强制「至少两个选项」，尽管 JSON Schema 里写的是
    # `minItems: 2`。声明和校验故意不一致：
    #
    #   · 声明是**引导** —— 告诉模型「正常情况该给几个」，影响它怎么写。
    #   · 校验是**兜底** —— 只拦真正会让下游出错的东西。
    #
    # 一个只有单选项的「确认一下」是合法的交互（配上自由文本输入就够了），
    # 为此把整轮打回去让模型重写，代价大于收益。真正的错误是 options 根本
    # 不是数组、或者里面全是空字符串 —— 那才拦。
    options = [opt.strip() for opt in raw_options if isinstance(opt, str) and opt.strip()]
    if not options:
        raise BadArguments("参数 options 里没有任何有效的字符串选项")

    allow_free_text = args.get("allow_free_text", True)
    if not isinstance(allow_free_text, bool):
        raise BadArguments("参数 allow_free_text 必须是布尔值")

    return DecisionRequest(
        call_id=call.id,
        question=question.strip(),
        options=options,
        allow_free_text=allow_free_text,
    )


# ══════════════════════════════════════════════════════ 派发


async def dispatch(
    call: ToolCall, *, executor: CodeExecutor, scope: str | None = None
) -> ToolResult:
    """执行一个工具调用，**永不抛异常**。

    任何失败都被折叠成一个 `ok=False` 的 ToolResult —— 包括「工具名不认识」
    和「参数是坏 JSON」这类本该是编程错误的情况。理由是：这两件事在真实运行中
    都会发生（模型幻觉出一个工具名、模型生成的 JSON 被截断），
    而且**模型有能力根据错误信息自我修复**。把它变成异常会直接掐断整条流，
    用户看到的是「出错了」，而本来只需要让模型重试一次。

    Args:
        call: M1 拼装好的工具调用，`arguments` 是未解析的 JSON 字符串。
        executor: 代码执行后端。
        scope: 这次调用产出的文件归属谁（会话 id）。`None` 表示不保留产物
            —— 无状态端点走的就是这条。定义见 `CodeExecutor.run`。
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
        result = await _HANDLERS[call.name](args, executor, scope)
    except BadArguments as exc:
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
        # 产物引用直接带上去 —— 它会被原样存进事件日志，所以刷新页面之后
        # 图还在。见 `ToolResult.artifacts` 的说明。
        artifacts=result.artifacts,
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

    if result.artifacts:
        # 把产物列给模型看，有两个具体用处：
        #   ① 它能照着这些文件名回答用户（「我画了一张《权重对比》，你可以点开看」），
        #      而不是含糊地说「图已经生成了」；
        #   ② 它知道自己这次确实产出了东西，下一轮就不会重复画同一张图。
        #
        # 这段文字会被**冻结**进事件日志（见 `loop._tool_message`），
        # 所以改措辞不会追溯性地改写历史会话里模型当时看到的内容。
        lines = [
            f"  · {item.name}（{item.kind}，{_human_size(item.size)}）"
            for item in result.artifacts
        ]
        parts.append(
            "【已保存的产物】下面这些文件已经存好，用户可以在界面上看到并下载。"
            "你可以在回答里提到它们：\n" + "\n".join(lines)
        )

    return "\n\n".join(parts)


def _human_size(size: int) -> str:
    """把字节数变成人看得懂的写法。模型和用户都会读这段文字。"""
    if size < 1024:
        return f"{size} 字节"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
