from agent.state import AgentState
from agent.utils.knowledge import (
    format_knowledge_block,
    retrieve,
    summarize_hits,
)
from agent.utils.trace import create_trace_event


def retrieve_knowledge(
    state: AgentState
):
    """
    业务口径检索节点（V4.6）。

    职责：

    1. 根据用户问题检索相关的业务口径
    2. 把口径文本写进 state，注入给 Planner 和 Executor
    3. 在 trace 里写清楚命中了哪些口径

    为什么单独做一个节点：

        口径必须同时给到 Planner（决定计划）
        和 Executor（决定工具参数）。
        做成独立节点之后，这一跳在 trace 里看得见，
        也能单独测试。

    注意：

        非表格输入（Word 正文 / 图片解读）不走分析链路，
        这里直接跳过。
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
            "knowledge_context": None,
            "knowledge_hits": [],
            "agent_trace": agent_trace
        }

    question = state.get("question") or ""

    result = retrieve(
        question,
        file_path=state.get("file_path")
    )

    hits = result.get("hits") or []

    summarized = summarize_hits(hits)

    if summarized:

        names = "、".join(
            item["name"] for item in summarized
        )

        agent_trace.append(
            create_trace_event(
                event="knowledge_retrieved",

                node="retrieve",

                status="success",

                message=f"检索到业务口径：{names}",

                metadata={
                    "method": result.get("method"),
                    "hits": summarized
                },

                display={
                    "title":
                        f"应用业务口径（{len(summarized)} 条）",
                    "icon": "book",
                    "level": "success",
                    "visible": True
                }
            )
        )

    else:

        agent_trace.append(
            create_trace_event(
                event="knowledge_empty",

                node="retrieve",

                status="success",

                message=(
                    "没有检索到相关的业务口径，"
                    "按字段名直接分析"
                ),

                metadata={"method": result.get("method")},

                display={
                    "title": "业务口径检索",
                    "icon": "book",
                    "level": "info",
                    "visible": True
                }
            )
        )

    context = format_knowledge_block(hits)

    skipped = result.get("skipped_inapplicable") or 0

    if skipped:

        agent_trace.append(
            create_trace_event(
                event="knowledge_skipped",

                node="retrieve",

                status="success",

                message=(
                    "{} 条口径因为所需字段不在数据集里被跳过".format(
                        skipped
                    )
                ),

                metadata={"skipped": skipped},

                display={
                    "title": "跳过不适用的口径",
                    "icon": "book",
                    "level": "info",
                    "visible": True
                }
            )
        )

    return {
        "knowledge_context": context or None,
        "knowledge_hits": summarized,
        "agent_trace": agent_trace
    }
