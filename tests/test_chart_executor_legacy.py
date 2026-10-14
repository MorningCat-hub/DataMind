"""
DataMind Chart Executor 测试（pytest 版）

由 4 个遗留独立脚本合并而来：

- test_chart_executor.py              基础单图生成
- test_chart_executor_standardized.py 标准化返回字段
- test_chart_executor_multi.py        多图表类型
- test_chart_executor_error.py        异常场景的结构化返回

与原脚本的差异：

1. 用 ``assert`` 取代 ``print`` 报告 + ``sys.exit``，
   不再有自定义的 check() 收集器；
2. 所有 PNG 都写进 pytest 的 ``tmp_path``，
   不会在项目目录里留下 ``temp_charts/``；
3. 数据集使用「项目根目录 + 文件名」的绝对路径，
   不依赖当前工作目录；
4. 每个断言都带上 ``result`` / 具体值作为失败信息。

本文件不调用任何模型，也不需要 API key。
"""

import os
import sys

import pytest

# =========================================================
# 项目根目录与数据集路径
# =========================================================

# 允许显式指定项目根（例如本文件还没被放进 <项目>/tests/ 时）
_PROJECT_OVERRIDE = os.environ.get("DATAMIND_PROJECT_ROOT")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # tests/ 的上一级
if not os.path.isfile(os.path.join(PROJECT, "test_chart_data.csv")):
    PROJECT = os.path.dirname(PROJECT)   # 兜底

if _PROJECT_OVERRIDE:
    PROJECT = _PROJECT_OVERRIDE

# 需要在项目根目录下才能 import agent...
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from agent.utils.chart_executor import execute_chart  # noqa: E402


DATASET_PATH = os.path.join(PROJECT, "test_chart_data.csv")

# 原始数据 8 行；按「地区」聚合（sum）后应剩下 3 个类别
EXPECTED_CATEGORY_COUNT = 3


# =========================================================
# fixture / 工具
# =========================================================

@pytest.fixture(autouse=True)
def isolated_chart_output(monkeypatch, tmp_path):
    """
    把图表输出目录指向 pytest 临时目录。

    chart_executor 只通过模块级常量 CHART_OUTPUT_DIR
    决定 ``os.makedirs`` 与 PNG 的落盘位置，
    因此改写它就能保证不往项目目录里写文件。
    """

    monkeypatch.setattr(
        "agent.utils.chart_executor.CHART_OUTPUT_DIR",
        str(tmp_path),
    )

    return tmp_path


def make_plan(
    chart_type="bar",
    title="各地区销售额对比",
    x_field="地区",
    y_field="销售额",
    aggregation="sum",
):
    """构造一个标准的图表计划，字段与原脚本保持一致。"""

    return {
        "chart_type": chart_type,
        "title": title,
        "reason": "比较不同地区销售额",
        "x_field": x_field,
        "y_field": y_field,
        "aggregation": aggregation,
        "priority": 1,
    }


@pytest.fixture
def bar_plan():
    return make_plan()


@pytest.fixture
def bar_result(bar_plan, isolated_chart_output):
    """
    基础柱状图的执行结果（成功场景）。

    显式依赖 isolated_chart_output，
    保证绘图时 CHART_OUTPUT_DIR 已经被替换成 tmp_path。
    """

    return execute_chart(
        file_path=DATASET_PATH,
        chart_plan=bar_plan,
    )


def assert_is_structured_error(result):
    """
    异常场景的统一断言：不能成功，但必须返回结构化错误。

    注意：这里故意不锁定具体的 error["type"]，
    原有的独立脚本也只要求「不抛异常 + success 为 False + error 非空」。
    """

    assert isinstance(result, dict), result

    # 异常场景下不能成功，但也不能直接抛异常
    # （真的抛异常的话，测试会在这里之前就报错）
    assert result.get("success") is False, result

    error = result.get("error")
    assert error, result
    assert isinstance(error, dict), error
    assert error.get("type"), error
    assert error.get("message"), error

    # 失败时不应留下任何产物
    assert result.get("chart_path") is None, result
    assert result.get("data_summary") is None, result


# =========================================================
# 1. 基础：单张柱状图能画出来（test_chart_executor.py）
# =========================================================

def test_bar_chart_returns_existing_png(bar_result, isolated_chart_output):
    """execute_chart 返回 success 且 PNG 真实存在、非空。"""

    assert bar_result["success"] is True, bar_result

    chart_path = bar_result["chart_path"]
    assert chart_path, bar_result
    assert os.path.exists(chart_path), chart_path
    assert os.path.isfile(chart_path), chart_path
    assert os.path.getsize(chart_path) > 0, chart_path

    # 隔离验证：图片必须落在本次测试的 tmp_path 里
    assert (
        os.path.dirname(os.path.abspath(chart_path))
        == str(isolated_chart_output)
    ), chart_path


# =========================================================
# 2. 标准化返回（test_chart_executor_standardized.py）
# =========================================================

REQUIRED_SUCCESS_FIELDS = [
    "success",
    "chart_path",
    "chart_type",
    "title",
    "description",
    "data_summary",
    "error",
    "warnings",
]


def test_success_result_has_all_required_fields(bar_result):
    """标准化返回结构里必须带齐所有字段。"""

    for field in REQUIRED_SUCCESS_FIELDS:
        assert field in bar_result, (
            f"缺少返回字段：{field}；实际返回：{bar_result}"
        )

    assert isinstance(bar_result["warnings"], list), bar_result


def test_success_result_metadata_matches_plan(bar_result, bar_plan):
    """图表类型、标题、说明、error 等元信息正确。"""

    assert bar_result["success"] is True, bar_result

    assert bar_result["chart_type"] == "bar", bar_result
    assert bar_result["title"] == "各地区销售额对比", bar_result

    assert bar_result["description"] is not None, bar_result
    assert isinstance(bar_result["description"], str), bar_result
    assert bar_result["description"].strip(), bar_result

    assert bar_result["data_summary"] is not None, bar_result

    # 成功场景下 error 必须为 None
    assert bar_result["error"] is None, bar_result

    # 成功场景不应有降级 warning（x_field 是文本字段，不涉及时间聚合）
    assert bar_result["warnings"] == [], bar_result


def test_data_summary_matches_plan(bar_result):
    """data_summary 反映真实的聚合口径。"""

    data_summary = bar_result["data_summary"]

    assert data_summary["row_count"] == EXPECTED_CATEGORY_COUNT, (
        f"按「地区」sum 聚合后应剩 "
        f"{EXPECTED_CATEGORY_COUNT} 类；实际：{data_summary}"
    )

    assert data_summary["x_field"] == "地区", data_summary
    assert data_summary["y_field"] == "销售额", data_summary
    assert data_summary["aggregation"] == "sum", data_summary

    # 「地区」是文本字段，不应被误判成日期而做时间聚合
    assert data_summary.get("time_granularity") is None, data_summary

    categories = data_summary.get("categories")
    assert categories, data_summary
    assert len(categories) == EXPECTED_CATEGORY_COUNT, data_summary


# =========================================================
# 3. 多图表类型（test_chart_executor_multi.py）
# =========================================================

# bar 由上面的基础测试覆盖，这里覆盖其余 5 种类型
MULTI_CHART_PLANS = [
    {
        "chart_type": "line",
        "title": "各地区销售额趋势",
        "x_field": "地区",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    },
    {
        "chart_type": "pie",
        "title": "各地区销售额占比",
        "x_field": "地区",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    },
    {
        "chart_type": "histogram",
        "title": "销售额分布",
        "x_field": "销售额",
        "y_field": None,
        "aggregation": "none",
        "priority": 1,
    },
    {
        "chart_type": "scatter",
        "title": "销售额与利润关系",
        "x_field": "销售额",
        "y_field": "利润",
        "aggregation": "none",
        "priority": 1,
    },
    {
        "chart_type": "horizontal_bar",
        "title": "各地区销售额对比",
        "x_field": "地区",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    },
]


@pytest.mark.parametrize(
    "chart_plan",
    MULTI_CHART_PLANS,
    ids=[plan["chart_type"] for plan in MULTI_CHART_PLANS],
)
def test_each_chart_type_renders(chart_plan, isolated_chart_output):
    """6 种图表类型（bar 见基础测试）都能生成真实存在的 PNG。"""

    result = execute_chart(
        file_path=DATASET_PATH,
        chart_plan=chart_plan,
    )

    assert result.get("success") is True, result

    assert result.get("chart_type") == chart_plan["chart_type"], result

    chart_path = result.get("chart_path")
    assert chart_path, result
    assert os.path.exists(chart_path), result

    # 隔离验证：图片必须落在 tmp_path 里
    assert (
        os.path.dirname(os.path.abspath(chart_path))
        == str(isolated_chart_output)
    ), chart_path


# =========================================================
# 4. 异常场景（test_chart_executor_error.py）
# =========================================================

ERROR_CHART_PLANS = [
    # 字段不存在
    make_plan(
        title="不存在字段测试",
        x_field="不存在的地区字段",
    ),
    # 不支持的图表类型
    make_plan(
        chart_type="unknown_chart",
        title="不支持类型测试",
    ),
    # 缺少必要字段
    make_plan(
        title="缺少字段测试",
        x_field=None,
    ),
]


@pytest.mark.parametrize(
    "chart_plan",
    ERROR_CHART_PLANS,
    ids=["unknown_field", "unsupported_chart_type", "missing_x_field"],
)
def test_error_scenarios_return_structured_result(
    chart_plan,
    isolated_chart_output,
):
    """异常输入不能让 execute_chart 崩溃，要返回结构化错误。"""

    result = execute_chart(
        file_path=DATASET_PATH,
        chart_plan=chart_plan,
    )

    assert_is_structured_error(result)


def test_missing_dataset_returns_structured_result(
    tmp_path,
    isolated_chart_output,
):
    """数据集文件不存在时返回结构化错误（不抛异常）。"""

    missing_path = str(tmp_path / "not_exist.csv")

    # 保证这个文件确实不存在
    assert not os.path.exists(missing_path), missing_path

    result = execute_chart(
        file_path=missing_path,
        chart_plan=make_plan(title="测试图表"),
    )

    assert_is_structured_error(result)
