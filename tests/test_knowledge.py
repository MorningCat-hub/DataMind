"""
业务口径检索（RAG）

检索方式是**字符二元组 + 别名匹配**，词法完全没命中时才让模型兜底挑一次。

为什么不用向量检索：
当前 API 供应商没有 embedding 接口（实测所有候选模型都是 404），
而且字典只有几十条、问题里通常直接出现指标名。

历史上踩过的一个坑（保留成回归用例）：
「所需字段齐全」那个加分项自己把分数顶过了命中阈值，
导致词法上几乎不相干的口径也被判为命中，LLM 兜底永远轮不到。
所以阈值只看词法得分，加分只影响排序。
"""

import os

import pytest

from agent.utils.knowledge import (
    dataset_columns,
    format_knowledge_block,
    load_entries,
    retrieve,
    score_entry,
    summarize_hits,
)


# ========================================
# 字典
# ========================================

def test_the_dictionary_loads():

    entries = load_entries()

    assert len(entries) >= 9
    assert all(item.get("name") and item.get("definition") for item in entries)


def test_the_dictionary_has_net_sales():

    assert any(item["name"] == "净销售额" for item in load_entries())


def test_a_missing_file_degrades_to_empty(project_root):

    assert load_entries(os.path.join(project_root, "nope.yaml")) == []


def test_columns_come_from_the_csv_header(demo_csv):

    columns = dataset_columns(demo_csv)

    assert "销售额" in columns
    assert "退款金额" in columns
    assert "订单状态" in columns


# ========================================
# 词法检索
# ========================================

def test_retrieves_by_metric_name(demo_csv):

    result = retrieve("净销售额是多少？", file_path=demo_csv, use_llm_fallback=False)

    assert result["method"] == "lexical"
    assert result["hits"][0]["entry"]["name"] == "净销售额"


def test_the_hit_explains_itself(demo_csv):

    reasons = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )["hits"][0]["reasons"]

    assert any("命中名称" in reason for reason in reasons)
    assert any("所需字段齐全" in reason for reason in reasons)


def test_an_alias_also_retrieves_the_entry(demo_csv):

    names = [
        item["entry"]["name"]
        for item in retrieve(
            "实收金额有多少？", file_path=demo_csv, use_llm_fallback=False
        )["hits"]
    ]

    assert "净销售额" in names


def test_a_different_metric_is_retrieved(demo_csv):

    names = [
        item["entry"]["name"]
        for item in retrieve(
            "毛利率是多少？", file_path=demo_csv, use_llm_fallback=False
        )["hits"]
    ]

    assert "毛利率" in names


def test_an_unrelated_question_retrieves_nothing(demo_csv):

    result = retrieve(
        "今天天气怎么样？", file_path=demo_csv, use_llm_fallback=False
    )

    assert result["hits"] == []


def test_top_k_is_respected(demo_csv):

    result = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False, top_k=1
    )

    assert len(result["hits"]) == 1


def test_the_applicability_bonus_does_not_manufacture_a_hit(demo_csv):
    """
    回归：加分项只该影响排序，不该把不相干的口径顶过命中阈值，
    否则 LLM 兜底永远轮不到。
    """

    _score, _reasons, lexical = score_entry(
        "实际收入有多少？",
        {"name": "净销售额", "requires": ["销售额"]},
        ["销售额"],
    )

    assert lexical < 3.0, "纯词法得分必须低于阈值"


# ========================================
# 提示词片段
# ========================================

def test_the_block_carries_the_formula_and_the_guards(demo_csv):

    hits = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )["hits"]

    block = format_knowledge_block(hits)

    assert "扣除退款金额和折扣金额" in block
    assert "销售额 - 退款金额 - 折扣金额" in block
    assert "涉及字段" in block
    assert "已完成" in block and "已取消" in block
    assert "不要用相近字段凑数" in block


def test_an_empty_hit_list_produces_no_block():

    assert format_knowledge_block([]) == ""


def test_summarize_hits_has_the_fields_the_api_needs(demo_csv):

    summarized = summarize_hits(retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )["hits"])

    assert set(summarized[0]) >= {"name", "type", "reasons", "score"}


# ========================================
# LLM 兜底
# ========================================

@pytest.fixture
def knowledge_llm(monkeypatch):

    import agent.utils.knowledge as knowledge

    holder = {"reply": "无"}

    def fake(messages, temperature=0.2):

        return holder["reply"]

    monkeypatch.setattr(knowledge, "call_llm", fake)

    return holder


@pytest.mark.parametrize(
    "reply, expected_method, expected_names",
    [
        ("净销售额", "llm", ["净销售额"]),
        ("无", "none", []),
        ("不存在的指标", "none", []),
    ],
)
def test_llm_fallback(demo_csv, knowledge_llm, reply, expected_method, expected_names):

    knowledge_llm["reply"] = reply

    result = retrieve("实际收入有多少？", file_path=demo_csv, use_llm_fallback=True)

    assert result["method"] == expected_method, result
    assert [item["entry"]["name"] for item in result["hits"]] == expected_names


# ========================================
# 检索节点
# ========================================

def test_the_node_sets_context_and_traces(demo_csv, make_state, trace_events):

    from agent.nodes.retriever import retrieve_knowledge

    out = retrieve_knowledge(make_state(question="净销售额是多少？", file_path=demo_csv))

    assert "销售额 - 退款金额 - 折扣金额" in out["knowledge_context"]
    assert out["knowledge_hits"][0]["name"] == "净销售额"

    assert "knowledge_retrieved" in trace_events(out)

    event = out["agent_trace"][0]

    assert "净销售额" in event["message"]
    assert event["display"]["visible"] is True


def test_no_match_produces_an_empty_event(demo_csv, make_state, trace_events):

    from agent.nodes.retriever import retrieve_knowledge
    import agent.nodes.retriever as retriever

    original = retriever.retrieve
    retriever.retrieve = lambda *a, **k: {"hits": [], "method": "none"}

    try:

        out = retrieve_knowledge(make_state(question="今天天气怎么样？",
                                            file_path=demo_csv))

    finally:

        retriever.retrieve = original

    assert "knowledge_empty" in trace_events(out)
    assert not out.get("knowledge_context")


def test_non_table_input_skips_retrieval(demo_csv, make_state):

    from agent.nodes.retriever import retrieve_knowledge

    out = retrieve_knowledge(make_state(
        question="净销售额是多少？", file_path=demo_csv, input_kind="text"
    ))

    assert out["knowledge_context"] is None
    assert out["knowledge_hits"] == []


# ========================================
# 注入两个提示词
# ========================================

def test_the_block_reaches_the_planner(demo_csv, stub_llm, make_state):

    import agent.nodes.planner as planner

    stub_llm.reply("中的 Planner", '{"plan": []}')

    hits = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )["hits"]

    planner.planner(make_state(
        question="净销售额是多少？",
        knowledge_context=format_knowledge_block(hits),
    ))

    assert "销售额 - 退款金额 - 折扣金额" in stub_llm.calls[-1]


def test_the_planner_says_so_when_there_is_no_context(stub_llm, make_state):

    import agent.nodes.planner as planner

    stub_llm.reply("中的 Planner", '{"plan": []}')

    planner.planner(make_state(question="销售额合计"))

    assert "没有检索到相关的业务口径" in stub_llm.calls[-1]


def test_the_block_reaches_the_executor(demo_csv, chart_csv, tool_model,
                                        fake_tool_call, make_state):

    import agent.nodes.executor as executor

    hits = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )["hits"]

    tool_model.push_tool(
        fake_tool_call("c1", "aggregate_metric", {"metric": "销售额", "agg": "sum"})
    )
    tool_model.push_text("完成。")

    executor.executor(make_state(
        file_path=chart_csv,
        plan=[{"step": 1, "task": "计算净销售额", "tool": "run_stat_calc"}],
        knowledge_context=format_knowledge_block(hits),
    ))

    prompt = tool_model.calls[0]["messages"][1]["content"]

    assert "销售额 - 退款金额 - 折扣金额" in prompt
    assert "不要用相近字段凑数" in prompt
    assert "计算净销售额" in prompt


def test_without_context_the_executor_prompt_has_no_block(
    chart_csv, tool_model, fake_tool_call, make_state
):

    import agent.nodes.executor as executor

    tool_model.push_text("完成。")

    executor.executor(make_state(
        file_path=chart_csv,
        plan=[{"step": 1, "task": "汇总", "tool": "run_stat_calc"}],
    ))

    assert "必须遵守的业务口径" not in tool_model.calls[0]["messages"][1]["content"]


def test_the_executor_is_told_not_to_invent_a_definition(project_root):
    """回归：字段不存在时，模型编了一个定义（把退款+折扣当成成本）。"""

    source = open(
        os.path.join(project_root, "agent", "nodes", "executor.py"),
        encoding="utf-8",
    ).read()

    assert source.count("不要用其他字段凑出一个定义") >= 2, (
        "系统提示词和任务提示词都要有"
    )
