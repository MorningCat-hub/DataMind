from agent.state import AgentState
from agent.utils.conclusion_check import (
    build_conclusion_notice,
    check_conclusions,
    summarize_conclusions,
)
from agent.utils.trace import create_trace_event


def verify_conclusions(
    state: AgentState
):
    """
    结论级核对节点（V4.12）。

    放在数值核对之后：

        数值核对回答「报告里的数字对不对」，
        这一跳回答「报告的结论，这份数据撑不撑得住」。

    规则来自数据体检里 severity = block 的发现项，
    所以这是**确定性**的 —— 拿猜测校验猜测没有意义。

    踩坑时：

    1. trace 标黄，节点被标成需要关注
    2. 由程序在报告末尾追加纠正说明（不经过模型）
    """

    agent_trace = list(
        state.get("agent_trace") or []
    )

    report = state.get("final_report") or ""

    if not report.strip():

        return {
            "conclusion_check": None,
            "agent_trace": agent_trace
        }

    findings = state.get("health_findings") or []

    result = check_conclusions(report, findings)

    summarized = summarize_conclusions(result)

    checked = result.get("checked", 0)

    violations = result.get("violations") or []

    # ==========================================
    # 没有可核对的规则
    # ==========================================

    if checked == 0:

        agent_trace.append(
            create_trace_event(
                event="conclusion_checked",

                node="conclusion_check",

                status="success",

                message="本次数据限制不涉及结论层面的核对",

                metadata={"checked": 0},

                display={
                    "title": "结论核对",
                    "icon": "check",
                    "level": "info",
                    "visible": True
                }
            )
        )

        return {
            "conclusion_check": summarized,
            "agent_trace": agent_trace
        }

    # ==========================================
    # 通过
    # ==========================================

    if not violations:

        agent_trace.append(
            create_trace_event(
                event="conclusion_checked",

                node="conclusion_check",

                status="success",

                message=(
                    "{} 项数据限制都没有被结论违反".format(checked)
                ),

                metadata={"checked": checked},

                display={
                    "title": "结论核对通过",
                    "icon": "check",
                    "level": "success",
                    "visible": True
                }
            )
        )

        return {
            "conclusion_check": summarized,
            "agent_trace": agent_trace
        }

    # ==========================================
    # 踩坑
    # ==========================================

    kinds = "、".join(sorted({item["kind"] for item in violations}))

    agent_trace.append(
        create_trace_event(
            event="conclusion_violation",

            node="conclusion_check",

            status="warning",

            message=(
                "{} 处结论与数据限制冲突：{}".format(
                    len(violations), kinds
                )
            ),

            metadata=summarized,

            display={
                "title": "结论核对：{} 处需要修正".format(len(violations)),
                "icon": "alert",
                "level": "warning",
                "visible": True
            }
        )
    )

    report = report + build_conclusion_notice(violations)

    return {
        "final_report": report,
        "conclusion_check": summarized,

        # 和图表失败 / 数值核对的处理保持一致：
        # 返回 error 之后节点会被标成「需要关注」而不是绿色完成
        "error": (
            "结论核对：{} 处结论与数据限制冲突（{}）".format(
                len(violations), kinds
            )
        ),

        "agent_trace": agent_trace
    }
