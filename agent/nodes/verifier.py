from agent.state import AgentState
from agent.utils.trace import create_trace_event
from agent.utils.verifier import (
    build_verification_notice,
    verify_report_numbers,
)


def verify_report(
    state: AgentState
):
    """
    数值核对节点（V4.4）。

    职责：

    1. 把报告里的数值与 execution_results 逐个核对
    2. 把核对结果写进 trace，
       让「结果可不可信」变成看得见的一件事
    3. 追溯不到的数值，由程序追加一段提示到报告末尾

    注意：

    这里不会删改报告内容，
    只是把「无法追溯」的数值标出来，
    最终判断仍然交给用户。
    """

    report = state.get("final_report") or ""

    agent_trace = list(
        state.get("agent_trace") or []
    )

    if not report.strip():

        return {
            "verification": None,
            "agent_trace": agent_trace
        }

    verification = verify_report_numbers(
        report,
        execution_results=state.get("execution_results"),
        chart_results=state.get("chart_results"),
    )

    checked = verification.get("checked", 0)
    unmatched_count = verification.get("unmatched_count", 0)
    source_count = verification.get("source_count", 0)

    # ==========================================
    # 报告里一个数值都没有
    # ==========================================

    if checked == 0:

        agent_trace.append(
            create_trace_event(
                event="number_verified",

                node="verify",

                status="success",

                message="报告里没有需要核对的数值",

                metadata={
                    "checked": 0,
                    "source_count": source_count
                },

                display={
                    "title": "数值核对",
                    "icon": "check",
                    "level": "info",
                    "visible": True
                }
            )
        )

        return {
            "verification": verification,
            "agent_trace": agent_trace
        }

    # ==========================================
    # 全部可追溯
    # ==========================================

    if verification.get("passed"):

        agent_trace.append(
            create_trace_event(
                event="number_verified",

                node="verify",

                status="success",

                message=(
                    f"{checked} 个数值全部可追溯到计算结果"
                ),

                metadata={
                    "checked": checked,
                    "matched": checked,
                    "source_count": source_count
                },

                display={
                    "title":
                        f"数值核对通过（{checked} 个）",
                    "icon": "check",
                    "level": "success",
                    "visible": True
                }
            )
        )

        return {
            "verification": verification,
            "agent_trace": agent_trace
        }

    # ==========================================
    # 有追溯不到的数值
    # ==========================================

    samples = "；".join(
        str(claim.get("raw"))
        for claim in (verification.get("unmatched") or [])[:3]
    )

    agent_trace.append(
        create_trace_event(
            event="number_unverified",

            node="verify",

            status="warning",

            message=(
                f"{unmatched_count} / {checked} 个数值"
                "无法追溯到计算结果"
                + (f"：{samples}" if samples else "")
            ),

            metadata={
                "checked": checked,
                "unmatched_count": unmatched_count,
                "source_count": source_count
            },

            display={
                "title":
                    f"数值核对：{unmatched_count} 处待核对",
                "icon": "alert",
                "level": "warning",
                "visible": True
            }
        )
    )

    report = report + build_verification_notice(
        verification
    )

    return {
        "final_report": report,
        "verification": verification,

        # 和图表失败的处理保持一致：
        # 返回 error 之后，节点包装器会把这一步标成「需要关注」，
        # 而不是显示绿色的「完成」。
        "error": (
            f"数值核对：{unmatched_count} / {checked}"
            " 个数值无法追溯到计算结果"
        ),

        "agent_trace": agent_trace
    }
