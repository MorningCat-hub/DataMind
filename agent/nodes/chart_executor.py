from agent.state import AgentState
from agent.utils.chart_executor import execute_chart
from agent.utils.trace import create_trace_event


# ==========================================
# V3.8
#
# V3.9：
#
# 计划几张就执行几张、执行几张就展示几张。
# 每个图表计划最多执行 MAX_CHARTS 张，
# 全部结果写回 chart_results。
#
# 数量由 Chart Planner 决定（默认只规划 1 张），
# 这里只负责不要漏执行。
# ==========================================

MAX_CHARTS = 3


def describe_chart_error(chart_result) -> str:
    """
    从图表结果里取出可以给人看的失败原因。
    """

    error = (chart_result or {}).get("error") or {}

    if isinstance(error, dict):

        return (
            error.get("message")
            or error.get("type")
            or "未知原因"
        )

    return str(error) or "未知原因"


def chart_executor(
    state: AgentState
):
    """
    DataMind V3.5 Chart Executor Node。

    职责：

    1. 获取 Chart Planner 生成的 chart_plan
    2. 提取具体的图表计划
    3. 调用真正的 chart_executor 工具
    4. 保存 chart_results
    5. 将结果交给 Reporter

    注意：

    这里不负责绘图实现。

    真正的绘图逻辑位于：

        agent/utils/chart_executor.py

    V3.8：

    - 每张图表单独生成一条 trace 事件，失败时标记为 error。
      之前图表生成失败了，执行过程里仍然显示绿色「完成」，
      用户完全看不出图表根本没画出来；
    - 只要有图表失败，本节点就会返回 error，
      节点包装器随即将这一步标记为「需要关注」。
    """

    chart_plan = state.get(
        "chart_plan"
    )

    file_path = state["file_path"]

    agent_trace = list(
        state.get("agent_trace") or []
    )

    # ==========================================
    # 收集需要执行的图表
    # ==========================================

    charts = []

    if chart_plan and chart_plan.get("need_chart"):

        charts = sorted(
            chart_plan.get("charts") or [],
            key=lambda chart: chart.get(
                "priority",
                999
            )
        )[:MAX_CHARTS]

    # ==========================================
    # 没有需要生成的图表
    # ==========================================

    if not charts:

        return {
            "chart_results": [],
            "error": None,
            "agent_trace": agent_trace
        }

    # ==========================================
    # 逐张执行
    # ==========================================

    results = []

    failures = []

    for index, chart in enumerate(charts, start=1):

        result = execute_chart(
            file_path=file_path,
            chart_plan=chart
        )

        result = {
            **result,
            "index": index
        }

        results.append(result)

        succeeded = result.get("success") is True

        if not succeeded:

            failures.append(result)

        title = chart.get("title") or "分析图表"

        # ======================================
        # V3.8
        # 每张图表一条 trace
        # ======================================

        if succeeded:

            agent_trace.append(
                create_trace_event(
                    event="chart_generated",

                    node="chart_executor",

                    status="success",

                    message=f"已生成图表：{title}",

                    metadata={
                        "chart_type":
                            result.get("chart_type"),

                        "index":
                            index
                    },

                    display={
                        "title": f"生成图表：{title}",
                        "icon": "check",
                        "level": "success",
                        "visible": True
                    }
                )
            )

        else:

            reason = describe_chart_error(
                result
            )

            agent_trace.append(
                create_trace_event(
                    event="chart_error",

                    node="chart_executor",

                    status="error",

                    message=f"图表生成失败：{reason}",

                    error=result.get("error"),

                    metadata={
                        "chart_type":
                            chart.get("chart_type"),

                        "index":
                            index
                    },

                    display={
                        "title":
                            f"图表生成失败：{title}",

                        "icon": "alert",
                        "level": "error",
                        "visible": True
                    }
                )
            )

    # ==========================================
    # 只要有图表失败，就把这一步标记为需要关注
    # ==========================================

    error = None

    if failures:

        error = (
            f"{len(failures)}/{len(results)} 张图表生成失败："
            + "；".join(
                describe_chart_error(item)
                for item in failures
            )
        )

    return {
        "chart_results": results,
        "error": error,
        "agent_trace": agent_trace
    }
