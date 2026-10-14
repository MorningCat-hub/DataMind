from datetime import datetime
from typing import Any, Dict, Optional

# ============================================================
# V3.4
# Trace 数据层
# ============================================================
#
# 作用：
# 1. 统一 Agent Trace 数据结构
# 2. 同时服务于开发者调试和前端展示
# 3. 不暴露 LLM 内部 Chain of Thought
# 4. 只展示 Agent 的“可解释执行过程”
#
# V3.4 新增：
# display 字段
#
# display:
# {
#     "title": "...",
#     "icon": "...",
#     "level": "...",
#     "visible": True
# }
#
# ============================================================

def create_trace_event(
    event: str,
    step: Optional[int] = None,
    node: Optional[str] = None,
    tool: Optional[str] = None,
    status: Optional[str] = None,
    message: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,

    # ========================================================
    # V3.4 新增
    # 前端展示配置
    # ========================================================
    display: Optional[Dict[str, Any]] = None,

) -> Dict[str, Any]:
    """
    创建统一格式的 Agent Trace Event。

    --------------------------------------------------------
    V3.3.4 标准字段
    --------------------------------------------------------

    event:
        事件类型

    step:
        当前任务步骤

    node:
        当前执行节点

    tool:
        当前使用工具

    status:
        当前事件状态

    message:
        给前端或用户展示的简短描述

    error:
        错误信息

    metadata:
        扩展信息

    timestamp:
        事件发生时间

    --------------------------------------------------------
    V3.4 新增
    --------------------------------------------------------

    display:
        前端展示配置

        {
            "title": "计算销售额总和",
            "icon": "calculator",
            "level": "success",
            "visible": True
        }

    --------------------------------------------------------
    设计原则
    --------------------------------------------------------

    Trace 记录的是：

        Agent 做了什么

    而不是：

        LLM 内部完整思考过程

    因此不会记录模型原始 Chain of Thought。
    """

    # ========================================================
    # V3.4
    # 默认展示配置
    # ========================================================

    if display is None:

        display = {
            "title": message or event,
            "icon": "info",
            "level": _get_display_level(
                event=event,
                status=status
            ),
            "visible": True
        }

    else:

        # ====================================================
        # 防御性处理
        #
        # 防止调用方只传了一部分 display 字段。
        # ====================================================

        display = {
            "title": display.get(
                "title",
                message or event
            ),

            "icon": display.get(
                "icon",
                "info"
            ),

            "level": display.get(
                "level",
                _get_display_level(
                    event=event,
                    status=status
                )
            ),

            "visible": display.get(
                "visible",
                True
            )
        }

    # ========================================================
    # 返回统一 Trace Schema
    # ========================================================

    return {

        # ----------------------------------------------------
        # 基础事件信息
        # ----------------------------------------------------

        "event": event,

        "step": step,

        "node": node,

        "tool": tool,

        "status": status,

        "message": message,

        # ----------------------------------------------------
        # 时间
        # ----------------------------------------------------

        "timestamp": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        # ----------------------------------------------------
        # 错误信息
        # ----------------------------------------------------

        "error": error,

        # ----------------------------------------------------
        # 扩展信息
        # ----------------------------------------------------

        "metadata": metadata or {},

        # ----------------------------------------------------
        # V3.4
        # 前端展示配置
        # ----------------------------------------------------

        "display": display
    }


# ============================================================
# V3.4
# 根据事件自动判断前端展示等级
# ============================================================

def _get_display_level(
    event: str,
    status: Optional[str]
) -> str:
    """
    根据 Trace Event 自动决定前端展示等级。

    返回：

        info
        success
        warning
        error

    注意：
    这里只负责“展示等级”，
    不负责判断业务逻辑是否真的成功。
    """

    # ========================================================
    # 明确失败
    # ========================================================

    if status in (
        "error",
        "failed",
        "failure"
    ):
        return "error"

    if event in (
        "tool_error",
        "execution_error",
        "error"
    ):
        return "error"

    # ========================================================
    # 警告 / 自动修复
    # ========================================================

    if event in (
        "error_analysis",
        "code_fixed",
        "retry",
        "warning"
    ):
        return "warning"

    if status in (
        "warning",
        "retry"
    ):
        return "warning"

    # ========================================================
    # 成功
    # ========================================================

    if status in (
        "success",
        "completed"
    ):
        return "success"

    if event in (
        "tool_success",
        "execution_success",
        "completed"
    ):
        return "success"

    # ========================================================
    # 默认
    # ========================================================

    return "info"