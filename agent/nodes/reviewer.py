
import json

from agent.state import AgentState
from services.llm_service import call_llm


def reviewer(state: AgentState):
    """
    Reviewer Node。

    职责：
    1. 检查数据分析是否回答了用户问题
    2. 检查是否存在关键分析遗漏
    3. 检查工具执行是否失败
    4. 判断是否需要重新规划

    注意：
    图表由后续 Chart Planner / Chart Executor 负责。
    Reviewer 不检查图表是否生成，也不要求 Planner 补充绘图任务。
    """

    question = state["question"]

    execution_results = state.get(
        "execution_results", []
    )

    prompt = f"""
你是 DataMind 数据分析智能体中的 Reviewer。

你的职责是检查 Agent 的数据分析过程是否真正回答了用户的问题。

注意：你只负责检查数据分析任务，不负责检查图表。
图表将在 Reviewer 通过后，由独立的 Chart Planner 和 Chart Executor 生成。

====================
用户问题
====================

{question}

====================
任务执行结果
====================

{execution_results}

====================
检查要求
====================

1. 用户问题是否已经得到回答？
2. 是否遗漏了回答问题所必需的关键数据分析？
3. 是否存在工具执行失败，导致关键分析结果缺失？
4. 是否确实需要重新规划数据分析任务？

以下情况不能单独作为不通过的理由：

- 尚未生成图表
- execution_results 中没有 plot_chart
- 尚未生成图片或图表链接

因为图表生成由后续 Chart Planner / Chart Executor 负责。

如果核心数据分析已经完成，且没有影响结论的关键错误，
即使还没有图表，也应该通过审查。

如果分析不完整，只列出真正缺失的数据分析任务。
不要把绘图任务放入 missing_tasks。
不要要求 Planner 使用 plot_chart。

====================
返回格式
====================

严格返回 JSON：

{{
    "passed": true,
    "reason": "核心数据分析已完成",
    "missing_tasks": []
}}

如果确实存在关键分析遗漏，返回：

{{
    "passed": false,
    "reason": "缺少必要的数据分析",
    "missing_tasks": [
        "具体缺失的数据分析任务"
    ]
}}

只返回 JSON。
不要返回 Markdown。
不要添加 JSON 之外的文字。
"""

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的数据分析质量审查员。"
                    "只审查数据分析的完整性和正确性。"
                    "图表生成由后续独立节点负责，"
                    "不得因尚未生成图表而判定分析失败。"
                    "必须严格返回 JSON。"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        response = call_llm(messages).strip()

        # 兼容模型偶尔返回 Markdown 代码块
        if response.startswith("```"):
            response = response.replace("```json", "")
            response = response.replace("```JSON", "")
            response = response.replace("```", "")
            response = response.strip()

        review_result = json.loads(response)

        # 规范返回字段，避免缺少字段影响图的路由
        passed = bool(review_result.get("passed", False))

        missing_tasks = review_result.get(
            "missing_tasks", []
        )

        if not isinstance(missing_tasks, list):
            missing_tasks = []

        # 防止模型将绘图任务放入 missing_tasks
        missing_tasks = [
            task for task in missing_tasks
            if isinstance(task, str)
            and not any(
                keyword in task.lower()
                for keyword in (
                    # V4.8.1：改成匹配「图表类型」而不是「生成 + 类型」。
                    #
                    # 原来只匹配连续的「生成柱状图」，
                    # 于是「生成各地区销售额柱状图」这种很自然的说法
                    # 会因为中间隔了字而漏过去，
                    # 导致审查被绘图需求判为不通过、白白触发全局重规划。
                    "plot_chart",
                    "绘图",
                    "图表",
                    "柱状图",
                    "折线图",
                    "饼图",
                    "条形图",
                    "散点图",
                    "可视化",
                    "图片",
                    "png",
                )
            )
        ]

        # 如果没有剩余的分析缺失任务，不能仅因绘图相关描述而失败
        # V4.8 修复：
        #
        # 原来这里是
        #   if not missing_tasks and not review_result.get("missing_tasks"):
        #       passed = bool(review_result.get("passed", False))
        # 而 missing_tasks 就是从 review_result 取出来再过滤的，
        # 所以这个条件等价于「原本就没有缺失任务」，
        # 而赋值又等于没赋 —— 整段是空操作。
        #
        # 按注释的原意，真正要做的是：
        # 如果缺失任务被过滤之后为空、但原本非空，
        # 说明 reviewer 想要的全部是绘图相关的任务。
        # 可视化由 Chart Planner 独立负责，
        # 不能因此判审查不通过，否则会白白触发全局重规划。
        if review_result.get("missing_tasks") and not missing_tasks:

            passed = True

        review_result["passed"] = passed
        review_result["missing_tasks"] = missing_tasks

        return {
            "review_result": review_result,
            "error": None
        }

    except Exception as e:
        return {
            "review_result": {
                "passed": False,
                "reason": (
                    f"Reviewer 异常："
                    f"{type(e).__name__}: {str(e)}"
                ),
                "missing_tasks": []
            },
            "error": str(e)
        }

