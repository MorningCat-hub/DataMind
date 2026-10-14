from agent.state import AgentState


def mark_task_failed(
    state: AgentState
):
    """
    单任务达到最大自动修复次数后执行。

    作用：

    1. 把最终失败信息写入 execution_results
    2. current_step + 1
    3. 清理 pending_code 等临时状态
    4. 让工作流继续执行后续任务
    5. 最终由 Reviewer 判断整体任务是否仍然完整

    这样可以避免：

    某一个任务无限修复
    ↓
    整个 Agent 永远卡死
    """

    current_step = state["current_step"]
    plan = state["plan"]

    if current_step >= len(plan):

        return {
            "execution_status": "idle"
        }

    current_task = plan[current_step]

    execution_results = list(
        state.get("execution_results", [])
    )

    error_info = state.get(
        "last_execution_error"
    ) or {}

    # V4.8：失败记录补齐成和成功记录一样的字段。
    #
    # 原来成功记录写 repair_count / original_code / final_code，
    # 失败记录写 retry_count / code —— 字段名对不上，
    # 下游（Reporter、核对器、评测）就得为两种形态各写一套取值逻辑。
    #
    # error 必须保留：Reporter 就是靠它生成失败提示的。
    execution_results.append(
        {
            "step": current_step + 1,
            "task": current_task["task"],
            "tool": current_task.get("tool") or "tool_calling",
            "status": "failed",
            "repair_count": state.get(
                "current_task_retry_count",
                0
            ),
            "auto_repaired": False,
            "original_code": None,
            "final_code": None,
            "repair_history": [],
            "data": {
                "result": (
                    "{}: {}".format(
                        error_info.get("type", "UnknownError"),
                        error_info.get("message", "")
                    )
                ),
                "output": "",
                "tool_calls": []
            },
            "metadata": {
                "tool": (
                    current_task.get("tool") or "tool_calling"
                ),
                "error": error_info
            },
            "error": error_info
        }
    )

    return {
        "execution_results":
            execution_results,

        "current_step":
            current_step + 1,

        "execution_status":
            "failed",

        "pending_code":
            None,

        "last_execution_error":
            None,

        "current_task_retry_count":
            0,

        # 不把整个 Agent 标记为致命错误。
        # Reviewer 会根据 failed task 判断是否需要重新规划。
        "error":
            None
    }
