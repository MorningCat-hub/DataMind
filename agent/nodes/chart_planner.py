from agent.state import AgentState
from agent.utils.chart_planner import plan_charts


def chart_planner(state: AgentState):
    """
    DataMind V3.5 Chart Planner Node。

    Reviewer 通过后进入这里。

    负责：
    1. 判断当前分析是否需要图表
    2. 选择图表类型
    3. 确定 x_field
    4. 确定 y_field
    5. 确定 aggregation
    6. 生成结构化 chart_plan
    """

    question = state["question"]
    dataset_info = state["dataset_info"]

    try:
        # 注意：
        # 当前 utils/chart_planner.py 的 plan_charts()
        # 只接收 question 和 dataset_info。
        chart_plan = plan_charts(
            question=question,
            dataset_info=dataset_info
        )

        return {
            "chart_plan": chart_plan,
            "error": None
        }

    except Exception as e:

        # Chart Planner 失败时，
        # 不应该让整个 DataMind 分析失败。
        # 直接跳过图表，进入 Reporter。

        fallback_plan = {
            "need_chart": False,
            "charts": [],
            "reason": (
                f"Chart Planner 异常："
                f"{type(e).__name__}: {str(e)}"
            )
        }

        return {
            "chart_plan": fallback_plan,
            "error": (
                f"Chart Planner 异常："
                f"{type(e).__name__}: {str(e)}"
            )
        }