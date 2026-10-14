from typing import Any, Dict, List

from agent.state import AgentState
from agent.utils.trace import create_trace_event
from services.llm_service import (
    assistant_turn,
    call_llm_with_tools
)
from tools.tool_registry import (
    TOOL_NAMES,
    TOOL_SCHEMAS,
    execute_tool_call,
    parse_arguments
)


# ==========================================
# V4.5 配置
# ==========================================

# 一个任务内最多几轮工具调用
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = (
    "你是 DataMind 的数据分析执行器。\n\n"
    "工具使用原则：\n"
    "1. 需要数据时必须调用工具，不要凭印象写数字。\n"
    "2. 用户问的指标如果在数据集里没有对应字段，"
    "要明确指出缺少哪个字段，"
    "不要用其他字段凑出一个定义。\n"
    "3. 优先使用专用工具："
    "看数据结构用 describe_dataset，"
    "分组汇通用 aggregate_metric，"
    "分布统计用 describe_metric。\n"
    "4. 只有当专用工具覆盖不了的时候才用 run_python"
    "（例如多字段四则运算、透视、复杂过滤）。"
    "需要按时间维度聚合时不需要写代码，"
    "直接用 aggregate_metric 的 time_granularity。\n"
    "5. 不要为了交叉验证重复计算同一个指标，"
    "也不要重复执行同一段代码。\n"
    "6. 可以一次调用多个工具。\n"
    "7. 拿到足够的数据之后，用中文简要说明你算出了什么。"
)


def clean_code(code: str) -> str:
    """
    清理模型偶尔返回的 Markdown 代码块。

    注意：

    V4.5 起 Executor 自己不再生成代码，
    但 error_handler 仍然会 import 这个函数，所以保留。
    """

    if not code:

        return ""

    import re

    code = code.strip()

    code = re.sub(
        r"^```(?:python)?\s*",
        "",
        code,
        flags=re.IGNORECASE
    )

    code = re.sub(
        r"\s*```$",
        "",
        code
    )

    return code.strip()


def build_task_prompt(
    task_description: str,
    dataset_info: str,
    pending_code: str = "",
    knowledge: str = "",
    health: str = ""
) -> str:
    """
    构造这一轮任务的提示词。
    """

    knowledge_block = (
        knowledge.strip() + "\n\n"
        if knowledge and knowledge.strip()
        else ""
    )

    health_block = (
        health.strip() + "\n\n"
        if health and health.strip()
        else ""
    )

    prompt = f"""
当前任务：

{task_description}

====================
数据集信息
====================

{dataset_info}

{health_block}{knowledge_block}====================
要求
====================

1. 需要数据时必须调用工具，不要凭印象写数字。
2. 只能使用数据集里真实存在的字段。
   如果用户问的指标在数据集里没有对应字段，
   要明确指出缺少哪个字段，
   不要用其他字段凑出一个定义。
2b. 遵守「数据体检结论」里标为【必须规避】的限制：
    例如某个期间不完整时，不要拿它和完整期间做比较。
3. 如果是趋势或时间相关的问题，先把日期字段按合适的粒度聚合。
4. 拿到足够的数据之后，用中文简要说明结果。
"""

    if pending_code:

        prompt += f"""
====================
上一次尝试失败了
====================

这是修复专家给出的参考代码。
你可以把它作为 run_python 的 code 参数直接执行，
也可以改用更合适的工具：

{pending_code}
"""

    return prompt


def describe_tool_call(
    name: str,
    arguments: Dict[str, Any]
) -> str:
    """
    把一次工具调用渲染成人能读的一行。
    """

    if not arguments:

        return f"{name}()"

    parts = ", ".join(
        f"{key}={value}"
        for key, value in arguments.items()
    )

    return f"{name}({parts})"


def run_tool_loop(
    file_path: str,
    task_description: str,
    dataset_info: str,
    pending_code: str,
    agent_trace: List[Dict[str, Any]],
    knowledge: str = "",
    health: str = ""
) -> Dict[str, Any]:
    """
    跑一次工具调用循环。

    返回：

        ok            是否拿到可用结果
        final_text    模型最后的说明
        observations  所有成功工具结果的拼接（下游核对数值用的就是它）
        tool_calls    调用记录
        rounds        实际轮数
        error         失败原因
    """

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": build_task_prompt(
                task_description,
                dataset_info,
                pending_code or "",
                knowledge or "",
                health or ""
            )
        }
    ]

    tool_call_log: List[Dict[str, Any]] = []
    observation_parts: List[str] = []

    successful = 0
    last_error = None

    for round_index in range(1, MAX_TOOL_ROUNDS + 1):

        try:

            message = call_llm_with_tools(
                messages,
                TOOL_SCHEMAS
            )

        except Exception as exc:  # noqa: BLE001

            last_error = {
                "type": type(exc).__name__,
                "message": str(exc)
            }

            break

        calls = list(
            getattr(message, "tool_calls", None) or []
        )

        # ==================================
        # 模型不再调用工具 -> 收尾
        # ==================================

        if not calls:

            final_text = (message.content or "").strip()

            return {
                "ok": successful > 0,
                "final_text": final_text,
                "observations": "\n\n".join(observation_parts),
                "tool_calls": tool_call_log,
                "rounds": round_index,
                "error": None if successful else (
                    last_error or {
                        "type": "NoToolCall",
                        "message": "模型没有调用任何工具就结束了"
                    }
                )
            }

        # ==================================
        # 把模型的这一轮回复回填
        # （包含 tool_calls 和 reasoning_content）
        # ==================================

        messages.append(assistant_turn(message))

        # ==================================
        # 执行每一个工具调用
        #
        # 注意：每一个 tool_call_id 都必须有对应回复，
        # 少一个下一轮就会报错。
        # ==================================

        for call in calls:

            name = call.function.name

            arguments = parse_arguments(
                call.function.arguments
            )

            agent_trace.append(
                create_trace_event(
                    event="tool_call",

                    node="executor",

                    tool=name,

                    status="running",

                    message=(
                        "调用 "
                        + describe_tool_call(name, arguments)
                    ),

                    metadata={"arguments": arguments},

                    display={
                        "title": f"调用工具：{name}",
                        "icon": "calculator",
                        "level": "info",
                        "visible": True
                    }
                )
            )

            outcome = execute_tool_call(
                name,
                arguments,
                file_path
            )

            succeeded = bool(outcome.get("ok"))

            tool_call_log.append({
                "name": name,
                "arguments": arguments,
                "ok": succeeded,
                "summary": outcome.get("summary", "")
            })

            if succeeded:

                successful += 1

                observation_parts.append(
                    outcome.get("observation", "")
                )

            else:

                last_error = outcome.get("error") or {
                    "type": "ToolError",
                    "message": outcome.get("summary", "")
                }

            agent_trace.append(
                create_trace_event(
                    event=(
                        "tool_result"
                        if succeeded
                        else "tool_error"
                    ),

                    node="executor",

                    tool=name,

                    status=(
                        "success"
                        if succeeded
                        else "error"
                    ),

                    message=outcome.get("summary", ""),

                    error=None if succeeded else last_error,

                    display={
                        "title": (
                            f"{name} 完成"
                            if succeeded
                            else f"{name} 失败"
                        ),
                        "icon": "check" if succeeded else "alert",
                        "level": (
                            "success"
                            if succeeded
                            else "error"
                        ),
                        "visible": True
                    }
                )
            )

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": outcome.get("observation", "")
            })

    # ======================================
    # 轮次用尽
    # ======================================

    return {
        "ok": False,
        "final_text": "",
        "observations": "\n\n".join(observation_parts),
        "tool_calls": tool_call_log,
        "rounds": MAX_TOOL_ROUNDS,
        "error": last_error or {
            "type": "ToolLoopExhausted",
            "message": (
                f"{MAX_TOOL_ROUNDS} 轮之内没有拿到可用结果"
            )
        }
    }


def executor(
    state: AgentState
):
    """
    DataMind Executor Node（V4.5：原生 Tool Calling）。

    与旧版的区别：

    旧版：

        Planner 给出一个工具名字符串 ->
        Executor 让模型针对它生成一段 Python 代码 ->
        exec 执行。

        模型并没有「决定调用哪个工具」，
        它只是写了一行代码，我们照着跑。

    现在：

        用 tools 协议声明工具 ->
        模型返回结构化的 tool_calls ->
        我们执行对应的函数 ->
        结果作为 role="tool" 回填 ->
        循环直到模型不再调用工具。

    自愈也变得更自然：

        工具报错时把错误原样回填，模型自己改参数重试；
        只有在整轮都拿不到结果时，才交给 error_handler
        走外层的任务级重试。

    注意：

    - 不再使用 exec 作为常规路径（只有 run_python 会用到）
    - pending_code（error_handler 的修复建议）会作为提示交给模型
    """

    current_step = state["current_step"]

    plan = state["plan"]

    agent_trace = list(
        state.get("agent_trace") or []
    )

    code_history = list(
        state.get("code_history") or []
    )

    # ==========================================
    # 防止任务越界
    # ==========================================

    if current_step >= len(plan):

        return {
            "execution_status": "idle",
            "error": None,
            "agent_trace": agent_trace
        }

    current_task = plan[current_step]

    task_description = current_task["task"]

    dataset_info = state["dataset_info"]

    file_path = state["file_path"]

    pending_code = state.get("pending_code")

    # V4.6：业务口径，注入给模型决定工具参数
    knowledge_context = (
        state.get("knowledge_context") or ""
    )

    # V4.11：数据体检结论
    health_context = (
        state.get("health_context") or ""
    )

    current_task_retry_count = state.get(
        "current_task_retry_count",
        0
    )

    step_number = current_step + 1

    agent_trace.append(
        create_trace_event(
            event="analysis_started",

            node="executor",

            tool="tool_calling",

            status="running",

            message=f"开始执行任务：{task_description}",

            metadata={"available_tools": TOOL_NAMES},

            display={
                "title": "执行数据分析",
                "icon": "calculator",
                "level": "info",
                "visible": True
            }
        )
    )

    result = run_tool_loop(
        file_path=file_path,
        task_description=task_description,
        dataset_info=dataset_info,
        pending_code=pending_code,
        agent_trace=agent_trace,
        knowledge=knowledge_context,
        health=health_context
    )

    # ==========================================
    # 成功
    # ==========================================

    if result["ok"]:

        execution_results = list(
            state.get("execution_results") or []
        )

        execution_results.append({
            "step": step_number,

            "task": task_description,

            "tool": "tool_calling",

            "status": "success",

            "repair_count": current_task_retry_count,

            "auto_repaired": current_task_retry_count > 0,

            # 下面两个字段是旧版留下的，保留是为了不破坏
            # Reporter / 核对器的数据结构
            "original_code": None,
            "final_code": None,

            "repair_history": [],

            "data": {
                # 核对器就是从这两个字段里提取「真实存在的数值」，
                # 所以工具结果必须原样放进来
                "result": (
                    result["observations"]
                    or result["final_text"]
                ),

                "output": result["final_text"],

                "tool_calls": result["tool_calls"]
            },

            "metadata": {
                "tool": "tool_calling",
                "rounds": result["rounds"],
                "tool_call_count": len(result["tool_calls"]),
                "tools": [
                    item["name"]
                    for item in result["tool_calls"]
                ]
            }
        })

        agent_trace.append(
            create_trace_event(
                event="analysis_done",

                node="executor",

                tool="tool_calling",

                status="success",

                message=(
                    f"任务完成，共调用 "
                    f"{len(result['tool_calls'])} 次工具"
                ),

                metadata={
                    "tools": [
                        item["name"]
                        for item in result["tool_calls"]
                    ]
                },

                display={
                    "title": "分析完成",
                    "icon": "check",
                    "level": "success",
                    "visible": True
                }
            )
        )

        return {
            "execution_results": execution_results,

            "agent_trace": agent_trace,

            "current_step": current_step + 1,

            "execution_status": "success",

            "pending_code": None,

            "last_execution_error": None,

            "current_task_retry_count": 0,

            "code_history": code_history,

            "error": None
        }

    # ==========================================
    # 失败
    # ==========================================

    error_info = result.get("error") or {
        "type": "ToolError",
        "message": "工具调用失败"
    }

    agent_trace.append(
        create_trace_event(
            event="analysis_error",

            node="executor",

            tool="tool_calling",

            status="error",

            message=(
                f"任务执行失败：{error_info['type']}"
            ),

            error=error_info,

            display={
                "title": "发现执行错误",
                "icon": "alert",
                "level": "error",
                "visible": True
            }
        )
    )

    max_task_retries = state.get(
        "max_task_retries",
        2
    )

    # ==========================================
    # 达到上限 -> 交给 task_failure
    # ==========================================

    if current_task_retry_count >= max_task_retries:

        return {
            "execution_status": "exhausted",

            "pending_code": None,

            "last_execution_error": error_info,

            "code_history": code_history,

            "agent_trace": agent_trace,

            "error": (
                "任务执行失败，且已达到最大自动修复次数："
                f"{error_info['type']}: {error_info['message']}"
            )
        }

    # ==========================================
    # 还可以交给 error_handler 再试
    # ==========================================

    return {
        "execution_status": "retry_needed",

        "pending_code": None,

        "last_execution_error": error_info,

        "code_history": code_history,

        "agent_trace": agent_trace,

        "error": (
            "任务执行失败："
            f"{error_info['type']}: {error_info['message']}"
        )
    }
