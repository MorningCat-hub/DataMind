"""
DataMind 时间粒度（time_granularity）测试

由 `_scratch/v37_verify.py` 中**与时间粒度相关**的部分移植而来，覆盖：

1. ``normalize()`` 的别名归一化（季度 / Q / monthly / 按月 …）；
2. ``detect_from_text()`` 从标题、说明里识别明确的粒度关键词；
3. ``select_by_span()`` 按真实日期跨度自动选粒度；
4. 优先级：**显式指定 > 标题关键词 > 按跨度自动选**
   （通过 ``resolve_time_granularity`` 验证，含
   「explicit beats the 季度 keyword in the title」）；
5. 非法 / 未知粒度的处理（校验时归零、聚合时降级而不是抛异常）；
6. ``validate_chart_item`` / ``validate_chart_plan`` 是否保留
   ``time_granularity``。

**故意不包含**绘图链路的断言 —— ``execute_chart`` 的返回、
分组桶数（9 个 / 3 个）、标签顺序、PNG 落盘、description 文案等
由另一个文件负责，这里不重复。

三条约束：

- 纯 pytest：``assert`` + ``pytest.mark.parametrize``，
  没有自定义 ``check()`` 收集器，也没有 ``sys.exit``；
- 完全离线：不调用任何模型（conftest 的 ``no_real_llm`` 会兜底拦截）；
- 不往项目目录写文件：本模块只读数据集，连 ``tmp_path`` 都不需要。
"""

import os
import sys

import pandas as pd
import pytest

# ========================================
# 项目根目录
#
# 本文件预期位于 <项目>/tests/，所以项目根 = 上一级；
# 若那一层没有 agent/（例如被临时放到更深一层），再往上兜底一层。
# 定位完全基于 __file__，与当前工作目录无关。
# ========================================

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if not os.path.isdir(os.path.join(PROJECT, "agent")):
    PROJECT = os.path.dirname(PROJECT)

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# services.llm_service 在**导入时**就会构造 OpenAI 客户端，
# 没有 key 会直接抛 OpenAIError。conftest 已经设过占位值，
# 这里再 setdefault 一次，保证本模块单独被导入时也不会去读 .env 里的真 key。
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")

from agent.utils.chart_executor import (  # noqa: E402
    aggregate_data,
    resolve_time_granularity,
)
from agent.utils.chart_planner import (  # noqa: E402
    validate_chart_item,
    validate_chart_plan,
)
from agent.utils.time_granularity import (  # noqa: E402
    SUPPORTED_GRANULARITIES,
    detect_from_text,
    normalize,
    select_by_span,
)


# ========================================
# 夹具与构造器
# ========================================

@pytest.fixture
def sales_df(sales_csv):
    """
    40 行销售数据（日期跨度 244 天）。

    用函数级夹具：``aggregate_data`` 内部会 copy，
    但这里仍然不跨用例共享可变 DataFrame。
    """

    return pd.read_csv(sales_csv)


def make_plan(**overrides):
    """
    构造一个标准的折线图计划；字段名与源脚本保持一致。
    """

    plan = {
        "chart_type": "line",
        "title": "销售额趋势",
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    }

    plan.update(overrides)

    return plan


# ========================================
# 1. normalize()：别名归一化
# ========================================

# 源脚本显式覆盖的 4 个别名（季度 / Q / monthly / 按月）+ 其余已注册别名
NORMALIZE_ALIASES = {
    "chinese_quarter": ("季度", "quarter"),
    "upper_q": ("Q", "quarter"),
    "english_quarterly": ("quarterly", "quarter"),
    "english_monthly": ("monthly", "month"),
    "chinese_monthly": ("按月", "month"),
    "chinese_month": ("月份", "month"),
    "chinese_month_short": ("月", "month"),
    "chinese_year": ("年度", "year"),
    "chinese_by_year": ("按年", "year"),
    "chinese_by_week": ("按周", "week"),
    "chinese_date": ("日期", "day"),
    "english_year": ("year", "year"),
    "single_letter_d": ("d", "day"),
}

# 无法识别 -> None；None / "none" 是源脚本显式覆盖的两条
NORMALIZE_UNKNOWN = {
    "python_none": (None, None),
    "lowercase_none": ("none", None),
    "null_word": ("null", None),
    "nan_word": ("NaN", None),
    "empty": ("", None),
    "blank": ("  ", None),
    "bogus": ("每个世纪", None),
}


@pytest.mark.parametrize(
    "raw,expected",
    list(NORMALIZE_ALIASES.values()) + list(NORMALIZE_UNKNOWN.values()),
    ids=list(NORMALIZE_ALIASES) + list(NORMALIZE_UNKNOWN),
)
def test_normalize_maps_alias_to_standard_granularity(raw, expected):
    """别名（含中文、英文、单字母）都要归一化成标准粒度。"""

    result = normalize(raw)

    assert result == expected, f"normalize({raw!r}) 期望 {expected!r}，实际 {result!r}"

    if expected is not None:
        assert result in SUPPORTED_GRANULARITIES, result


def test_normalize_unknown_is_none_not_a_guess():
    """未知粒度必须返回 None，不能瞎猜成最接近的粒度。"""

    assert normalize("每个世纪") is None, normalize("每个世纪")
    assert normalize("每两周") is None, normalize("每两周")


# ========================================
# 2. detect_from_text()：从文本识别粒度
# ========================================

DETECT_CASES = {
    # 源脚本的 4 条
    "quarter_keyword": ("各季度销售额趋势", "quarter"),
    "month_keyword": ("每月销售额变化", "month"),
    "vague_cycle_is_not_guessed": ("不同周期销售额总和趋势", None),
    "vague_trend_is_not_guessed": ("销售额变化趋势", None),
    # 补充：同一个关键词表里的其它写法
    "quarter_short": ("季度销售额趋势", "quarter"),
    "month_word": ("月度销售额", "month"),
    # 纯类别标题里没有时间词
    "category_title": ("各地区销售额对比", None),
    "no_text": (None, None),
    "empty_text": ("", None),
}


@pytest.mark.parametrize(
    "text,expected",
    list(DETECT_CASES.values()),
    ids=list(DETECT_CASES),
)
def test_detect_from_text_finds_explicit_keyword(text, expected):
    """
    只认「明确」的关键词。

    「周期」「趋势」这类模糊说法必须返回 None，
    交给 select_by_span() 按真实数据跨度决定。
    """

    result = detect_from_text(text)

    assert result == expected, f"detect_from_text({text!r}) 期望 {expected!r}，实际 {result!r}"


# ========================================
# 3. select_by_span()：按跨度自动选粒度
# ========================================

def _daily_series():
    """8 天，逐日。"""

    return pd.Series(pd.date_range("2026-01-01", periods=8, freq="D"))


def _half_year_series():
    """244 天（2026-01-05 ~ 2026-09-06），40 个点。"""

    return pd.Series(pd.date_range("2026-01-05", "2026-09-06", periods=40))


def _three_year_series():
    """约 3.3 年。"""

    return pd.Series(pd.date_range("2024-01-01", periods=40, freq="30D"))


SPAN_CASES = {
    "7_days_day": (_daily_series, "day"),
    "244_days_month": (_half_year_series, "month"),
    "3_years_quarter": (_three_year_series, "quarter"),
}


@pytest.mark.parametrize(
    "factory,expected",
    list(SPAN_CASES.values()),
    ids=list(SPAN_CASES),
)
def test_select_by_span_keeps_axis_readable(factory, expected):
    """
    在桶数不超过上限的前提下选最细的粒度：

    7 天 -> day，244 天 -> month，约 3 年 -> quarter。
    """

    dates = factory()

    span_days = (dates.max() - dates.min()).days

    result = select_by_span(dates)

    assert result == expected, (
        f"跨度 {span_days} 天应选 {expected!r}，实际 {result!r}"
    )

    assert result in SUPPORTED_GRANULARITIES, result


# ========================================
# 4. 优先级：显式 > 关键词 > 按跨度自动选
# ========================================

PRIORITY_CASES = {
    # 源脚本的 regression：标题写着「季度」，但 Planner 显式要求 month
    "explicit_month_beats_quarter_keyword": (
        {"title": "季度销售额趋势", "time_granularity": "month"},
        "month",
    ),
    # 显式值本身也要走别名归一化
    "explicit_alias_beats_month_keyword": (
        {"title": "月度销售额趋势", "time_granularity": "季度"},
        "quarter",
    ),
    # 没有显式值时，标题关键词说了算
    "quarter_keyword_when_not_explicit": (
        {"title": "季度销售额趋势"},
        "quarter",
    ),
    "month_keyword_when_not_explicit": (
        {"title": "月度销售额趋势"},
        "month",
    ),
    # 关键词缺失（「周期」「趋势」故意不猜）才按跨度自动选
    "vague_wording_falls_back_to_span": (
        {"title": "不同周期销售额总和趋势"},
        "month",
    ),
    "no_hint_falls_back_to_span": (
        {"title": "销售额趋势"},
        "month",
    ),
}


@pytest.mark.parametrize(
    "overrides,expected",
    list(PRIORITY_CASES.values()),
    ids=list(PRIORITY_CASES),
)
def test_resolution_priority_explicit_then_keyword_then_span(
    sales_df, overrides, expected
):
    """
    优先级：显式指定 > 标题/说明关键词 > 按跨度自动选。

    销售数据的跨度是 244 天，自动档的期望值就是 month。
    """

    plan = make_plan(**overrides)

    result = resolve_time_granularity(sales_df, plan)

    assert result == expected, (
        f"计划 {plan}（跨度 244 天）期望 {expected!r}，实际 {result!r}"
    )


# ========================================
# 5. x_field 不是日期 -> 一律不做时间聚合
# ========================================

NON_DATE_CASES = {
    # 真实的 hijack bug：标题有日期关键词，但 x 轴是订单号
    "date_keyword_but_id_x_field": (
        {
            "chart_type": "line",
            "title": "季度销售额趋势",
            "reason": "按季度看变化",
            "x_field": "订单号",
            "y_field": "销售额",
            "aggregation": "sum",
            "priority": 1,
        },
        None,
    ),
    # 数值列永远不能当成日期（pd.to_datetime 会把 int 当成 1970 纳秒）
    "numeric_x_field": (
        {
            "chart_type": "line",
            "x_field": "数量",
            "y_field": "销售额",
            "aggregation": "sum",
        },
        None,
    ),
    # 字段不存在
    "missing_x_field": (
        {
            "chart_type": "bar",
            "x_field": "不存在的地区字段",
            "y_field": "销售额",
            "aggregation": "sum",
        },
        None,
    ),
    # x_field 是 None
    "none_x_field": (
        {
            "chart_type": "bar",
            "x_field": None,
            "y_field": "销售额",
            "aggregation": "sum",
        },
        None,
    ),
}


@pytest.mark.parametrize(
    "plan,expected",
    list(NON_DATE_CASES.values()),
    ids=list(NON_DATE_CASES),
)
def test_non_date_x_field_disables_time_aggregation(sales_df, plan, expected):
    """
    x_field 必须真实存在且真的能解析成日期，否则返回 None。

    这是 v3.7 修掉的 hijack bug：标题里的「季度」
    不应该让一张按订单号分组的图去做时间聚合。
    """

    result = resolve_time_granularity(sales_df, plan)

    assert result == expected, (
        f"x_field={plan.get('x_field')!r} 不应启用时间聚合，实际 {result!r}"
    )


# ========================================
# 6. Planner 校验必须保留 time_granularity
# ========================================

# 源脚本 3 条：季度 -> quarter、month 原样保留、未知粒度 -> None
PLANNER_ITEM_CASES = {
    "chinese_alias_kept": ("季度", "quarter"),
    "standard_value_kept": ("month", "month"),
    "unknown_becomes_none": ("每个世纪", None),
    "missing_becomes_none": (None, None),
    "empty_becomes_none": ("", None),
}


@pytest.mark.parametrize(
    "raw,expected",
    list(PLANNER_ITEM_CASES.values()),
    ids=list(PLANNER_ITEM_CASES),
)
def test_validate_chart_item_keeps_time_granularity(raw, expected):
    """
    v3.7 之前这里会把 Planner 显式给出的 time_granularity 整条丢掉，
    导致「显式粒度」这条路径永远走不到。
    """

    item = validate_chart_item({
        "chart_type": "line",
        "title": "各季度销售额趋势",
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": "sum",
        "time_granularity": raw,
        "priority": 1,
    })

    assert item.get("time_granularity") == expected, (
        f"time_granularity={raw!r} 期望 {expected!r}，"
        f"实际 {item.get('time_granularity')!r}（整条 item：{item}）"
    )


def test_validate_chart_item_unknown_granularity_becomes_none():
    """未知粒度必须被归零，不能透传到绘图链路。"""

    item = validate_chart_item({
        "chart_type": "line",
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": "sum",
        "time_granularity": "每个世纪",
    })

    assert item.get("time_granularity") is None, item


def test_validate_chart_plan_keeps_time_granularity():
    """粒度要能一路穿过整份 plan，而不只是单个 item。"""

    plan = validate_chart_plan({
        "need_chart": True,
        "charts": [{
            "chart_type": "line",
            "title": "月度趋势",
            "x_field": "订单日期",
            "y_field": "销售额",
            "aggregation": "sum",
            "time_granularity": "month",
            "priority": 1,
        }],
        "reason": "趋势",
    })

    assert plan["charts"], plan

    assert plan["charts"][0].get("time_granularity") == "month", plan["charts"][0]


# ========================================
# 7. 非法粒度在聚合时的处理：降级，而不是抛异常
# ========================================

def _degrade_case(sales_df):
    """按源脚本的调用方式跑一次「不存在的粒度」。"""

    return aggregate_data(
        sales_df,
        {
            "chart_type": "bar",
            "x_field": "订单日期",
            "y_field": "销售额",
            "aggregation": "sum",
        },
        "不存在的粒度",
    )


def test_unknown_granularity_degrades_instead_of_raising(sales_df):
    """非法粒度不能让整张图失败，要降级为不聚合并留下 warning。"""

    degraded, warnings = _degrade_case(sales_df)

    assert len(degraded) > 0, f"降级后不该是空数据：{degraded}"

    assert any("降级" in w for w in warnings), (
        f"降级必须被记录成 warning，实际 warnings={warnings}"
    )


def test_degraded_result_keeps_raw_dates_and_raw_grouping(sales_df):
    """
    降级后保留原始日期、按原始 x_field 分组，不做任何时间聚合。
    """

    degraded, _warnings = _degrade_case(sales_df)

    date_values = [str(v) for v in degraded["订单日期"]]

    # 原生日期是 "YYYY-MM-DD"，时间标签则是 "2026-01" / "2026Q1" 之类
    assert all(len(v) == 10 for v in date_values), (
        f"降级后应保留原始日期（10 字符），实际前 3 个：{date_values[:3]}"
    )

    expected_rows = sales_df["订单日期"].nunique()

    assert len(degraded) == expected_rows, (
        f"降级后应按原始 x_field 分组，期望 {expected_rows} 行，"
        f"实际 {len(degraded)} 行"
    )


def test_granularity_without_aggregation_falls_back_to_sum(sales_df):
    """
    计划里给了粒度、却写 aggregation="none" 时，
    必须自动按 sum 聚合（并说明原因），否则同一个时间标签会留下多个点。
    """

    result, warnings = aggregate_data(
        sales_df,
        {
            "chart_type": "line",
            "title": "月度销售额",
            "x_field": "订单日期",
            "y_field": "销售额",
            "aggregation": "none",
        },
        "month",
    )

    assert any("sum" in w for w in warnings), (
        f"隐式 sum 必须被记录成 warning，实际 warnings={warnings}"
    )

    # 每个时间桶只能有一个点（不锁定具体桶数，桶数由绘图链路负责）
    assert len(result) == result["订单日期"].nunique(), (
        f"每个时间桶只能有一行，实际 {len(result)} 行 / "
        f"{result['订单日期'].nunique()} 个桶"
    )
