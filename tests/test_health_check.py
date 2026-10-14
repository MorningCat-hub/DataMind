"""
数据体检（agent/utils/health_check.py）

它存在的理由：

    通用 Agent 会给你一个**自信的错误结论** ——
    例如「12 月环比下降 70%」，而 12 月只覆盖了 9 天。

    所以先确定性体检，再用体检结论去拦截报告里的错误结论。

这些用例用**自造的陷阱数据**做主验证（快、确定），
如果仓库里有真实采样再补一组真实数据断言。
"""

import os

import pytest

from agent.utils.health_check import (
    SEVERITY_BLOCK,
    SEVERITY_INFO,
    SEVERITY_WARN,
    format_health_block,
    format_health_notice,
    run_health_checks,
    summarize_findings,
)


# ========================================
# 自造的「全是坑」数据集
# ========================================

HEADER = "InvoiceNo,InvoiceDate,StockCode,Quantity,UnitPrice,CustomerID,Country"


def _rows():

    rows = []

    # 9 个完整月份的 UK 行
    for month in range(1, 10):

        for day in (1, 10, 20, 28):

            rows.append(
                "INV{}{:02d},2011-{:02d}-{:02d},SKU1,5,10.0,C{:03d},United Kingdom"
                .format(month, day, month, day, month)
            )

    # 退货：负数 + C 开头发票
    for index in range(8):

        rows.append(
            "CINV{0},2011-09-15,SKU1,-3,10.0,C{1:03d},United Kingdom"
            .format(index, index)
        )

    # 单价为 0（赠品）
    for index in range(6):

        rows.append(
            "INV900{0},2011-09-16,SKU2,2,0.0,C{1:03d},United Kingdom"
            .format(index, index)
        )

    # 缺失客户 ID
    for index in range(10):

        rows.append(
            "INV800{0},2011-09-17,SKU3,1,7.5,,France".format(index)
        )

    # 最后一个自然月只有 3 天（期间不完整）
    for day in (1, 2, 3):

        rows.append(
            "INV700{0},2011-10-{1:02d},SKU4,4,9.0,C001,United Kingdom".format(day, day)
        )

    return rows


@pytest.fixture
def trap_csv(write_csv):

    return write_csv(
        HEADER + "\n" + "\n".join(_rows()) + "\n",
        "traps.csv",
    )


def _by_kind(result, kind):

    return [
        item for item in result["findings"]
        if item["kind"] == kind
    ]


# ========================================
# 期间不完整
# ========================================

def test_partial_last_period_is_a_blocking_finding(trap_csv):

    result = run_health_checks(trap_csv)

    hits = _by_kind(result, "partial_period")

    assert len(hits) == 1, result["findings"]
    assert hits[0]["severity"] == SEVERITY_BLOCK
    assert hits[0]["period_label"] == "2011-10"
    assert hits[0]["evidence"]["span_days"] == 3
    assert hits[0]["evidence"]["typical_span_days"] > 3


def test_regression_string_dates_are_recognised(write_csv):
    """
    回归（在真实数据上实测踩到）：

    项目里的数据一律会被**归一化成 CSV**，
    所以日期列是字符串（"2011-01-05"）而不是 datetime 类型。
    如果只认 datetime 类型，「期间不完整」这个最重要的检查
    永远不会触发 —— 而那正是最该发现的东西。
    """

    rows = ["date,amount"]

    for month in range(1, 7):

        for day in (1, 10, 20, 28):

            rows.append("2011-{0:02d}-{1:02d},100".format(month, day))

    # 最后一个月只有 3 天
    for day in (1, 2, 3):

        rows.append("2011-07-{0:02d},50".format(day))

    path = write_csv("\n".join(rows) + "\n", "string_dates.csv")

    hits = _by_kind(run_health_checks(path), "partial_period")

    assert hits, "字符串日期也必须被识别成日期列"
    assert hits[0]["period_label"] == "2011-07"
    assert hits[0]["evidence"]["span_days"] == 3


def test_regression_the_whole_file_is_scanned(write_csv):
    """
    回归：数据按时间排序时，不完整的那个月**只在文件尾部**。
    如果体检只看前 N 行，最重要的发现就会被截掉。
    """

    rows = ["date,amount"]

    for month in range(1, 7):

        for day in (1, 10, 20, 28):

            rows.append("2011-{0:02d}-{1:02d},100".format(month, day))

    # 大量填充行，确保「最后一个不完整的月」位于很靠后的位置
    for index in range(5000):

        rows.append("2011-06-15,1")

    for day in (1, 2):

        rows.append("2011-07-{0:02d},50".format(day))

    path = write_csv("\n".join(rows) + "\n", "tail_trap.csv")

    hits = _by_kind(run_health_checks(path), "partial_period")

    assert hits, "尾部的期间不完整必须被发现"
    assert hits[0]["period_label"] == "2011-07"


def test_partial_period_finding_names_the_period_for_later_checks(trap_csv):
    """结论核对器要靠这个标签去报告里找有没有踩坑。"""

    hit = _by_kind(run_health_checks(trap_csv), "partial_period")[0]

    assert "2011-10" in hit["detail"]
    assert hit["period_column"] == "InvoiceDate"


# ========================================
# 负值 / 退货
# ========================================

def test_negative_quantity_is_a_blocking_finding(trap_csv):

    hits = _by_kind(run_health_checks(trap_csv), "negative_values")

    assert hits, "数量为负必须被发现"
    assert hits[0]["severity"] == SEVERITY_BLOCK
    assert hits[0]["evidence"]["column"] == "Quantity"
    assert hits[0]["evidence"]["negative_rows"] == 8


def test_negative_finding_points_at_the_cancel_invoices(trap_csv):
    """C 开头的发票号是取消/退货的标志，要指出来。"""

    hit = _by_kind(run_health_checks(trap_csv), "negative_values")[0]

    assert any("C 开头" in hint for hint in hit["evidence"]["cancel_hints"]), hit


def test_the_finding_explains_the_two_possible_口径(trap_csv):

    hit = _by_kind(run_health_checks(trap_csv), "negative_values")[0]

    assert "毛额" in hit["detail"] and "净额" in hit["detail"]


# ========================================
# 缺失关键维度
# ========================================

def test_missing_customer_id_is_reported_with_coverage(trap_csv):

    hits = _by_kind(run_health_checks(trap_csv), "missing_dimension")

    assert hits, "客户 ID 缺失必须被发现"
    assert hits[0]["severity"] == SEVERITY_WARN
    assert hits[0]["evidence"]["column"] == "CustomerID"
    assert "覆盖率" in hits[0]["detail"]


# ========================================
# 单价 <= 0
# ========================================

def test_non_positive_price_is_reported(trap_csv):

    hits = _by_kind(run_health_checks(trap_csv), "non_positive_price")

    assert hits
    assert hits[0]["evidence"]["zero_rows"] == 6


# ========================================
# 维度集中度
# ========================================

def test_dimension_concentration_is_reported(trap_csv):

    hits = _by_kind(run_health_checks(trap_csv), "concentration")

    assert hits
    hit = hits[0]
    assert hit["evidence"]["column"] == "Country"
    assert hit["evidence"]["top_value"] == "United Kingdom"
    assert hit["evidence"]["top_share"] > 0.8


# ========================================
# 干净数据不该报警
# ========================================

def test_clean_data_has_no_blocking_findings(write_csv):

    rows = []

    for month in range(1, 7):

        for day in (1, 8, 15, 22, 28):

            rows.append(
                "INV{0}{1},2011-{0:02d}-{1:02d},SKU,5,10.0,C{1:03d},A"
                .format(month, day)
            )

    path = write_csv(
        "id,day,sku,qty,price,customer,region\n"
        + "\n".join(
            "{},{},{}".format(index, row.split(",")[1], "5,10.0,C1,A")
            for index, row in enumerate(rows)
        )
        + "\n",
        "clean.csv",
    )

    result = run_health_checks(path)

    assert result["ok"] is True
    assert [
        item for item in result["findings"]
        if item["severity"] == SEVERITY_BLOCK
    ] == [], result["findings"]


def test_a_missing_file_degrades_gracefully(tmp_path):

    result = run_health_checks(str(tmp_path / "nope.csv"))

    assert result["ok"] is False
    assert result["findings"] == []
    assert result["error"]


def test_a_broken_file_degrades_gracefully(write_csv):

    path = write_csv("\x00\x01 not a csv \x02", "broken.csv")

    result = run_health_checks(path)

    assert isinstance(result["findings"], list)


# ========================================
# 排序与渲染
# ========================================

def test_findings_are_sorted_by_severity(trap_csv):

    severities = [
        item["severity"] for item in run_health_checks(trap_csv)["findings"]
    ]

    assert severities[0] == SEVERITY_BLOCK
    assert severities == sorted(
        severities,
        key=lambda value: {SEVERITY_BLOCK: 0, SEVERITY_WARN: 1,
                           SEVERITY_INFO: 2}[value],
    )


def test_the_prompt_block_marks_each_severity(trap_csv):

    findings = run_health_checks(trap_csv)["findings"]

    block = format_health_block(findings)

    assert "必须规避" in block
    assert "需要声明" in block
    assert "不要凭空猜测数据限制" in block


def test_the_prompt_block_is_empty_without_findings():

    assert format_health_block([]) == ""


def test_the_user_notice_only_carries_blocking_items(trap_csv):

    findings = run_health_checks(trap_csv)["findings"]

    notice = format_health_notice(findings)

    assert "数据体检提示" in notice
    assert "重点防护" not in notice
    assert notice.count("> - ") >= 1


def test_the_user_notice_is_empty_when_nothing_blocks(write_csv):

    path = write_csv("a,b\n1,2\n3,4\n", "tiny.csv")

    assert format_health_notice(
        run_health_checks(path)["findings"]
    ) == ""


def test_summarize_keeps_the_fields_the_api_needs(trap_csv):

    summarized = summarize_findings(run_health_checks(trap_csv)["findings"])

    assert summarized
    assert set(summarized[0]) >= {"id", "kind", "severity", "title", "detail"}


# ========================================
# 节点
# ========================================

def test_the_node_stores_findings_and_traces(trap_csv, make_state, trace_events):

    from agent.nodes.health_check import check_data_health

    out = check_data_health(make_state(file_path=trap_csv))

    assert out["health_findings"]
    assert "必须规避" in out["health_context"]
    assert "health_checked" in trace_events(out)

    event = out["agent_trace"][0]

    assert event["display"]["level"] == "warning", "有阻断项就该标黄"
    assert event["display"]["visible"] is True


def test_the_node_skips_non_table_input(make_state):

    from agent.nodes.health_check import check_data_health

    out = check_data_health(make_state(file_path="x", input_kind="text"))

    assert out["health_findings"] == []
    assert out["health_context"] is None


def test_the_node_survives_an_unreadable_file(make_state, trace_events):

    from agent.nodes.health_check import check_data_health

    out = check_data_health(make_state(file_path="/nope/nothing.csv"))

    assert out["health_findings"] == []
    assert "health_check_failed" in trace_events(out)


# ========================================
# 真实数据（仓库里有采样才跑）
# ========================================

REAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "online_retail.csv",
)


@pytest.mark.skipif(
    not os.path.isfile(REAL),
    reason="需要 data/online_retail.csv（跑 scripts/fetch_demo_data.py 生成）",
)
def test_real_retail_data_has_the_known_traps():

    result = run_health_checks(REAL)

    kinds = {item["kind"] for item in result["findings"]}

    assert "partial_period" in kinds, "2011-12 只有 9 天数据"
    assert "negative_values" in kinds, "Quantity 有负值（退货）"
    assert "missing_dimension" in kinds, "CustomerID 缺失约 24.9%"
    assert "concentration" in kinds, "UK 占比约 91%"

    partial = _by_kind(result, "partial_period")[0]

    assert partial["period_label"] == "2011-12"
    assert partial["evidence"]["span_days"] <= 12
