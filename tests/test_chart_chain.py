"""
DataMind 绘图链路测试（pytest 版）

由三个独立验证脚本里「绘图链路」相关的检查点合并而来：

- ``v37_verify.py``
    §2  Chart Planner 校验后仍然保留 time_granularity
    §3  标题里有日期关键词、但 x_field 不是日期（回归：整张图曾经直接失败）
    §4  显式 / 关键词 / 自动选出的粒度，真的体现在画出来的图上
    §5  隐式聚合与「粒度不合法就降级」
    §6  时间轴按真实时间排序
    §7  普通类别图不受影响
- ``v38_verify.py``
    图表失败的可观测性：失败不能被吞掉，
    既要有 ``chart_error`` 事件，也要由程序写进最终报告
- ``v39_verify.py``
    图链路端到端：单图 / 多图 / 部分失败 / 全部失败 / chart_results 形状

边界说明：

    纯时间粒度逻辑（``normalize`` / ``detect_from_text`` / ``select_by_span``）
    由 ``tests/test_time_granularity.py`` 负责，本文件不重复。

本文件遵守的约定：

1. 纯 pytest —— ``def test_xxx()`` + ``assert``，
   没有 ``sys.exit``，也没有自制的 check() 收集器；
2. 完全离线 —— 所有模型调用都由 ``stub_llm`` / ``tool_model`` 夹具伪造；
3. 不往项目目录写任何东西 —— PNG 落进 ``tmp_path``，
   数据集归一化出来的 CSV 也落进 ``tmp_path``；
4. 不依赖当前工作目录 —— 路径全部基于项目根目录拼出来。
"""

import json
import os
import sys

import pytest

# =========================================================
# 项目根目录
# =========================================================

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # tests/ 的上一级

if not os.path.isdir(os.path.join(PROJECT, "agent")):
    PROJECT = os.path.dirname(PROJECT)   # 兜底

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from agent.nodes.reporter import build_failure_notice  # noqa: E402
from agent.utils.chart_executor import (  # noqa: E402
    aggregate_data,
    execute_chart,
    resolve_time_granularity,
)
from agent.utils.chart_planner import (  # noqa: E402
    validate_chart_item,
    validate_chart_plan,
)


# =========================================================
# 常量：桩数据与桩回复
# =========================================================

# 与 sales 数据集匹配的粒度断言（写在断言信息里，改数据时容易定位）
MONTH_BUCKETS = 9
QUARTER_BUCKETS = 3

GOOD_CODE = "result = df['销售额'].sum()"
BAD_CODE = "result = df['不存在的字段'].sum()"

REPORT_TEXT = "# 分析报告\n\n核心结论：销售额汇总已完成。"

PLAN_JSON = json.dumps(
    {"plan": [{"step": 1, "task": "计算销售额总和", "tool": "run_stat_calc"}]},
    ensure_ascii=False,
)

REVIEWER_JSON = json.dumps(
    {"passed": True, "reason": "ok", "missing_tasks": []},
    ensure_ascii=False,
)

TREND_REASON = "看时间趋势"

STANDARD_BAR = {
    "chart_type": "bar",
    "title": "各地区销售额对比",
    "reason": "比较不同地区",
    "x_field": "地区",
    "y_field": "销售额",
    "aggregation": "sum",
    "priority": 1,
}

HISTOGRAM = {
    "chart_type": "histogram",
    "title": "销售额分布",
    "reason": "看分布",
    "x_field": "销售额",
    "y_field": None,
    "aggregation": "none",
    "priority": 1,
}

GOOD_CHARTS = [
    {
        "chart_type": "bar",
        "title": "各地区销售额对比",
        "reason": "对比",
        "x_field": "地区",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    },
    {
        "chart_type": "pie",
        "title": "各品类销售额占比",
        "reason": "占比",
        "x_field": "品类",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 2,
    },
]


def broken_chart(priority=3, title="坏掉的图表"):
    """引用不存在字段的图表计划：execute_chart 一定会失败。"""

    return {
        "chart_type": "bar",
        "title": title,
        "reason": "字段不存在",
        "x_field": "不存在的字段",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": priority,
    }


def sales_plan(title, time_granularity=None, aggregation="sum"):
    """
    sales 数据集上的「订单日期 × 销售额」折线图计划。

    time_granularity 为 None 时不写这个字段 ——
    这样才能区分「调用方没传粒度」和「显式给了粒度」。
    """

    plan = {
        "chart_type": "line",
        "title": title,
        "reason": TREND_REASON,
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": aggregation,
        "priority": 1,
    }

    if time_granularity is not None:
        plan["time_granularity"] = time_granularity

    return plan


# =========================================================
# 小工具
# =========================================================

def png_files(root):
    """列出目录下（递归）所有 PNG 文件名。"""

    found = []

    for _dirpath, _dirnames, filenames in os.walk(str(root)):

        found.extend(
            name for name in filenames if name.endswith(".png")
        )

    return sorted(found)


def node_completed_event(state, node):
    """取某个节点的 node_completed 事件。"""

    for event in state.get("agent_trace") or []:

        if (
            event.get("node") == node
            and event.get("event") == "node_completed"
        ):

            return event

    return None


def chart_error_events(state):
    """取全部 chart_error 事件。"""

    return [
        event
        for event in (state.get("agent_trace") or [])
        if event.get("event") == "chart_error"
    ]


# =========================================================
# 夹具：隔离 + 整图运行
# =========================================================

@pytest.fixture(autouse=True)
def chart_output_in_tmp(monkeypatch, tmp_path):
    """
    所有 PNG 都必须落进 pytest 的 tmp_path。

    chart_executor 只认模块级常量 CHART_OUTPUT_DIR，
    改写它就能保证测试不会往项目的 temp_charts/ 里写图。

    目录名和 conftest 的 isolated_temp 保持一致（tmp_path/temp_charts），
    这样两个夹具谁先谁后都指向同一个目录。
    """

    charts = tmp_path / "temp_charts"

    charts.mkdir(exist_ok=True)

    monkeypatch.setattr(
        "agent.utils.chart_executor.CHART_OUTPUT_DIR",
        str(charts),
    )

    return str(charts)


@pytest.fixture(autouse=True)
def normalized_dataset_in_tmp(monkeypatch, tmp_path):
    """
    dataset 节点会把上传的表格归一化成 temp/data_*.csv。

    这里把归一化输出也指到 tmp_path，
    整图测试同样不会在项目 temp/ 里留文件。
    """

    import tools.loader as loader
    import agent.nodes.dataset as dataset_node

    output_dir = tmp_path / "normalized"

    output_dir.mkdir(exist_ok=True)

    real_ingest = loader.ingest

    def _ingest(path):

        return real_ingest(path, output_dir=str(output_dir))

    monkeypatch.setattr(dataset_node, "ingest", _ingest)

    return str(output_dir)


@pytest.fixture
def sales_df(sales_csv):
    """sales 数据集的 DataFrame（只读）。"""

    import pandas as pd

    return pd.read_csv(sales_csv)


@pytest.fixture
def run_chart_graph(stub_llm, tool_model, fake_tool_call, make_state, chart_csv):
    """
    跑一次完整的 Agent 图（离线）。

    用法：

        final = run_chart_graph([GOOD_CHARTS[0]])
        final = run_chart_graph([broken_chart()], executor_rounds=3, code=BAD_CODE)

    参数：

        charts            Chart Planner 桩返回的图表计划列表
        executor_rounds   Executor 会被进入几次（1 = 一次成功；
                          3 = 首次失败 + 两次修复都没救回来）
        code              run_python 里的代码（BAD_CODE 会让任务永久失败）
        report            报告节点桩返回的文本
    """

    from agent.graph import build_graph

    graph = build_graph()

    def _run(
        charts,
        executor_rounds=1,
        code=GOOD_CODE,
        report=REPORT_TEXT,
    ):

        stub_llm.reply("中的 Planner", PLAN_JSON)
        stub_llm.reply("中的 Reviewer", REVIEWER_JSON)
        stub_llm.reply("代码修复专家", code)
        stub_llm.reply("专业的数据分析师", report)
        stub_llm.reply(
            "Chart Planner",
            json.dumps(
                {
                    "need_chart": bool(charts),
                    "charts": list(charts),
                    "reason": "按需出图",
                },
                ensure_ascii=False,
            ),
        )

        for index in range(executor_rounds):

            tool_model.push_tool(
                fake_tool_call(
                    f"call_{index}",
                    "run_python",
                    {"code": code},
                )
            )

            tool_model.push_text("已完成计算。")

        return graph.invoke(
            make_state(question="销售额是多少", file_path=chart_csv)
        )

    return _run


@pytest.fixture
def serving_charts_from_tmp(isolated_temp):
    """
    把 main.app 上已经挂好的 /charts 改指向隔离目录。

    StaticFiles 在 app.mount() 那一刻就把目录固定下来了，
    monkeypatch main.CHART_FOLDER 不会影响它；
    不重定向的话，/charts/xxx.png 会去读项目的 temp_charts/。

    测试结束后还原，避免影响其它测试文件。
    """

    import main

    saved = []

    def _retarget(name, directory):

        for route in main.app.routes:

            if getattr(route, "name", None) != name:
                continue

            static_app = getattr(route, "app", None)

            if static_app is None or not hasattr(
                static_app, "all_directories"
            ):
                continue

            saved.append((
                static_app,
                list(static_app.all_directories),
                static_app.directory,
            ))

            static_app.all_directories = [str(directory)]
            static_app.directory = str(directory)
            static_app.config_checked = False

            return True

        return False

    retargeted = _retarget("charts", isolated_temp["charts"])

    try:

        yield retargeted

    finally:

        for static_app, directories, directory in saved:

            static_app.all_directories = directories
            static_app.directory = directory
            static_app.config_checked = False


# =========================================================
# 1. Chart Planner 保留 time_granularity（v37 §2）
# =========================================================

def test_validate_chart_item_normalizes_time_granularity():
    """Chart Planner 的显式粒度不能被校验逻辑丢掉。"""

    item = validate_chart_item({
        "chart_type": "line",
        "title": "各季度销售额趋势",
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": "sum",
        "time_granularity": "季度",
        "priority": 1,
    })

    assert item.get("time_granularity") == "quarter", item


def test_validate_chart_plan_keeps_time_granularity():
    """整份 plan 走一遍校验之后，粒度仍然在。"""

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

    assert plan.get("charts"), plan
    assert plan["charts"][0].get("time_granularity") == "month", plan


def test_unknown_time_granularity_falls_back_to_none():
    """认不出来的粒度一律当作「不做时间聚合」。"""

    bogus = validate_chart_item({
        "chart_type": "line",
        "x_field": "订单日期",
        "y_field": "销售额",
        "aggregation": "sum",
        "time_granularity": "每个世纪",
    })

    assert bogus.get("time_granularity") is None, bogus


# =========================================================
# 2. 标题有日期关键词但 x_field 不是日期（v37 §3，回归）
# =========================================================

def test_non_date_x_field_never_gets_time_aggregation(sales_df):
    """x 轴不是日期时，标题里的「季度」不能劫持时间聚合。"""

    hijack = {
        "chart_type": "line",
        "title": "季度销售额趋势",
        "reason": "按季度看变化",
        "x_field": "订单号",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    }

    assert resolve_time_granularity(sales_df, hijack) is None, hijack

    # 数值列也不能被当成日期（pd.to_datetime 会把整数当纳秒时间戳）
    numeric = {
        "chart_type": "line",
        "x_field": "数量",
        "y_field": "销售额",
        "aggregation": "sum",
    }

    assert resolve_time_granularity(sales_df, numeric) is None, numeric


def test_quarter_title_with_non_date_x_field_still_renders(sales_csv):
    """v3.7 之前这里整张图会失败，现在必须照常画出来。"""

    result = execute_chart(sales_csv, {
        "chart_type": "line",
        "title": "季度销售额趋势",
        "reason": "按季度看变化",
        "x_field": "订单号",
        "y_field": "销售额",
        "aggregation": "sum",
        "priority": 1,
    })

    assert result["success"] is True, result
    assert result["data_summary"]["time_granularity"] is None, result["data_summary"]


# =========================================================
# 3. 粒度真的体现在图上（v37 §4）
# =========================================================

def test_explicit_granularity_wins_over_title_keyword(sales_csv):
    """显式粒度优先，并且说明文字与真实聚合一致。"""

    explicit = execute_chart(
        sales_csv,
        sales_plan("销售额趋势", "month"),
    )

    assert explicit["success"] is True, explicit

    summary = explicit["data_summary"]

    assert summary["time_granularity"] == "month", summary
    assert summary["row_count"] == MONTH_BUCKETS, summary

    assert "月份" in explicit["description"], explicit["description"]
    assert "sum" in explicit["description"], explicit["description"]

    # 标题写着「季度」，但显式给了 month，仍然按月
    explicit_wins = execute_chart(
        sales_csv,
        sales_plan("季度销售额趋势", "month"),
    )

    assert (
        explicit_wins["data_summary"]["time_granularity"] == "month"
    ), explicit_wins["data_summary"]


def test_quarter_keyword_produces_three_buckets(sales_csv):
    """标题里的「季度」要真的按季度分组。"""

    result = execute_chart(
        sales_csv,
        sales_plan("季度销售额趋势"),
    )

    assert result["success"] is True, result

    summary = result["data_summary"]

    assert summary["time_granularity"] == "quarter", summary
    assert summary["row_count"] == QUARTER_BUCKETS, summary


def test_vague_period_falls_back_to_data_span(sales_csv):
    """「不同周期」这种模糊说法不猜关键词，按真实跨度自动选。"""

    result = execute_chart(
        sales_csv,
        sales_plan("不同周期销售额总和趋势"),
    )

    assert result["success"] is True, result

    summary = result["data_summary"]

    assert summary["time_granularity"] == "month", summary
    assert summary["row_count"] == MONTH_BUCKETS, summary

    assert "月份" in result["description"], result["description"]
    assert isinstance(result.get("warnings"), list), result.get("warnings")


# =========================================================
# 4. 隐式聚合与降级（v37 §5）
# =========================================================

def test_granularity_without_aggregation_defaults_to_sum(sales_csv):
    """aggregation=none + 有时间粒度时，必须真的按桶聚合。"""

    result = execute_chart(
        sales_csv,
        sales_plan("月度销售额", aggregation="none"),
    )

    assert result["success"] is True, result
    assert (
        result["data_summary"]["row_count"] == MONTH_BUCKETS
    ), result["data_summary"]

    warnings = result.get("warnings") or []

    assert any("sum" in item for item in warnings), warnings


def test_bad_granularity_degrades_to_raw_dates(sales_df):
    """粒度不合法时降级为不聚合，并且记录一条 warning。"""

    degraded, warnings = aggregate_data(
        sales_df,
        {
            "chart_type": "bar",
            "x_field": "订单日期",
            "y_field": "销售额",
            "aggregation": "sum",
        },
        "不存在的粒度",
    )

    assert len(degraded) > 0, degraded

    assert all(
        len(str(value)) == 10 for value in degraded["订单日期"]
    ), list(degraded["订单日期"].head(3))

    assert len(degraded) == sales_df["订单日期"].nunique(), (
        len(degraded),
        sales_df["订单日期"].nunique(),
    )

    assert any("降级" in item for item in warnings), warnings


# =========================================================
# 5. 时间轴顺序（v37 §6）
# =========================================================

def test_monthly_labels_are_chronological(sales_df, write_csv):
    """输入被打乱时，折线图的月份标签仍然按时间排序。"""

    shuffled = sales_df.sample(
        frac=1.0, random_state=7
    ).reset_index(drop=True)

    path = write_csv(
        shuffled.to_csv(index=False),
        name="shuffled_sales.csv",
    )

    result = execute_chart(path, sales_plan("月度趋势", "month"))

    assert result["success"] is True, result

    labels = result["data_summary"]["categories"]

    assert labels == sorted(labels), labels


# =========================================================
# 6. 普通类别图不受影响（v37 §7）
# =========================================================

def test_category_axis_chart_is_untouched(chart_csv):
    """文本 x 轴照旧：3 个类别、sum、不做时间聚合。"""

    result = execute_chart(chart_csv, STANDARD_BAR)

    assert result["success"] is True, result

    summary = result["data_summary"]

    assert summary["row_count"] == 3, summary
    assert summary["aggregation"] == "sum", summary
    assert summary["time_granularity"] is None, summary

    for field in (
        "success",
        "chart_path",
        "chart_type",
        "title",
        "description",
        "data_summary",
        "error",
    ):

        assert field in result, (field, result)


def test_histogram_keeps_raw_observations(chart_csv):
    """直方图保留原始观测（8 行）而不是聚合。"""

    result = execute_chart(chart_csv, HISTOGRAM)

    assert result["success"] is True, result
    assert result["data_summary"]["row_count"] == 8, result["data_summary"]


def test_chart_png_lands_in_the_isolated_dir(chart_csv, chart_output_in_tmp):
    """绘图产物只能落在 tmp_path，不能落在项目的 temp_charts/。"""

    result = execute_chart(chart_csv, STANDARD_BAR)

    assert result["success"] is True, result

    chart_path = result["chart_path"]

    assert chart_path, result
    assert os.path.dirname(os.path.abspath(chart_path)) == (
        chart_output_in_tmp
    ), chart_path

    assert png_files(chart_output_in_tmp) == [
        os.path.basename(chart_path)
    ], png_files(chart_output_in_tmp)


# =========================================================
# 7. 干净的一次运行（v38 scenario ok + v39 单图）
# =========================================================

def test_clean_run_generates_exactly_one_chart(
    run_chart_graph, trace_events, tmp_path
):
    """一张图、一张 PNG、一条 chart_generated，节点是绿色完成。"""

    final = run_chart_graph([GOOD_CHARTS[0]])

    results = final.get("chart_results")
    report = final.get("final_report") or ""

    assert isinstance(results, list), results
    assert len(results) == 1, results
    assert results[0].get("success") is True, results[0]
    assert results[0].get("chart_path"), results[0]

    events = trace_events(final)

    assert events.count("chart_generated") == 1, events
    assert "chart_error" not in events, events

    assert len(png_files(tmp_path)) == 1, png_files(tmp_path)

    node = node_completed_event(final, "chart_executor")

    assert node is not None, final.get("agent_trace")
    assert node["display"]["level"] == "success", node["display"]

    assert report, final
    assert "未能完全完成" not in report, report


# =========================================================
# 8. 图表失败必须被看见（v38 scenario chart_fails）
# =========================================================

def test_single_chart_failure_is_disclosed(
    run_chart_graph, trace_events, tmp_path
):
    """失败不能被吞掉：要有 error 级 trace，也要写进报告。"""

    final = run_chart_graph([broken_chart()])

    results = final.get("chart_results") or []
    report = final.get("final_report") or ""

    assert results and results[0].get("success") is False, results

    errors = chart_error_events(final)

    assert len(errors) == 1, errors
    assert errors[0]["display"]["level"] == "error", errors[0]["display"]
    assert errors[0].get("error"), errors[0]

    # 不能出现「其实画好了」的误导性事件
    assert trace_events(final).count("chart_generated") == 0, trace_events(final)

    # 失败的那张不能留下半成品 PNG
    assert png_files(tmp_path) == [], png_files(tmp_path)

    # 节点要标成「需要关注」，而不是绿色完成
    node = node_completed_event(final, "chart_executor")

    assert node is not None, final.get("agent_trace")
    assert node["display"]["level"] != "success", node["display"]
    assert node["display"]["level"] == "warning", node["display"]

    # 模型写的报告保留，失败披露由程序追加
    assert "# 分析报告" in report, report
    assert "未能完全完成" in report, report
    assert "图表生成失败" in report, report
    assert "未经模型润色" in report, report


# =========================================================
# 9. 部分失败：成功的照旧，失败的如实披露（v39 多图）
# =========================================================

def test_partial_chart_failure_keeps_the_other_charts(
    run_chart_graph, trace_events, trace_titles, stub_llm, tmp_path
):
    """3 张图里坏 1 张：3 条结果、2 张 PNG、1 条 chart_error。"""

    final = run_chart_graph(GOOD_CHARTS + [broken_chart()])

    results = final.get("chart_results") or []
    report = final.get("final_report") or ""

    assert isinstance(results, list), results
    assert len(results) == 3, results

    successes = [item for item in results if item.get("success") is True]
    failures = [item for item in results if item.get("success") is not True]

    assert len(successes) == 2, results
    assert len(failures) == 1, results

    # 成功的图照旧；失败的那张没有孤儿 PNG
    assert len(png_files(tmp_path)) == 2, png_files(tmp_path)

    for item in successes:

        chart_path = item.get("chart_path")

        assert chart_path and os.path.isfile(chart_path), item
        assert os.path.abspath(chart_path).startswith(
            str(tmp_path)
        ), chart_path

    events = trace_events(final)

    assert events.count("chart_generated") == 2, events
    assert events.count("chart_error") == 1, events

    titles = trace_titles(final)

    assert any(
        "图表生成失败" in (title or "") for title in titles
    ), titles

    # 只有 1 张失败 -> 节点是「需要关注」，不是绿色完成
    node = node_completed_event(final, "chart_executor")

    assert node is not None, final.get("agent_trace")
    assert node["display"]["level"] == "warning", node["display"]

    # 报告提示词里逐张说明，失败的那张明说失败
    prompts = [
        call for call in stub_llm.calls
        if "专业的数据分析师" in call
    ]

    assert prompts, stub_llm.calls

    prompt = prompts[-1]

    assert "第 1 张图表" in prompt, prompt[-200:]
    assert "第 2 张图表" in prompt, prompt[-200:]
    assert "第 3 张图表生成失败" in prompt, prompt[-200:]

    # 最终报告必须如实披露失败的那张（程序追加，不经过模型）
    assert "第 3 张图表生成失败" in report, report[-120:]


# =========================================================
# 10. 全部失败：一张都不留
# =========================================================

def test_all_charts_failing_leaves_no_orphan_png(
    run_chart_graph, trace_events, tmp_path
):
    """全部失败时：0 张 PNG、0 条 chart_generated、报告逐张披露。"""

    final = run_chart_graph([
        broken_chart(3, "坏掉的图表一"),
        broken_chart(4, "坏掉的图表二"),
    ])

    results = final.get("chart_results") or []
    report = final.get("final_report") or ""

    assert len(results) == 2, results
    assert all(
        item.get("success") is False for item in results
    ), results

    assert png_files(tmp_path) == [], png_files(tmp_path)

    events = trace_events(final)

    assert events.count("chart_generated") == 0, events
    assert events.count("chart_error") == 2, events

    node = node_completed_event(final, "chart_executor")

    assert node is not None, final.get("agent_trace")
    assert node["display"]["level"] == "warning", node["display"]

    assert "未能完全完成" in report, report
    assert "第 1 张图表生成失败" in report, report
    assert "第 2 张图表生成失败" in report, report


# =========================================================
# 11. 分析任务失败也要写进报告（v38 scenario task_fails）
# =========================================================

def test_failed_task_is_disclosed_in_report(run_chart_graph, stub_llm):
    """修复次数用尽的任务必须出现在报告里，并保留真实报错。"""

    final = run_chart_graph(
        [broken_chart()],
        executor_rounds=3,
        code=BAD_CODE,
    )

    results = final.get("execution_results") or []
    report = final.get("final_report") or ""

    failed = [
        item for item in results if item.get("status") == "failed"
    ]

    assert len(failed) == 1, results
    assert (failed[0].get("error") or {}).get("type") == "KeyError", (
        failed[0]
    )

    repairs = [
        call for call in stub_llm.calls
        if "代码修复专家" in call
    ]

    assert len(repairs) == 2, len(repairs)

    assert "未能完全完成" in report, report
    assert "执行失败" in report, report
    assert str(failed[0].get("task")) in report, report
    assert "请不要据此推断" in report, report


# =========================================================
# 12. build_failure_notice 单元用例（v38）
# =========================================================

def test_failure_notice_is_empty_for_a_clean_state():

    notice = build_failure_notice(
        [], {"passed": True}, {"success": True}, "info"
    )

    assert notice == "", notice


def test_failure_notice_discloses_a_missing_dataset():

    notice = build_failure_notice(
        [], {"passed": True}, {"success": True}, None
    )

    assert "数据集读取失败" in notice, notice


def test_failure_notice_discloses_the_reviewer_gap():

    notice = build_failure_notice(
        [],
        {
            "passed": False,
            "reason": "缺地区维度分析",
            "missing_tasks": ["各地区汇总"],
        },
        {"success": True},
        "info",
    )

    assert "Reviewer 判定分析不完整" in notice, notice
    assert "各地区汇总" in notice, notice


def test_failure_notice_discloses_a_failed_task():

    notice = build_failure_notice(
        [{
            "step": 1,
            "task": "计算总和",
            "tool": "run_stat_calc",
            "status": "failed",
            "error": {"type": "KeyError", "message": "boom"},
        }],
        {"passed": True},
        {"success": True},
        "info",
    )

    assert "KeyError" in notice, notice
    assert "boom" in notice, notice


def test_failure_notice_discloses_a_failed_chart():
    """图表失败是报告披露的一部分：逐张写清楚，并标注系统追加。"""

    notice = build_failure_notice(
        [],
        {"passed": True},
        [
            {"success": True},
            {
                "success": False,
                "error": {"message": "数据集中不存在字段：不存在的字段"},
            },
        ],
        "info",
    )

    assert "第 2 张图表生成失败" in notice, notice
    assert "数据集中不存在字段" in notice, notice
    assert "未经模型润色" in notice, notice


# =========================================================
# 13. chart_results 形状 / API 契约（v39 末节）
# =========================================================

def test_analyze_endpoint_returns_servable_charts(
    stub_llm,
    tool_model,
    fake_tool_call,
    isolated_temp,
    serving_charts_from_tmp,
    chart_csv,
    tmp_path,
):
    """POST /analyze 返回 list，每项带 chart_url，且 URL 真的能取到 PNG。"""

    testclient = pytest.importorskip("fastapi.testclient")

    import main

    assert serving_charts_from_tmp, "main.app 上找不到 /charts 静态挂载"

    stub_llm.reply("中的 Planner", PLAN_JSON)
    stub_llm.reply("中的 Reviewer", REVIEWER_JSON)
    stub_llm.reply(
        "专业的数据分析师",
        "# 分析报告\n\n核心结论：已完成汇总。",
    )
    stub_llm.reply(
        "Chart Planner",
        json.dumps(
            {
                "need_chart": True,
                "charts": GOOD_CHARTS,
                "reason": "按需出图",
            },
            ensure_ascii=False,
        ),
    )

    tool_model.push_tool(
        fake_tool_call("call_0", "run_python", {"code": GOOD_CODE})
    )
    tool_model.push_text("已完成计算。")

    client = testclient.TestClient(main.app)

    with open(chart_csv, "rb") as handle:

        response = client.post(
            "/analyze",
            files={
                "file": ("test_chart_data.csv", handle, "text/csv"),
            },
            data={"question": "分析销售额"},
        )

    assert response.status_code == 200, response.text

    payload = response.json()

    assert not payload.get("error"), str(payload.get("error"))[:200]

    charts = payload.get("chart_results")

    assert isinstance(charts, list), type(charts).__name__
    assert len(charts) == 2, charts

    # 旧的单图字段不能又冒出来
    assert "chart_result" not in payload, sorted(payload)

    urls = [
        item.get("chart_url")
        for item in charts
        if item.get("success") is True
    ]

    assert len(urls) == 2, charts
    assert len(png_files(tmp_path)) == 2, png_files(tmp_path)

    for url in urls:

        assert url and url.startswith("/charts/"), url

        image = client.get(url)

        assert image.status_code == 200, (url, image.status_code)
        assert image.headers.get("content-type", "").startswith(
            "image/"
        ), image.headers
        assert len(image.content) > 5000, (
            url,
            len(image.content),
        )
