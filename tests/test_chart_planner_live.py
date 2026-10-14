"""
真机用例：会调用真实模型，默认跳过。

运行方式：

    set DATAMIND_LIVE=1
    pytest -m live

注意：

`agent.utils.chart_planner` 在模块顶层 import `services.llm_service`，
而后者在 import 时就 `load_dotenv()` 并构造 OpenAI 客户端 ——
也就是说「有没有 key」这件事在**收集阶段**就会发生。
所以这里的 import 一律放进测试函数体内，不要在模块顶层裸 import。

断言只覆盖结构不变量：模型的输出不稳定，
不要断言具体图数量、具体字段名或中文标题。
"""

import os

import pytest

pytestmark = pytest.mark.live

LIVE = os.getenv("DATAMIND_LIVE") == "1"

SUPPORTED_TYPES = {
    "bar", "line", "pie", "histogram", "scatter", "horizontal_bar",
}


@pytest.mark.skipif(not LIVE, reason="设置 DATAMIND_LIVE=1 才会跑真机用例")
def test_plan_charts_returns_a_parsable_plan(sales_csv):

    from agent.utils.chart_planner import plan_charts

    result = plan_charts(
        "分析各地区销售额并比较差异",
        "数据集字段：订单号、订单日期、地区、品类、销售额",
    )

    assert isinstance(result, dict)
    assert isinstance(result.get("need_chart"), bool)

    for chart in result.get("charts") or []:

        assert chart.get("chart_type") in SUPPORTED_TYPES, chart
        assert "x_field" in chart
        assert "y_field" in chart


@pytest.mark.skipif(not LIVE, reason="设置 DATAMIND_LIVE=1 才会跑真机用例")
def test_plan_charts_tolerates_an_irrelevant_question(sales_csv):
    """问法和分析无关时，允许多模型说不画图，不该判失败。"""

    from agent.utils.chart_planner import plan_charts

    result = plan_charts(
        "你好",
        "数据集字段：订单号、订单日期、地区、品类、销售额",
    )

    assert isinstance(result, dict)
    assert isinstance(result.get("need_chart"), bool)
