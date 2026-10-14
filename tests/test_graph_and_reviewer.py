"""
图接线与 Reviewer

历史问题（保留成回归用例）：

1. reviewer 里有一段「如果没有剩余缺失任务就保持 passed」的判断，
   实际上条件等价于它自己、赋值又等于没赋 —— 整段是空操作，
   于是「唯一不通过理由只是还没画图」会导致白白重规划两次。

2. 用来过滤绘图任务的**关键词是连续子串**（"生成柱状图"），
   像「生成各地区销售额柱状图」这种很自然的说法会漏过去，
   修复 1 的效果就被绕过了。
"""

import json
import os
import re

import pytest


def _graph_nodes(project_root):

    source = open(
        os.path.join(project_root, "agent", "graph.py"), encoding="utf-8"
    ).read()

    return re.findall(r'workflow\.add_node\(\s*"([a-z_]+)"', source)


def test_the_graph_has_the_expected_nodes(project_root):

    nodes = _graph_nodes(project_root)

    assert set(nodes) == {
        "dataset", "health_check", "retrieve", "planner", "executor",
        "error_handler", "task_failure", "reviewer", "retry",
        "chart_planner", "chart_executor", "report", "verify",
        "conclusion_check",
    }


def test_dataset_routes_through_retrieve_before_planner(project_root):

    source = open(
        os.path.join(project_root, "agent", "graph.py"), encoding="utf-8"
    ).read()

    # V4.11：读取之后先做数据体检，再检索口径
    assert 'return "health_check"' in source
    assert 'return "retrieve"' not in source
    assert source.count('"health_check"') >= 3
    assert source.count('"retrieve"') >= 3


# ========================================
# Reviewer 的修复
# ========================================

@pytest.fixture
def review(stub_llm, make_state):
    """用给定 payload 跑一次 reviewer。"""

    import agent.nodes.reviewer as reviewer

    def _run(payload):

        stub_llm.default = json.dumps(payload, ensure_ascii=False)

        return reviewer.reviewer(make_state(
            question="各地区销售额",
            dataset_info="字段：地区、销售额",
            plan=[{"step": 1, "task": "汇总", "tool": "run_stat_calc"}],
            execution_results=[{
                "step": 1, "task": "汇总", "tool": "tool_calling",
                "status": "success", "data": {"result": "华东 100"},
            }],
            chart_results=[],
        ))

    return _run


@pytest.mark.parametrize(
    "task",
    [
        "生成各地区销售额柱状图",
        "补一张各地区的可视化",
        "生成图表",
        "输出图片",
        "plot_chart 一张",
    ],
)
def test_chart_only_missing_tasks_do_not_fail_the_review(review, task):

    out = review({"passed": False, "reason": "缺少图表", "missing_tasks": [task]})

    assert out["review_result"]["passed"] is True, task
    assert out["review_result"]["missing_tasks"] == [], task


def test_a_real_missing_task_still_fails_the_review(review):

    out = review({
        "passed": False,
        "reason": "缺少分析",
        "missing_tasks": ["计算各地区销售额的同比"],
    })

    assert out["review_result"]["passed"] is False
    assert out["review_result"]["missing_tasks"] == ["计算各地区销售额的同比"]


def test_a_passing_review_stays_passing(review):

    out = review({"passed": True, "reason": "完备", "missing_tasks": []})

    assert out["review_result"]["passed"] is True


def test_a_passing_review_with_chart_tasks_is_still_passing(review):

    out = review({
        "passed": True,
        "reason": "完备",
        "missing_tasks": ["生成趋势折线图"],
    })

    assert out["review_result"]["passed"] is True
    assert out["review_result"]["missing_tasks"] == []


def test_the_no_op_condition_is_gone(project_root):

    source = open(
        os.path.join(project_root, "agent", "nodes", "reviewer.py"),
        encoding="utf-8",
    ).read()

    code_only = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )

    assert "not missing_tasks and not review_result" not in code_only
    assert (
        'if review_result.get("missing_tasks") and not missing_tasks:'
        in code_only
    )


def test_the_filter_matches_chart_types_not_phrases(project_root):

    source = open(
        os.path.join(project_root, "agent", "nodes", "reviewer.py"),
        encoding="utf-8",
    ).read()

    for keyword in ("柱状图", "折线图", "可视化", "图表"):

        assert f'"{keyword}"' in source, keyword


# ========================================
# 失败记录的统一字段
# ========================================

SUCCESS_KEYS = {
    "step", "task", "tool", "status", "repair_count", "auto_repaired",
    "original_code", "final_code", "repair_history", "data", "metadata",
}


def test_failure_entries_have_the_same_shape_as_success_entries(make_state):

    import agent.nodes.task_failure as failure

    out = failure.mark_task_failed(make_state(
        plan=[{"step": 1, "task": "汇总销售额", "tool": "run_stat_calc"}],
        last_execution_error={"type": "KeyError", "message": "'成本'"},
        current_task_retry_count=1,
    ))

    entry = out["execution_results"][0]

    assert SUCCESS_KEYS.issubset(set(entry)), SUCCESS_KEYS - set(entry)
    assert entry["status"] == "failed"
    assert entry["repair_count"] == 1
    assert entry["error"]["type"] == "KeyError", "Reporter 靠它生成失败提示"
    assert set(entry["data"]) == {"result", "output", "tool_calls"}
    assert "retry_count" not in entry and "code" not in entry
