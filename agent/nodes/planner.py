import json

from agent.state import AgentState
from services.llm_service import call_llm
from agent.utils.history import format_history


def planner(state: AgentState):
    """
    DataMind Planner Node。

    职责：
    1. 根据用户问题制定数据分析计划
    2. 根据已有执行结果避免重复任务
    3. 根据 Reviewer 的 missing_tasks 补充分析任务
    4. 只规划数据计算任务
    5. 可视化统一交给 Chart Planner / Chart Executor
    """

    question = state["question"]
    dataset_info = state["dataset_info"]

    # V4.6：检索到的业务口径
    knowledge_context = (
        state.get("knowledge_context") or ""
    )

    knowledge_text = (
        knowledge_context
        if knowledge_context.strip()
        else "（本次没有检索到相关的业务口径）"
    )

    # V4.11：数据体检结论
    health_text = (
        state.get("health_context") or ""
    ).strip()

    if not health_text:

        health_text = "（本次没有发现需要特别声明的数据问题）"

    # V4.2：多轮对话上下文
    history_text = format_history(
        state.get("history")
    )

    execution_results = state.get(
        "execution_results", []
    )

    review_result = state.get(
        "review_result"
    ) or {}

    missing_tasks = review_result.get(
        "missing_tasks", []
    )

    prompt = f"""
你是 DataMind 数据分析智能体中的 Planner。

你的任务是根据用户问题、数据集信息以及已经执行的分析结果，
制定数据分析计划。

注意：你只负责数据计算和统计分析。
可视化由独立的 Chart Planner 和 Chart Executor 负责，
你绝对不能规划绘图任务。

====================
用户问题
====================

{question}

====================
数据集信息
====================

{dataset_info}

{health_text}

{knowledge_text}

====================
已经执行的分析结果
====================

{execution_results}

====================
Reviewer 指出的缺失任务
====================

{missing_tasks}

====================
之前的对话
====================

{history_text}

====================
规划要求
====================

1. 第一次规划时，完整拆解用户的问题，规划必要的数据计算和统计分析任务。

2. 如果已经存在执行结果：
   - 不要重复已经完成的分析
   - 只规划尚未完成的分析任务

3. 如果 Reviewer 给出了 missing_tasks：
   - 优先补充尚未完成的缺失分析任务
   - 不要重复已经完成且结果有效的任务

4. 每一个任务只能使用以下工具：
   - run_stat_calc

5. 执行阶段由模型通过原生 Tool Calling 自行选择工具，
   实际具备的能力是：
   - 查看字段、类型、缺失值和行数
   - 按一到多个维度聚合一个数值字段，
     分组字段是日期时可以直接指定时间粒度（按年、季度、月、周、日）
   - 查看单个数值字段的分布（均值、中位数、分位数、标准差等）

   所以任务描述里说清楚「算什么指标、按什么维度分组、用什么聚合方式」就够了，
   不需要指定具体工具。

6. 规划「分析目标」，不要规划「准备工作」。

   不要规划「生成季度字段」「把日期转成月份」这类造字段的任务 ——
   聚合能力本身就能按时间粒度直接分组。
   也不要把一个分析拆成「先造字段、再统计」两步，
   直接写成一个聚合任务即可。

7. 严禁使用 plot_chart。
   严禁规划绘图、生成图表、保存图片等任务。
   所有可视化任务由独立的 Chart Planner / Chart Executor 处理。

8. 不要使用不存在的工具。

9. 不要引用数据集中不存在的字段。
   字段名称必须与数据集信息保持一致。

10. 每个任务必须是明确、可以独立执行的数据分析任务。
    需要计算时，说明具体的计算目标和所需字段。

11. 如果当前已经没有需要执行的分析任务，返回空 plan。
12. 如果「之前的对话」里已经有分析结果，
    而用户这次是在追问
    （例如「只看华东地区」「再按季度拆一下」「排除退款订单」），
    只需要规划回应这次追问所必需的任务，
    不要重复已经完成的分析。

====================
返回格式
====================

必须严格返回 JSON：

{{
    "plan": [
        {{
            "step": 1,
            "task": "计算各地区销售额总和并进行比较",
            "tool": "run_stat_calc"
        }}
    ]
}}

不要返回 Markdown。
不要添加 JSON 之外的任何文字。
"""

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 DataMind 数据分析智能体中的 Planner。"
                    "你只负责数据计算和统计分析。"
                    "禁止规划任何绘图任务，禁止使用 plot_chart。"
                    "必须严格返回 JSON，不要返回 Markdown。"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        response = call_llm(messages)
        response = response.strip()

        if response.startswith("```"):
            response = response.replace("```json", "")
            response = response.replace("```JSON", "")
            response = response.replace("```", "")

        result = json.loads(response)

        plan = result.get("plan", [])

        if not isinstance(plan, list):
            raise ValueError("Planner 返回的 plan 必须是列表")

        # 防止模型偶尔违反提示词，生成旧式绘图任务
        validated_plan = []

        for index, task in enumerate(plan, start=1):
            if not isinstance(task, dict):
                continue

            tool = task.get("tool")

            # 可视化已经由 Chart Planner / Chart Executor 负责
            # plot_chart 不需要单独判断，
            # 下面的白名单校验会一并拒绝它。

            # 拒绝任何不在白名单中的工具
            if tool != "run_stat_calc":
                continue

            task["step"] = index
            validated_plan.append(task)

        return {
            "plan": validated_plan,
            "current_step": 0,
            "error": None
        }

    except Exception as e:
        return {
            "plan": [],
            "current_step": 0,
            "error": (
                f"Planner 异常："
                f"{type(e).__name__}: {str(e)}"
            )
        }

