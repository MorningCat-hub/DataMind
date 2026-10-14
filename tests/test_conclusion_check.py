"""
结论级核对（agent/utils/conclusion_check.py）

它回答的问题：

    数值核对管「报告里的数字对不对」，
    这一层管「报告的结论，这份数据撑不撑得住」。

真实数据上的例子：

    体检发现 2011-12 只覆盖 9 天（完整月约 30 天），
    报告却写「12 月销售额环比下降 70%」——
    数字没问题，比较不成立。

设计原则是**保守**：只有当一句话做了比较、
且没有自己声明这个限制时才算违规。
否则它会去指责报告里那句本来正确的声明。
"""

import pytest

from agent.utils.conclusion_check import (
    build_conclusion_notice,
    check_conclusions,
    period_variants,
    split_sentences,
    summarize_conclusions,
)


# ========================================
# 夹具：真实数据上会得到的那两条 block
# ========================================

PARTIAL = {
    "id": "partial_last_period",
    "kind": "partial_period",
    "severity": "block",
    "title": "最后一个期间不完整（2011-12 只覆盖 9 天）",
    "detail": "...",
    "period_label": "2011-12",
    "evidence": {"span_days": 9, "typical_span_days": 30},
}

NEGATIVE = {
    "id": "negative_values:Quantity",
    "kind": "negative_values",
    "severity": "block",
    "title": "「Quantity」存在负值（10,624 行）",
    "detail": "...",
    "metric_column": "Quantity",
    "evidence": {"column": "Quantity", "negative_rows": 10624},
}

MISSING = {
    "id": "missing_dimension:CustomerID",
    "kind": "missing_dimension",
    "severity": "warn",
    "title": "「CustomerID」缺失 24.9%",
    "detail": "...",
    "evidence": {"column": "CustomerID", "share": 0.249},
}


# ========================================
# 分句与期间写法
# ========================================

def test_sentences_split_on_chinese_punctuation():

    assert split_sentences("第一句。第二句！第三句？") == [
        "第一句", "第二句", "第三句",
    ]


def test_markdown_table_rows_stay_one_sentence():

    rows = split_sentences("| 月份 | 销售额 |\n|---|---|\n| 2011-12 | 100 |")

    assert "| 月份 | 销售额 |" in rows
    assert "| 2011-12 | 100 |" in rows


def test_period_variants_cover_common_renderings():

    variants = period_variants("2011-12")

    assert "2011-12" in variants
    assert "2011年12月" in variants
    assert "12月" in variants


# ========================================
# 期间不完整
# ========================================

@pytest.mark.parametrize(
    "report",
    [
        "2011-12 销售额环比下降 70%。",
        "12 月销售额较上月下降 70%。",
        "2011年12月环比减少了 25,000。",
        "| 月份 | 环比 |\n|---|---|\n| 2011-12 | -70% |",
    ],
)
def test_a_comparison_on_an_incomplete_period_is_blocked(report):

    result = check_conclusions(report, [PARTIAL])

    assert result["passed"] is False, report
    assert result["violation_count"] >= 1
    assert result["violations"][0]["kind"] == "partial_period"


def test_the_violation_explains_why(report="2011-12 销售额环比下降 70%。"):

    violation = check_conclusions(report, [PARTIAL])["violations"][0]

    assert "只覆盖 9 天" in violation["reason"]
    assert "不成立" in violation["reason"]
    assert "日均" in violation["suggestion"]


@pytest.mark.parametrize(
    "report",
    [
        "2011-12 环比下降 70%，但该月数据不完整，不可比。",
        "2011-12 只覆盖 9 天，因此不宜与完整月份比较。",
        "说明：12 月数据仅覆盖 9 天，相关环比已按日均口径计算。",
    ],
)
def test_a_correct_disclosure_is_not_flagged(report):
    """报告自己声明了这个限制，那正是我们要求它做的事。"""

    result = check_conclusions(report, [PARTIAL])

    assert result["passed"] is True, report


@pytest.mark.parametrize(
    "report",
    [
        "2011-11 销售额环比增长 12%。",
        "2011-12 销售额为 25,525。",
        "各月销售额见下表。",
    ],
)
def test_unrelated_sentences_are_not_flagged(report):
    """别的月份、或者只是陈述数值，都不该被拦。"""

    assert check_conclusions(report, [PARTIAL])["passed"] is True, report


# ========================================
# 口径未声明
# ========================================

def test_a_total_without_a_declared_caliber_is_blocked():

    result = check_conclusions(
        "本期销售额合计为 9,747,748。", [NEGATIVE]
    )

    assert result["passed"] is False
    assert result["violations"][0]["kind"] == "undeclared_caliber"
    assert "毛额" in result["violations"][0]["reason"]


@pytest.mark.parametrize(
    "report",
    [
        "本期净销售额合计为 9,700,000（已扣除退货）。",
        "销售额合计 9,747,748，其中已剔除退货部分。",
        "合计金额按扣除退货后的净额口径统计。",
    ],
)
def test_a_declared_caliber_passes(report):

    assert check_conclusions(report, [NEGATIVE])["passed"] is True, report


def test_no_total_no_caliber_complaint():
    """没有给总额，就不用声明口径。"""

    assert check_conclusions(
        "各商品的销量分布见下表。", [NEGATIVE]
    )["passed"] is True


# ========================================
# 覆盖率
# ========================================

def test_customer_analysis_without_coverage_is_flagged():

    result = check_conclusions("复购率为 32%。", [MISSING])

    assert result["passed"] is False
    assert result["violations"][0]["kind"] == "undeclared_coverage"
    assert result["violations"][0]["severity"] == "warn"


def test_customer_analysis_with_coverage_passes():

    assert check_conclusions(
        "复购率为 32%，仅覆盖有客户 ID 的 75.1% 数据。", [MISSING]
    )["passed"] is True


def test_a_report_without_customer_analysis_is_not_checked():

    assert check_conclusions("销售额合计 100。", [MISSING])["passed"] is True


# ========================================
# 边界与汇总
# ========================================

def test_no_findings_means_nothing_is_checked():

    result = check_conclusions("2011-12 环比下降 70%。", [])

    assert result["checked"] == 0
    assert result["passed"] is True


def test_warn_only_findings_are_still_checked():
    """warn 级的发现项也要参与核对，只是不阻断。"""

    result = check_conclusions("复购率为 32%。", [MISSING])

    assert result["checked"] == 1


def test_multiple_conflicts_are_all_reported():

    result = check_conclusions(
        "销售额合计 9,747,748。复购率为 32%。", [NEGATIVE, MISSING]
    )

    assert result["violation_count"] == 2, result["violations"]


def test_duplicate_claims_are_deduplicated():

    result = check_conclusions(
        "销售额合计 100。销售额合计 100。", [NEGATIVE]
    )

    assert result["violation_count"] == 1


def test_the_notice_names_the_claim_and_the_fix():

    notice = build_conclusion_notice(
        check_conclusions("2011-12 环比下降 70%。", [PARTIAL])["violations"]
    )

    assert "结论核对提示" in notice
    assert "2011-12" in notice
    assert "建议" in notice
    assert "由程序生成" in notice


def test_the_notice_is_empty_when_clean():

    assert build_conclusion_notice([]) == ""


def test_summarize_has_the_fields_the_api_needs():

    summarized = summarize_conclusions(
        check_conclusions("2011-12 环比下降 70%。", [PARTIAL])
    )

    assert summarized["passed"] is False
    assert summarized["violation_count"] >= 1
    assert "partial_period" in summarized["kinds"]
    assert set(summarized["violations"][0]) >= {
        "kind", "severity", "claim", "reason",
    }


# ========================================
# 节点
# ========================================

def test_the_node_appends_a_program_generated_notice(
    make_state, trace_events
):

    from agent.nodes.conclusion_check import verify_conclusions

    state = make_state(
        final_report="报告正文\n\n2011-12 销售额环比下降 70%。",
        health_findings=[PARTIAL],
    )

    out = verify_conclusions(state)

    assert out["conclusion_check"]["passed"] is False
    assert "结论核对提示" in out["final_report"]
    assert "conclusion_violation" in trace_events(out)
    assert out["error"], "踩坑之后节点要被标成需要关注"

    event = [
        item for item in out["agent_trace"]
        if item["event"] == "conclusion_violation"
    ][0]

    assert event["display"]["level"] == "warning"


def test_the_node_passes_a_clean_report(make_state, trace_events):

    from agent.nodes.conclusion_check import verify_conclusions

    out = verify_conclusions(make_state(
        final_report="2011-12 只覆盖 9 天，不宜与完整月份比较。",
        health_findings=[PARTIAL],
    ))

    assert out["conclusion_check"]["passed"] is True
    assert "conclusion_checked" in trace_events(out)
    assert "final_report" not in out, "通过时不该改动报告"


def test_the_node_skips_without_a_report(make_state):

    from agent.nodes.conclusion_check import verify_conclusions

    out = verify_conclusions(make_state(final_report=None))

    assert out["conclusion_check"] is None


def test_the_node_handles_no_findings(make_state, trace_events):

    from agent.nodes.conclusion_check import verify_conclusions

    out = verify_conclusions(make_state(
        final_report="报告", health_findings=[]
    ))

    assert out["conclusion_check"]["passed"] is True
