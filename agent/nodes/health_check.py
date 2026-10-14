from agent.state import AgentState
from agent.utils.health_check import (
    format_health_block,
    format_health_notice,
    run_health_checks,
    summarize_findings,
)
from agent.utils.trace import create_trace_event


def check_data_health(
    state: AgentState
):
    """
    数据体检节点（V4.11）。

    放在检索口径之前：

    先知道这份数据有什么坑，再去决定怎么算。

    注意：

    这个节点是**确定性**的（纯 pandas，不调用模型）。
    因为它的结论会被用来拦截报告里的错误结论 ——
    如果体检本身是模型判断的，就是用猜测校验猜测。

    非表格输入（Word 正文 / 图片解读）不走分析链路，直接跳过。
    """

    agent_trace = list(
        state.get("agent_trace") or []
    )

    input_kind = state.get(
        "input_kind",
        "table"
    )

    if input_kind != "table":

        return {
            "health_findings": [],
            "health_context": None,
            "health_notice": None,
            "agent_trace": agent_trace
        }

    result = run_health_checks(
        state.get("file_path") or ""
    )

    findings = result.get("findings") or []

    summarized = summarize_findings(findings)

    blocks = [
        item for item in summarized
        if item["severity"] == "block"
    ]

    if not result.get("ok"):

        agent_trace.append(
            create_trace_event(
                event="health_check_failed",

                node="health_check",

                status="warning",

                message=(
                    "数据体检没能完成："
                    + str(result.get("error") or "未知原因")
                ),

                display={
                    "title": "数据体检未完成",
                    "icon": "alert",
                    "level": "warning",
                    "visible": True
                }
            )
        )

        return {
            "health_findings": [],
            "health_context": None,
            "health_notice": None,
            "agent_trace": agent_trace
        }

    if blocks:

        level = "warning"

    elif summarized:

        level = "info"

    else:

        level = "success"

    agent_trace.append(
        create_trace_event(
            event="health_checked",

            node="health_check",

            status="success",

            message=(
                "{}（必须规避 {} 项）".format(
                    result.get("summary"), len(blocks)
                )
                if blocks
                else result.get("summary")
            ),

            metadata={
                "findings": summarized,
                "rows": result.get("rows")
            },

            display={
                "title": (
                    "数据体检：{} 项需要注意".format(len(summarized))
                    if summarized
                    else "数据体检通过"
                ),
                "icon": "stethoscope",
                "level": level,
                "visible": True
            }
        )
    )

    return {
        "health_findings": summarized,
        "health_context": format_health_block(findings) or None,
        "health_notice": format_health_notice(findings) or None,
        "agent_trace": agent_trace
    }
