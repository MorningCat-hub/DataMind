"""
报告数值核对（agent/utils/verifier.py）

这个模块历史上出过两次误判，两个回归用例都保留在下面：

1. 四位数年份启发式用「1900-2100 的整数」判断，
   结果把销售额 1935、6495 当成「年份」从数据里丢掉，
   而报告里带千分位的 1,935 又不会被当成 年份 ——
   于是完全正确的数字被标成「无法追溯」。

2. 数值池被重复值撑到超过上限，子集求和直接放弃，
   正确的边际合计（月度 135,192、季度 39,990）被误报。
"""

import pytest

from agent.utils.verifier import (
    MAX_SUBSET_VALUES,
    _all_subset_sums,
    _is_subset_sum,
    build_verification_notice,
    collect_source_values,
    extract_numbers,
    verify_report_numbers,
)


# ========================================
# 数值抽取
# ========================================

def test_extracts_plain_numbers():

    values = [claim["value"] for claim in extract_numbers("总和 175182，增长 12.5%")]

    assert 175182.0 in values
    assert 12.5 in values


def test_expands_chinese_units():

    values = [claim["value"] for claim in extract_numbers("总额约 17.5 万，占比 3 亿")]

    assert 175000.0 in values
    assert 300000000.0 in values


def test_ignores_an_explicit_year():

    claims = extract_numbers("数据覆盖 2026 年 Q1 至 Q3")

    assert [claim["reason"] for claim in claims] == ["year"]


def test_ignores_list_markers():

    claims = extract_numbers("1. 核心结论\n2. 数据分析")

    assert all(claim["reason"] == "list_marker" for claim in claims)


def test_ignores_ordinals_but_keeps_real_counts():

    reasons = [
        (claim["raw"], claim["reason"])
        for claim in extract_numbers("第 3 步执行失败，共 2 张图表")
    ]

    assert ("3", "ordinal") in reasons
    assert ("2", None) in reasons


@pytest.mark.parametrize(
    "text",
    [
        "2026-01 到 2026-09 的销售额",
        "2026Q1 与 2026-W03",
    ],
)
def test_masks_dates_and_period_labels(text):

    assert extract_numbers(text) == []


def test_keeps_context_so_the_user_can_locate_it():

    claims = extract_numbers("华东地区销售额为 12000 元")

    assert "华东地区销售额" in claims[0]["context"]


@pytest.mark.parametrize("raw", ["1935", "6495", "2026"])
def test_regression_four_digit_data_is_not_treated_as_a_year(raw):
    """回归：四位数数据不能被当成「年份」丢掉。"""

    claims = extract_numbers(f"该值为 {raw} 元")

    assert [claim["reason"] for claim in claims] == [None]


# ========================================
# 合法数值与派生
# ========================================

SERIES = "地区\n华东    12000\n华南    15000\n华北    9000"


def test_collects_numbers_from_a_result_string():

    values, groups = collect_source_values(
        [{"status": "success", "data": {"result": SERIES, "output": ""}}], []
    )

    assert len(values) >= 3
    assert len(groups) == 1


def test_chart_row_count_counts_as_a_source_value():

    values, _groups = collect_source_values(
        [{"status": "success", "data": {"result": SERIES}}],
        [{"success": True, "data_summary": {"row_count": 40, "categories": []}}],
    )

    assert 40.0 in values


@pytest.mark.parametrize(
    "target, label",
    [
        (36000.0, "组内求和"),
        (12000.0, "组内均值"),
        (3000.0, "两两之差"),
    ],
)
def test_derived_values_are_legitimate(target, label):
    """报告里的合理推算不该被标成「无法追溯」。"""

    result = verify_report_numbers(
        f"合计相关数值 {target:g}。",
        [{"status": "success", "data": {"result": SERIES, "output": ""}}],
        [],
    )

    assert result["passed"] is True, (label, result["unmatched"])


def test_group_share_is_legitimate():

    result = verify_report_numbers(
        "华东占比约 33.3%。",
        [{"status": "success", "data": {"result": SERIES, "output": ""}}],
        [],
    )

    assert result["passed"] is True, result["unmatched"]


# ========================================
# 核对判定
# ========================================

GOOD_REPORT = (
    "## 结论\n"
    "三个地区合计 36000 元，其中华东 12000 元，占比 33.3%，比华北多 3000 元。\n"
)

SOURCE = [{"status": "success", "data": {"result": SERIES, "output": ""}}]


def test_a_report_built_from_the_results_passes():

    result = verify_report_numbers(GOOD_REPORT, SOURCE, [])

    assert result["passed"] is True, result["unmatched"]
    assert result["checked"] >= 4
    assert result["source_count"] >= 3


def test_a_fabricated_number_is_caught():

    result = verify_report_numbers(
        GOOD_REPORT + "此外利润达到 987654 元。", SOURCE, []
    )

    assert result["passed"] is False
    assert result["unmatched_count"] == 1
    assert result["unmatched"][0]["value"] == 987654.0
    assert "利润" in result["unmatched"][0]["context"]


def test_rounding_and_units_are_tolerated():

    result = verify_report_numbers("合计约 3.6 万。", SOURCE, [])

    assert result["passed"] is True, result["unmatched"]


def test_a_report_without_numbers_passes_with_zero_checked():

    result = verify_report_numbers("本次没有数值。", SOURCE, [])

    assert result["passed"] is True
    assert result["checked"] == 0


def test_without_any_result_everything_is_untraceable():

    result = verify_report_numbers("总利润 12345 元。", [], [])

    assert result["passed"] is False
    assert result["unmatched_count"] == 1


# ========================================
# 追加提示
# ========================================

def test_no_notice_when_everything_checks_out():

    assert build_verification_notice({"passed": True}) == ""


def test_the_notice_lists_the_value_context_and_is_not_a_verdict():

    notice = build_verification_notice(
        verify_report_numbers(GOOD_REPORT + "利润 987654 元。", SOURCE, [])
    )

    assert "987654" in notice
    assert "利润" in notice
    assert "不代表结论一定错误" in notice


# ========================================
# 子集求和
# ========================================

def test_subset_sums_include_empty_and_total():

    assert sorted(_all_subset_sums([1.0, 2.0])) == [0.0, 1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    "target, expected",
    [
        (500.0, True),
        (450.0, False),
        (987654.0, False),
    ],
)
def test_subset_sum_matching(target, expected):

    assert _is_subset_sum([100.0, 200.0, 300.0], target) is expected


def test_a_small_arithmetic_error_on_a_big_total_is_caught():
    """子集求和在核对精确算术，容差不能宽到放过算错。"""

    assert _is_subset_sum([175182.0 * 0.4, 175182.0 * 0.6], 175195.0) is False


def test_too_many_values_bails_out_instead_of_hanging():

    assert (
        _is_subset_sum([1.0] * (MAX_SUBSET_VALUES + 1), 5.0) is False
    )


REGION_SOURCE = [{
    "status": "success",
    "data": {"result": (
        "地区  销售额\n华东  57368\n东北  47115\n华中  30218\n"
        "西南  19081\n华南  14031\n华北  7369"
    ), "output": ""},
}]


def test_a_ratio_written_as_bei_is_traceable():
    """「是第二名的 1.22 倍」——不带百分号的比值。"""

    result = verify_report_numbers("华东约为东北的 1.22 倍。", REGION_SOURCE, [])

    assert result["passed"] is True, result["unmatched"]


def test_a_percentage_over_a_three_term_subtotal_is_traceable():
    """76.90% = 134701 / 175182，分子是三个地区之和。"""

    result = verify_report_numbers(
        "华东、东北、华中合计 134701，占全部的约 76.90%。", REGION_SOURCE, []
    )

    assert result["passed"] is True, result["unmatched"]


def test_a_percentage_over_a_pairwise_sum_is_traceable():
    """9.9% = (600 + 650) / 12600。"""

    source = [{
        "status": "success",
        "data": {"result": (
            "销售额合计 12600\n退款合计 600\n折扣合计 650\n净销售额 11350"
        ), "output": ""},
    }]

    result = verify_report_numbers(
        "退款与折扣合计占销售额的约 9.9%。", source, []
    )

    assert result["passed"] is True, result["unmatched"]


def test_a_fabricated_ratio_is_still_caught():

    result = verify_report_numbers("华东约为东北的 5.5 倍。", REGION_SOURCE, [])

    assert result["passed"] is False, result["unmatched"]


def test_a_fabricated_percentage_is_still_caught():

    result = verify_report_numbers(
        "华东、东北、华中合计 134701，占全部的约 91.30%。", REGION_SOURCE, []
    )

    assert result["passed"] is False, result["unmatched"]


def test_regression_marginal_totals_survive_a_pool_full_of_duplicates():
    """
    回归：数值池被 describe_dataset 的输出（行数、唯一值数、样例值）
    撑到超过上限时，子集求和会放弃，正确的边际合计反被误报。

    去重之后同一张表打印两次也不会把池子撑爆。
    """

    noisy = [{
        "status": "success",
        "data": {"result": " ".join(
            ["rows 40 cols 9", "0 1 0 38 0 10 0 4 0 6 0 8 0 16",
             "699 0 36 2796"] * 3
            + ["34879", "23276", "7418", "27379", "23817", "18423",
               "9485", "29344", "1161"] * 2
            + ["175182 40 9 9 175182 40 4379.55 40"]
        )},
    }]

    result = verify_report_numbers(
        "上半年合计 135192，第三季度合计 39990。", noisy, []
    )

    assert result["passed"] is True, result["unmatched"]
