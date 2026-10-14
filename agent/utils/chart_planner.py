"""
=========================================
DataMind V3.5.1
Chart Planner

职责：

1. 判断当前用户问题是否需要图表
2. 如果需要，选择合适的图表类型
3. 推断图表使用的字段
4. 返回结构化图表计划

注意：

本模块只负责“图表规划”。

暂时不负责：

- 生成 Matplotlib 代码
- 执行绘图
- 保存图片
- 修改现有 Agent Graph
=========================================
"""

import json
import re
from typing import Any, Dict, List

from services.llm_service import call_llm

from agent.utils.time_granularity import (
    normalize as normalize_time_granularity
)


# ========================================
# 支持的图表类型
# ========================================

SUPPORTED_CHART_TYPES = {
    "bar": "柱状图",
    "line": "折线图",
    "pie": "饼图",
    "histogram": "直方图",
    "scatter": "散点图",
    "horizontal_bar": "水平柱状图",
}


# ========================================
# Chart Planner Prompt
# ========================================

def build_chart_planner_prompt(
    question: str,
    dataset_info: str
) -> str:
    """
    构造 Chart Planner 的 Prompt。
    """

    chart_type_description = "\n".join(
        [
            f"- {key}: {value}"
            for key, value in SUPPORTED_CHART_TYPES.items()
        ]
    )

    return f"""
你是 DataMind 的 Chart Planner，负责为数据分析任务规划合适的图表。

你的职责不是生成 Python 代码，而是根据用户问题和数据集字段，
判断是否需要可视化，并返回结构化 JSON。

====================
用户问题
====================

{question}

====================
数据集信息
====================

{dataset_info}

====================
支持的图表类型
====================

{chart_type_description}

====================
决策规则
====================

1. 如果用户只要求计算一个汇总数值，通常不需要图表。
2. 如果用户要求趋势、变化、增长、时间走势，优先使用 line。
3. 如果用户要求比较不同地区、类别、产品或渠道，优先使用 bar。
4. 如果用户要求占比、构成、比例，通常使用 pie。
5. 如果用户要求数据分布、频数、区间分布，使用 histogram。
6. 如果用户要求分析两个数值字段之间的关系，使用 scatter。
7. 如果类别名称较长，或者需要展示 Top N，优先使用 horizontal_bar。
8. 只能使用数据集中真实存在的字段。
9. 不要编造数据集不存在的字段。
10. 如果字段无法确定，可以将字段设置为 null。
11. 图表数量：**默认只规划 1 张**。
    只有当用户问题里确实包含多个彼此独立的分析角度时，
    才规划 2-3 张（最多 3 张）。
    宁少勿多：一张图能讲清楚，就不要拆成两张。
12. 图表必须服务于用户问题。
    不要为了画图而画图，
    也不要为了让结果看起来丰富而多画。
13. 如果 x_field 是日期 / 时间字段
    （字段名里带「日期」「时间」「date」「time」，
    或者该字段的数据类型本身就是日期时间），
    并且用户想看趋势、变化、周期、走势，
    必须显式给出 time_granularity，不要留 null。
    选择原则：
      - 数据跨度超过 2 年   → year
      - 数据跨度超过 6 个月 → quarter
      - 数据跨度超过 2 个月 → month
      - 数据跨度超过 2 周   → week
      - 其它                → day
14. 如果 x_field 不是日期字段，time_granularity 必须是 null。
15. 如果 time_granularity 不是 null，
    aggregation 不能是 none，必须明确写成 sum、mean 或 count。

====================
必须返回的 JSON 结构
====================

如果不需要图表：

{{
  "need_chart": false,
  "charts": [],
  "reason": "说明为什么不需要图表"
}}

如果需要图表：

{{
  "need_chart": true,
  "charts": [
    {{
      "chart_type": "bar",
      "title": "图表标题",
      "reason": "选择该图表的原因",
      "x_field": "真实字段名或 null",
      "y_field": "真实字段名或 null",
      "aggregation": "sum / mean / count / none",
      "time_granularity": "year / quarter / month / week / day / null",
      "priority": 1
    }}
  ],
  "reason": "整体可视化规划说明"
}}

注意：

1. 只返回 JSON。
2. 不要 Markdown。
3. 不要使用 ``` 包裹。
4. 不要输出额外解释。
"""


# ========================================
# 清理模型返回内容
# ========================================

def clean_json_text(
    text: str
) -> str:
    """
    清理 LLM 可能返回的 Markdown JSON 代码块。
    """

    if not text:
        return ""

    text = text.strip()

    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    return text.strip()


# ========================================
# 默认返回结构
# ========================================

def empty_chart_plan(
    reason: str
) -> Dict[str, Any]:
    """
    返回不需要图表时的标准结构。
    """

    return {
        "need_chart": False,
        "charts": [],
        "reason": reason
    }


# ========================================
# 校验单个图表计划
# ========================================

def validate_chart_item(
    chart: Dict[str, Any]
) -> Dict[str, Any]:
    """
    对单个图表计划进行基础校验。

    这里只做结构校验，
    不验证字段是否真的存在于 DataFrame。
    """

    if not isinstance(chart, dict):
        return {}

    chart_type = chart.get(
        "chart_type"
    )

    if chart_type not in SUPPORTED_CHART_TYPES:
        chart_type = "bar"

    aggregation = chart.get(
        "aggregation",
        "none"
    )

    if aggregation not in {
        "sum",
        "mean",
        "count",
        "none"
    }:
        aggregation = "none"

    # V3.7：
    # 之前这里会把 Planner 显式给出的 time_granularity 整条丢掉，
    # 于是 chart_executor 里「优先读取显式粒度」的分支永远走不到，
    # 只能靠标题关键词猜。
    time_granularity = normalize_time_granularity(
        chart.get("time_granularity")
    )

    priority = chart.get(
        "priority",
        1
    )

    if not isinstance(priority, int):
        priority = 1

    return {
        "chart_type": chart_type,

        "title": chart.get(
            "title",
            "数据分析图表"
        ),

        "reason": chart.get(
            "reason",
            ""
        ),

        "x_field": chart.get(
            "x_field"
        ),

        "y_field": chart.get(
            "y_field"
        ),

        "aggregation": aggregation,

        "time_granularity": time_granularity,

        "priority": priority
    }


# ========================================
# 校验完整图表规划
# ========================================

def validate_chart_plan(
    plan: Dict[str, Any]
) -> Dict[str, Any]:
    """
    校验并标准化 Chart Planner 输出。
    """

    if not isinstance(plan, dict):
        return empty_chart_plan(
            "Chart Planner 返回格式不正确"
        )

    need_chart = plan.get(
        "need_chart",
        False
    )

    if not isinstance(need_chart, bool):
        need_chart = False

    raw_charts = plan.get(
        "charts",
        []
    )

    if not isinstance(raw_charts, list):
        raw_charts = []

    charts: List[Dict[str, Any]] = []

    for raw_chart in raw_charts[:3]:

        chart = validate_chart_item(
            raw_chart
        )

        if chart:
            charts.append(chart)

    if not charts:
        need_chart = False

    return {
        "need_chart": need_chart,

        "charts": charts,

        "reason": plan.get(
            "reason",
            ""
        )
    }


# ========================================
# 主函数
# ========================================

def plan_charts(
    question: str,
    dataset_info: str
) -> Dict[str, Any]:
    """
    调用 LLM，生成图表规划。

    参数：
        question:
            用户问题

        dataset_info:
            数据集字段及基本信息

    返回：
        标准化 Chart Plan
    """

    if not question:
        return empty_chart_plan(
            "用户问题为空"
        )

    if not dataset_info:
        return empty_chart_plan(
            "数据集信息为空，无法规划图表"
        )

    prompt = build_chart_planner_prompt(
        question=question,
        dataset_info=dataset_info
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是一个严谨的数据可视化规划器。"
                "必须严格返回合法 JSON。"
            )
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    try:

        raw_response = call_llm(
            messages
        )

        cleaned_response = clean_json_text(
            raw_response
        )

        parsed_plan = json.loads(
            cleaned_response
        )

        return validate_chart_plan(
            parsed_plan
        )

    except json.JSONDecodeError as e:

        return empty_chart_plan(
            f"Chart Planner JSON 解析失败：{str(e)}"
        )

    except Exception as e:

        return empty_chart_plan(
            f"Chart Planner 执行失败：{str(e)}"
        )