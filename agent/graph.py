from langgraph.graph import (
   StateGraph,
    END)

from agent.nodes.chart_planner import chart_planner
from agent.nodes.chart_executor import chart_executor

from agent.state import AgentState
from agent.utils.trace import create_trace_event

from agent.nodes.dataset import analyze_dataset
from agent.nodes.planner import planner
from agent.nodes.executor import executor
from agent.nodes.error_handler import error_handler
from agent.nodes.task_failure import mark_task_failed
from agent.nodes.reviewer import reviewer
from agent.nodes.reporter import generate_report
from agent.nodes.verifier import verify_report
from agent.nodes.conclusion_check import verify_conclusions
from agent.nodes.retriever import retrieve_knowledge
from agent.nodes.health_check import check_data_health


def with_node_trace(node_fn, node_name, title, icon):
    """为每个 LangGraph 节点统一记录开始和结束事件。"""
    def wrapped(state: AgentState):
        trace = list(state.get("agent_trace") or [])
        trace.append(create_trace_event(
            event="node_started",
            node=node_name,
            status="running",
            message=f"开始执行：{title}",
            display={
                "title": title,
                "icon": icon,
                "level": "info",
                "visible": True,
            },
        ))

        node_state = {**state, "agent_trace": trace}
        result = node_fn(node_state)
        if not isinstance(result, dict):
            result = {}

        updated_trace = list(result.get("agent_trace") or node_state.get("agent_trace") or trace)
        node_error = result.get("error")
        execution_status = result.get("execution_status")
        has_warning = bool(node_error) or execution_status in ("retry_needed", "retrying", "exhausted")

        updated_trace.append(create_trace_event(
            event="node_completed",
            node=node_name,
            status="warning" if has_warning else "completed",
            message=f"{title}结束，存在需要关注的状态" if has_warning else f"{title}完成",
            error={"message": str(node_error)} if node_error else None,
            display={
                "title": f"{title}结束，需关注" if has_warning else f"{title}完成",
                "icon": "alert-circle" if has_warning else "check-circle",
                "level": "warning" if has_warning else "success",
                "visible": True,
            },
        ))
        return {**result, "agent_trace": updated_trace}

    return wrapped


# ==================================
# Executor 路由
# ==================================

def route_after_executor(
    state: AgentState
):
    """
    V3.1 Executor 路由：

    success
        ↓
    有剩余任务？
        ├── 是 → executor
        └── 否 → reviewer

    retry_needed
        ↓
    error_handler

    exhausted
        ↓
    mark_task_failed
    """

    execution_status = state.get(
        "execution_status",
        "idle"
    )

    # =========================
    # 当前任务成功
    # =========================

    if execution_status == "success":

        current_step = state["current_step"]
        plan = state["plan"]

        if current_step < len(plan):

            return "executor"

        return "reviewer"

    # =========================
    # 当前任务需要修复
    # =========================

    if execution_status in (
        "retry_needed",
        "retrying"
    ):

        return "error_handler"

    # =========================
    # 已达到最大修复次数
    # =========================

    if execution_status == "exhausted":

        return "task_failure"

    # =========================
    # 兜底
    # =========================

    current_step = state["current_step"]
    plan = state["plan"]

    if current_step < len(plan):

        return "executor"

    return "reviewer"


# ==================================
# Reviewer 路由
# ==================================

def route_after_reviewer(
    state: AgentState
):
    """
    Reviewer 完成后：

    passed
        ↓
    Chart Planner

    failed + retry budget
        ↓
    Retry → Planner

    failed + retry exhausted
        ↓
    Chart Planner
    """

    review_result = state.get(
        "review_result"
    ) or {}

    retry_count = state.get(
        "retry_count",
        0
    )

    # ==================================
    # 分析完整
    # ↓
    # 图表规划
    # ==================================

    if review_result.get(
        "passed",
        False
    ):
        return "chart_planner"

    # ==================================
    # 分析不完整
    # 还有全局重试机会
    # ==================================

    if retry_count < 2:
        return "retry"

    # ==================================
    # 重试次数耗尽
    # 仍然进入图表规划
    # 然后生成最终报告
    # ==================================

    return "chart_planner"


# ==================================
# Retry Node
# ==================================


def route_after_chart_planner(
    state: AgentState
):
    """
    Chart Planner 完成后：

    need_chart == True
        ↓
    Chart Executor

    need_chart == False
        ↓
    Report
    """

    chart_plan = state.get(
        "chart_plan"
    ) or {}

    if chart_plan.get(
        "need_chart",
        False
    ):
        return "chart_executor"

    return "report"


def increase_retry(
    state: AgentState
):
    """
    Reviewer 失败后：

    retry_count + 1

    然后重新进入 Planner。

    注意：

    这是“整体分析重新规划”。

    与 V3.1 的：
    - current_task_retry_count
    - Error Handler

    是两个不同层级的重试机制。
    """

    current_retry = state.get(
        "retry_count",
        0
    )

    return {
        "retry_count":
            current_retry + 1,

        "review_result":
            None,

        "execution_status":
            "idle",

        "pending_code":
            None,

        "last_execution_error":
            None,

        "current_task_retry_count":
            0
    }


# ==================================
# Dataset 路由（V4.1）
# ==================================

def route_after_dataset(
    state: AgentState
):
    """
    读取节点完成后：

    表格数据
        ↓
    Planner（正常分析链路）

    非表格内容（Word 正文 / 图片解读）
        ↓
    Report（直接根据内容回答用户问题）

    读取失败
        ↓
    Report（如实说明失败原因）
    """

    input_kind = state.get(
        "input_kind",
        "table"
    )

    if input_kind == "table":

        return "health_check"

    return "report"


# ==================================
# Build Graph
# ==================================

def build_graph():

    workflow = StateGraph(
        AgentState
    )

    # =========================
    # 添加 Nodes
    # =========================

    workflow.add_node(
        "dataset",
        with_node_trace(
            analyze_dataset, "dataset", "读取与分析数据集", "database"
        ),
    )

    # =========================
    # V4.11 数据体检
    #
    # 放在最前面：
    # 先知道这份数据有什么坑，再去决定要不要下结论。
    # =========================

    workflow.add_node(
        "health_check",
        with_node_trace(
            check_data_health, "health_check", "数据体检", "stethoscope"
        ),
    )

    # =========================
    # V4.6 业务口径检索
    #
    # 放在 Planner 之前：
    # 先知道「净销售额」是什么意思，再决定怎么算。
    # =========================

    workflow.add_node(
        "retrieve",
        with_node_trace(
            retrieve_knowledge, "retrieve", "检索业务口径", "book"
        ),
    )

    workflow.add_node(
        "planner",
        with_node_trace(
            planner, "planner", "制定分析计划", "list-check"
        ),
    )

    workflow.add_node(
        "executor",
        with_node_trace(
            executor, "executor", "执行数据分析", "calculator"
        ),
    )

    workflow.add_node(
        "error_handler",
        with_node_trace(
            error_handler, "error_handler", "分析并修复错误", "wrench"
        ),
    )

    workflow.add_node(
        "task_failure",
        with_node_trace(
            mark_task_failed, "task_failure", "记录任务失败", "alert-triangle"
        ),
    )

    workflow.add_node(
        "reviewer",
        with_node_trace(
            reviewer, "reviewer", "检查分析结果", "clipboard-check"
        ),
    )

    workflow.add_node(
        "retry",
        with_node_trace(
            increase_retry, "retry", "重新规划分析任务", "refresh-cw"
        ),
    )

    workflow.add_node(
        "chart_planner",
        with_node_trace(
            chart_planner, "chart_planner", "规划可视化图表", "chart-no-axes-combined"
        ),
    )

    workflow.add_node(
        "chart_executor",
        with_node_trace(
            chart_executor, "chart_executor", "生成可视化图表", "image"
        ),
    )

    workflow.add_node(
        "report",
        with_node_trace(
            generate_report, "report", "生成最终分析报告", "file-text"
        ),
    )

    # =========================
    # V4.4 数值核对
    #
    # 报告生成之后过一遍核对，
    # 把「无法追溯到计算结果的数值」标出来。
    # =========================

    workflow.add_node(
        "verify",
        with_node_trace(
            verify_report, "verify", "核对报告数值", "shield-check"
        ),
    )

    # =========================
    # Entry
    # =========================

    workflow.set_entry_point(
        "dataset"
    )

    # =========================
    # Dataset
    # ↓
    # Planner / Report
    #
    # V4.1：
    # 表格数据才进分析链路；
    # 非表格内容或读取失败直接进报告。
    # =========================

    workflow.add_conditional_edges(
        "dataset",
        route_after_dataset,
        {
            "health_check":
                "health_check",

            "report":
                "report"
        }
    )

    # =========================
    # V4.11 数据体检
    #
    # Health Check
    # ↓
    # Retrieve
    # =========================

    workflow.add_edge(
        "health_check",
        "retrieve"
    )

    # =========================
    # V4.6 业务口径
    #
    # Retrieve
    # ↓
    # Planner
    # =========================

    workflow.add_edge(
        "retrieve",
        "planner"
    )

    # Planner
    # ↓
    # Executor
    workflow.add_edge(
        "planner",
        "executor"
    )

    # =========================
    # Executor
    # ↓
    # Executor / Error Handler /
    # Task Failure / Reviewer
    # =========================

    workflow.add_conditional_edges(
        "executor",
        route_after_executor,
        {
            "executor":
                "executor",

            "error_handler":
                "error_handler",

            "task_failure":
                "task_failure",

            "reviewer":
                "reviewer"
        }
    )

    # =========================
    # Error Handler
    # ↓
    # Executor
    # =========================

    workflow.add_edge(
        "error_handler",
        "executor"
    )

    # =========================
    # Task Failure
    # ↓
    # Executor / Reviewer
    # =========================

    workflow.add_conditional_edges(
        "task_failure",
        lambda state: (
            "executor"
            if state["current_step"] < len(state["plan"])
            else "reviewer"
        ),
        {
            "executor":
                "executor",

            "reviewer":
                "reviewer"
        }
    )

    # =========================
    # Reviewer
    # ↓
    # Retry / Report
    # =========================

    workflow.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {
            "retry": "retry",
            "chart_planner": "chart_planner"
        }
    )

    # ==================================
    # Chart Planner
    # ↓
    # Chart Executor / Report
    # ==================================

    workflow.add_conditional_edges(
        "chart_planner",
        route_after_chart_planner,
        {
            "chart_executor":
                "chart_executor",

            "report":
                "report"
        }
    )

    # ==================================
    # Chart Executor
    # ↓
    # Report
    # ==================================

    workflow.add_edge(
        "chart_executor",
        "report"
    )


    # Retry
    # ↓
    # Planner
    workflow.add_edge(
        "retry",
        "planner"
    )

    # Report
    # ↓
    # Verify
    workflow.add_edge(
        "report",
        "verify"
    )

    # Verify
    # ↓
    # Conclusion Check
    workflow.add_edge(
        "verify",
        "conclusion_check"
    )

    # =========================
    # V4.12 结论核对
    #
    # 数值核对回答「数字对不对」，
    # 这一跳回答「结论这份数据撑不撑得住」。
    # =========================

    workflow.add_node(
        "conclusion_check",
        with_node_trace(
            verify_conclusions,
            "conclusion_check",
            "核对报告结论",
            "shield-check"
        ),
    )

    # Conclusion Check
    # ↓
    # END
    workflow.add_edge(
        "conclusion_check",
        END
    )

    graph = workflow.compile()

    return graph
