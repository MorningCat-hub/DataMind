"""
DataMind V3.4.2
Trace 前端展示数据转换层

作用：
1. 保留完整 agent_trace 作为调试数据
2. 将 agent_trace 转换成前端更容易使用的 agent_trace_view
3. 前端无需理解 Trace 内部复杂结构
"""

from typing import Any, Dict, List


def format_trace_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    将单个 Trace Event 转换成前端展示格式。
    """

    display = event.get("display") or {}

    return {
        "title": display.get(
            "title",
            event.get("event", "未知操作")
        ),

        "icon": display.get(
            "icon",
            "info"
        ),

        "level": display.get(
            "level",
            "info"
        ),

        "status": event.get(
            "status",
            "info"
        ),

        "message": event.get(
            "message",
            ""
        ),

        "step": event.get(
            "step"
        ),

        "visible": display.get(
            "visible",
            True
        )
    }


def format_agent_trace(
    agent_trace: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    将完整 Agent Trace 转换成前端展示列表。

    注意：
    原始 agent_trace 不会被修改。
    """

    if not agent_trace:
        return []

    trace_view = []

    for event in agent_trace:

        if not isinstance(event, dict):
            continue

        formatted_event = format_trace_event(event)

        # visible=False 的事件不进入前端展示列表
        if not formatted_event["visible"]:
            continue

        trace_view.append(formatted_event)

    return trace_view