from agent.state import AgentState
from services.llm_service import call_llm
from agent.nodes.executor import clean_code


# =========================
# V3.3.4
# 统一 Agent Trace 工具
# =========================

from agent.utils.trace import create_trace_event


def error_handler(
    state: AgentState
):
    """
    Error Handler / Code Fixer Node。

    职责：

    1. 获取当前任务
    2. 获取原始 / 上一次修复后的代码
    3. 获取真实执行错误
    4. 结合数据集信息分析错误
    5. 生成修复后的代码
    6. 增加当前任务修复次数
    7. 返回 Executor 再次执行

    V3.3.4：

    - 统一 Agent Trace Event 结构
    - 使用 create_trace_event()
    - 所有事件包含：
        event
        step
        node
        tool
        status
        message
        timestamp
        error
        metadata

    V3.4：

    - 增加用户可视化 Trace
    - display
    - title
    - icon
    - level
    - visible

    注意：

    这里只负责“修复代码”，
    不执行代码。

    真正执行仍然由 Executor 完成。
    """

    # ==========================================
    # 获取当前任务
    # ==========================================

    current_step = state["current_step"]

    plan = state["plan"]

    # ==========================================
    # 获取 Agent Trace
    # ==========================================

    agent_trace = list(
        state.get(
            "agent_trace",
            []
        )
    )

    # ==========================================
    # 防止任务越界
    # ==========================================

    if current_step >= len(plan):

        error_info = {
            "type": "StepOutOfRange",
            "message": (
                "Error Handler 执行时任务索引越界"
            )
        }

        # ======================================
        # V3.4
        # 用户可视化 Trace
        # ======================================

        agent_trace.append(
            create_trace_event(
                event="repair_failed",

                step=current_step + 1,

                node="error_handler",

                tool=None,

                status="error",

                message=(
                    "Error Handler 执行失败："
                    "任务索引越界"
                ),

                error=error_info,

                display={
                    "title": "自动修复失败",
                    "icon": "alert",
                    "level": "error",
                    "visible": True
                }
            )
        )

        return {
            "execution_status": "exhausted",

            "agent_trace":
                agent_trace,

            "error":
                error_info["message"]
        }

    # ==========================================
    # 当前任务
    # ==========================================

    current_task = plan[current_step]

    task_description = current_task["task"]

    tool_name = current_task["tool"]

    # ==========================================
    # 获取需要修复的代码
    # ==========================================

    original_code = state.get(
        "pending_code"
    ) or ""

    # ==========================================
    # 获取真实错误信息
    # ==========================================

    error_info = state.get(
        "last_execution_error"
    ) or {}

    error_type = error_info.get(
        "type",
        "UnknownError"
    )

    error_message = error_info.get(
        "message",
        ""
    )

    # ==========================================
    # 数据集信息
    # ==========================================

    dataset_info = state["dataset_info"]

    # ==========================================
    # 当前任务已经修复次数
    # ==========================================

    current_retry_count = state.get(
        "current_task_retry_count",
        0
    )

    # ==========================================
    # V3.4
    #
    # Agent 开始分析错误
    # ==========================================

    agent_trace.append(
        create_trace_event(
            event="error_analysis",

            step=current_step + 1,

            node="error_handler",

            tool=tool_name,

            status="running",

            message=(
                "正在分析工具执行错误"
            ),

            error={
                "type": error_type,
                "message": error_message
            },

            metadata={
                "retry_count":
                    current_retry_count
            },

            display={
                "title": "分析执行错误",
                "icon": "search",
                "level": "warning",
                "visible": True
            }
        )
    )

    # ==========================================
    # 构造代码修复 Prompt
    # ==========================================

    prompt = f"""
你是 DataMind 数据分析 Agent 的代码修复专家。

当前数据分析任务执行失败。

你的任务是：

根据原始代码、真实报错信息、数据集信息和任务目标，
修复代码，并保证修复后的代码可以直接执行。

====================

当前任务：

{task_description}

====================

使用工具：

{tool_name}

====================

数据集信息：

{dataset_info}

====================

原始代码：

{original_code}

====================

真实报错类型：

{error_type}

====================

真实报错信息：

{error_message}

====================

修复要求：

1. 只修复当前代码，不要改变任务目标。
2. DataFrame 变量固定为 df。
3. 如果是统计任务，最终结果必须赋值给 result。
4. 不要读取文件。
5. 不要 import。
6. 时间重采样月末使用 ME。
7. 如果是绘图任务，不要调用 savefig 或 close。
8. 只返回修复后的 Python 代码。
9. 不要 Markdown。
10. 不要解释。
"""

    # ==========================================
    # 构造 LLM 消息
    # ==========================================

    messages = [
        {
            "role": "system",

            "content": (
                "你是严谨的 Python "
                "数据分析代码修复专家。"
                "必须根据真实错误修复代码。"
            )
        },

        {
            "role": "user",

            "content": prompt
        }
    ]

    # ==========================================
    # 调用 LLM 修复代码
    # ==========================================

    try:

        fixed_code = call_llm(
            messages
        )

        # ======================================
        # 清理 Markdown 代码块
        # ======================================

        fixed_code = clean_code(
            fixed_code
        )

        # ======================================
        # 增加修复次数
        # ======================================

        new_retry_count = (
            current_retry_count + 1
        )

        # ======================================
        # 获取代码历史
        # ======================================

        code_history = list(
            state.get(
                "code_history",
                []
            )
        )

        # ======================================
        # 保存修复后的代码
        # ======================================

        code_history.append(
            {
                "step":
                    current_step + 1,

                "task":
                    task_description,

                "tool":
                    tool_name,

                "action":
                    "fixed",

                "retry_count":
                    new_retry_count,

                "error_type":
                    error_type,

                "error_message":
                    error_message,

                "code":
                    fixed_code
            }
        )

        # ======================================
        # V3.4
        #
        # Agent 已完成代码修复
        # ======================================

        agent_trace.append(
            create_trace_event(
                event="code_fixed",

                step=current_step + 1,

                node="error_handler",

                tool=tool_name,

                status="success",

                message=(
                    "已根据错误信息生成修复代码"
                ),

                error={
                    "type": error_type,
                    "message": error_message
                },

                metadata={
                    "retry_count":
                        new_retry_count
                },

                display={
                    "title": "自动修复代码",
                    "icon": "wrench",
                    "level": "warning",
                    "visible": True
                }
            )
        )

        # ======================================
        # 返回 Executor
        #
        # Graph Router 根据：
        #
        # execution_status == retrying
        #
        # 再次进入 Executor
        # ======================================

        return {
            "pending_code":
                fixed_code,

            "current_task_retry_count":
                new_retry_count,

            "execution_status":
                "retrying",

            "code_history":
                code_history,

            "agent_trace":
                agent_trace,

            "error":
                None
        }

    # ==========================================
    # Error Handler 自己执行失败
    # ==========================================

    except Exception as e:

        error_info = {
            "type":
                type(e).__name__,

            "message":
                str(e)
        }

        # ======================================
        # V3.4
        #
        # 统一记录修复失败
        # ======================================

        agent_trace.append(
            create_trace_event(
                event="repair_failed",

                step=current_step + 1,

                node="error_handler",

                tool=tool_name,

                status="error",

                message=(
                    "代码自动修复失败"
                ),

                error=error_info,

                metadata={
                    "retry_count":
                        current_retry_count
                },

                display={
                    "title": "自动修复失败",
                    "icon": "alert",
                    "level": "error",
                    "visible": True
                }
            )
        )

        return {
            "execution_status":
                "exhausted",

            "agent_trace":
                agent_trace,

            "error": (
                "代码修复失败："
                f"{error_info['type']}: "
                f"{error_info['message']}"
            )
        }