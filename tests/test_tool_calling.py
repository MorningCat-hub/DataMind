"""
原生 Tool Calling：工具注册表 + 工具调用循环

历史上踩过的坑（都保留成回归用例）：

1. `tool_choice` 强制指定会返回 400（模型跑在思考模式），只能固定 auto
2. 回填历史必须带上 `reasoning_content`
3. 模型可能一次返回多个 tool_calls，
   **每一个 tool_call_id 都必须有对应的 tool 消息**，少一个下一轮就 400
4. `aggregate_metric` 一开始只支持一个分组字段，
   于是交叉表只能落到 run_python —— 这是工具集缺口，不是模型问题
"""

import json
import os

import pytest

from tools.tool_registry import (
    TOOL_NAMES,
    TOOL_SCHEMAS,
    execute_tool_call,
    parse_arguments,
)


# ========================================
# 工具声明
# ========================================

def test_four_tools_are_declared():

    assert set(TOOL_NAMES) == {
        "describe_dataset",
        "aggregate_metric",
        "describe_metric",
        "run_python",
    }


def test_every_schema_is_described_and_has_parameters():

    for schema in TOOL_SCHEMAS:

        function = schema["function"]

        assert function.get("description"), function["name"]
        assert "parameters" in function


# ========================================
# describe_dataset
# ========================================

def test_describe_dataset_lists_columns_and_types(chart_csv):

    outcome = execute_tool_call("describe_dataset", {}, chart_csv)

    assert outcome["ok"] is True, outcome
    assert all(name in outcome["observation"] for name in ("地区", "品类", "销售额"))
    assert outcome.get("summary")


# ========================================
# aggregate_metric
# ========================================

def test_aggregate_metric_sums_by_group(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "销售额", "agg": "sum"},
        chart_csv,
    )

    assert outcome["ok"] is True, outcome
    assert "30,000" in outcome["observation"]
    assert "33,000" in outcome["observation"]


def test_aggregate_metric_without_grouping(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": None, "metric": "销售额", "agg": "sum"},
        chart_csv,
    )

    assert outcome["ok"] is True
    assert "85,000" in outcome["observation"]


def test_aggregate_metric_count(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "销售额", "agg": "count"},
        chart_csv,
    )

    assert outcome["ok"] is True
    assert "记录数" in outcome["observation"]


def test_a_missing_column_is_rejected_and_lists_what_exists(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "不存在的字段", "metric": "销售额", "agg": "sum"},
        chart_csv,
    )

    assert outcome["ok"] is False
    assert "地区" in outcome["observation"]
    assert outcome.get("observation"), "失败时也必须给模型一段可读的说明"


def test_a_non_numeric_metric_suggests_count(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "品类", "agg": "sum"},
        chart_csv,
    )

    assert outcome["ok"] is False
    assert "count" in outcome["observation"]


def test_an_unsupported_aggregation_is_rejected(chart_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "销售额", "agg": "median"},
        chart_csv,
    )

    assert outcome["ok"] is False


# ========================================
# 时间粒度
# ========================================

def test_monthly_aggregation_is_chronological(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "订单日期", "metric": "销售额", "agg": "sum",
         "time_granularity": "month"},
        sales_csv,
    )

    assert outcome["ok"] is True, outcome
    assert "2026-01" in outcome["observation"]
    assert "2026-09" in outcome["observation"]
    assert outcome["observation"].index("2026-01") < outcome["observation"].index("2026-09")


def test_quarterly_aggregation(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "订单日期", "metric": "销售额", "agg": "sum",
         "time_granularity": "quarter"},
        sales_csv,
    )

    assert outcome["ok"] is True
    assert "2026Q3" in outcome["observation"]


def test_a_non_date_group_by_degrades_gracefully(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "销售额", "agg": "sum",
         "time_granularity": "month"},
        sales_csv,
    )

    assert outcome["ok"] is True
    assert "已忽略时间粒度" in outcome["observation"]


# ========================================
# 交叉分组（v4.5.1：工具集缺口的修复）
# ========================================

def test_two_dimension_grouping_works(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": ["地区", "订单日期"], "metric": "销售额", "agg": "sum",
         "time_granularity": "quarter"},
        sales_csv,
    )

    assert outcome["ok"] is True, outcome
    assert "华东" in outcome["observation"]
    assert "2026Q1" in outcome["observation"]
    assert "17" in outcome["summary"]


def test_a_single_column_still_works(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": "地区", "metric": "销售额", "agg": "sum"},
        sales_csv,
    )

    assert outcome["ok"] is True
    assert "6 行" in outcome["summary"]


def test_a_bad_column_inside_the_list_is_rejected(sales_csv):

    outcome = execute_tool_call(
        "aggregate_metric",
        {"group_by": ["地区", "不存在"], "metric": "销售额", "agg": "sum"},
        sales_csv,
    )

    assert outcome["ok"] is False
    assert "地区" in outcome["observation"]


# ========================================
# 其它工具与分发
# ========================================

def test_describe_metric_reports_distribution(chart_csv):

    outcome = execute_tool_call("describe_metric", {"metric": "销售额"}, chart_csv)

    assert outcome["ok"] is True
    assert "均值" in outcome["observation"]
    assert "中位数" in outcome["observation"]


def test_run_python_is_the_escape_hatch(chart_csv):

    outcome = execute_tool_call(
        "run_python", {"code": "result = df['销售额'].sum()"}, chart_csv
    )

    assert outcome["ok"] is True
    assert "85000" in outcome["observation"]


def test_broken_code_reports_the_exception_type(chart_csv):

    outcome = execute_tool_call(
        "run_python", {"code": "result = df['不存在'].sum()"}, chart_csv
    )

    assert outcome["ok"] is False
    assert "KeyError" in outcome["observation"]


def test_an_unknown_tool_lists_the_real_ones(chart_csv):

    outcome = execute_tool_call("no_such_tool", {}, chart_csv)

    assert outcome["ok"] is False
    assert "aggregate_metric" in outcome["observation"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"a": 1}, {"a": 1}),
        ('{"metric": "销售额"}', {"metric": "销售额"}),
        (None, {}),
        ("not json", {}),
        ("[1,2]", {}),
    ],
)
def test_parse_arguments_is_forgiving(raw, expected):

    assert parse_arguments(raw) == expected


# ========================================
# 工具调用循环
# ========================================

PLAN = [{"step": 1, "task": "计算各地区的销售额合计", "tool": "run_stat_calc"}]


def test_the_loop_protocol_is_correct(chart_csv, tool_model, fake_tool_call, make_state):

    import agent.nodes.executor as executor

    tool_model.push_tool(
        fake_tool_call("call_1", "describe_dataset", "{}"),
        fake_tool_call("call_2", "aggregate_metric",
                       {"group_by": "地区", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_text("完成。")

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    assert state["execution_status"] == "success"
    assert state["current_step"] == 1

    entry = state["execution_results"][0]

    assert entry["status"] == "success"
    assert entry["tool"] == "tool_calling"
    assert [call["name"] for call in entry["data"]["tool_calls"]] == [
        "describe_dataset", "aggregate_metric",
    ]
    assert entry["data"]["tool_calls"][1]["arguments"]["group_by"] == "地区"
    assert entry["metadata"]["tools"] == ["describe_dataset", "aggregate_metric"]

    # data.result 是核对器提取真实数值的地方
    assert "30,000" in entry["data"]["result"]

    # 第二次调用时，历史里必须有 assistant + 每个 tool_call 各一条 tool 消息
    second = tool_model.calls[1]["messages"]
    roles = [item["role"] for item in second]

    assert "assistant" in roles
    assert roles.count("tool") == 2

    tool_ids = sorted(
        item.get("tool_call_id") for item in second if item["role"] == "tool"
    )

    assert tool_ids == ["call_1", "call_2"], "每个 tool_call_id 都必须有回复"

    assistant = [item for item in second if item["role"] == "assistant"][0]

    assert assistant.get("reasoning_content") == "思考中", (
        "思考模式要求把 reasoning_content 带回去"
    )
    assert len(assistant["tool_calls"]) == 2

    # 工具列表与 tool_choice
    assert len(tool_model.calls[0]["tools"]) == 4
    assert tool_model.calls[0]["tool_choice"] == "auto"


def test_the_loop_recovers_from_a_bad_argument(
    chart_csv, tool_model, fake_tool_call, make_state, trace_events
):

    import agent.nodes.executor as executor

    tool_model.push_tool(
        fake_tool_call("c1", "aggregate_metric",
                       {"group_by": "亏区", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_tool(
        fake_tool_call("c2", "aggregate_metric",
                       {"group_by": "地区", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_text("修正后完成。")

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    entry = state["execution_results"][0]

    assert state["execution_status"] == "success"
    assert [call["ok"] for call in entry["data"]["tool_calls"]] == [False, True]

    fed_back = [
        item for item in tool_model.calls[1]["messages"] if item["role"] == "tool"
    ]

    assert "没有字段" in fed_back[0]["content"], "错误必须回填给模型"


def test_a_run_with_no_successful_call_is_retryable(
    chart_csv, tool_model, fake_tool_call, make_state
):

    import agent.nodes.executor as executor

    tool_model.push_tool(
        fake_tool_call("c1", "aggregate_metric",
                       {"group_by": "错", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_text("我算不出来。")

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    assert state["execution_status"] == "retry_needed"
    assert state["last_execution_error"]
    assert not state.get("execution_results")


def test_the_retry_cap_produces_exhausted(
    chart_csv, tool_model, fake_tool_call, make_state
):

    import agent.nodes.executor as executor

    tool_model.push_tool(
        fake_tool_call("c1", "aggregate_metric",
                       {"group_by": "错", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_text("我算不出来。")

    state = executor.executor(
        make_state(file_path=chart_csv, plan=PLAN, current_task_retry_count=2)
    )

    assert state["execution_status"] == "exhausted"


def test_the_loop_is_bounded(chart_csv, tool_model, fake_tool_call, make_state):

    import agent.nodes.executor as executor

    for index in range(executor.MAX_TOOL_ROUNDS):

        tool_model.push_tool(
            fake_tool_call(f"c{index}", "describe_dataset", "{}")
        )

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    assert executor.MAX_TOOL_ROUNDS == 8
    assert state["execution_status"] == "retry_needed"


def test_an_api_error_becomes_retryable(chart_csv, monkeypatch, make_state):

    import agent.nodes.executor as executor

    def boom(messages, tools, temperature=0.2, tool_choice="auto"):

        raise RuntimeError("API 挂了")

    monkeypatch.setattr(executor, "call_llm_with_tools", boom)

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    assert state["execution_status"] == "retry_needed"
    assert state["last_execution_error"]["type"] == "RuntimeError"


def test_the_repair_hint_reaches_the_model(chart_csv, tool_model, make_state):

    import agent.nodes.executor as executor

    tool_model.push_text("完成。")

    executor.executor(make_state(
        file_path=chart_csv,
        plan=PLAN,
        current_task_retry_count=1,
        pending_code="result = df['销售额'].sum()",
    ))

    assert "result = df['销售额'].sum()" in tool_model.calls[0]["messages"][1]["content"]


def test_tool_errors_are_visible_in_the_trace(
    chart_csv, tool_model, fake_tool_call, make_state, trace_events
):

    import agent.nodes.executor as executor

    tool_model.push_tool(
        fake_tool_call("c1", "aggregate_metric",
                       {"group_by": "错", "metric": "销售额", "agg": "sum"}),
    )
    tool_model.push_text("完成。")

    state = executor.executor(make_state(file_path=chart_csv, plan=PLAN))

    assert "tool_call" in trace_events(state)
    assert "tool_error" in trace_events(state)


def test_the_legacy_code_generation_path_is_gone(project_root):

    source = open(
        os.path.join(project_root, "agent", "nodes", "executor.py"),
        encoding="utf-8",
    ).read()

    assert "build_code_generation_prompt" not in source
    assert "exec(" not in source, "常规路径不该再用 exec"
    assert "TEST_FORCE_ERROR" not in source
    assert "def clean_code" in source, "error_handler 还要 import 它"


def test_error_handler_still_imports_clean_code():

    import agent.nodes.error_handler as error_handler

    assert callable(getattr(error_handler, "clean_code", None))
