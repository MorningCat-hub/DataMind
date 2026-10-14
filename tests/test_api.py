"""
HTTP 契约：/analyze 与 /chat/stream

这些测试只验证**接口形状**（状态码、字段、SSE 帧类型与顺序），
不验证分析质量 —— 那是 eval/ 的职责。
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(isolated_temp, stub_llm, tool_model, fake_tool_call, chart_csv, monkeypatch):

    import main

    # 让上传落在 tmp_path 里，不碰项目目录
    monkeypatch.setattr(main, "TEMP_FOLDER", isolated_temp["temp"], raising=False)

    stub_llm.reply("中的 Planner", json.dumps(
        {"plan": [{"step": 1, "task": "汇总销售额", "tool": "run_stat_calc"}]},
        ensure_ascii=False,
    ))
    stub_llm.reply("中的 Reviewer", json.dumps(
        {"passed": True, "reason": "ok", "missing_tasks": []}, ensure_ascii=False
    ))
    stub_llm.reply("Chart Planner", json.dumps(
        {"need_chart": False, "charts": [], "reason": "不需要"}, ensure_ascii=False
    ))
    stub_llm.reply("专业的数据分析师", "## 结论\n销售额合计为 85,000 元。")

    tool_model.push_tool(fake_tool_call(
        "c1", "aggregate_metric", {"group_by": None, "metric": "销售额", "agg": "sum"}
    ))
    tool_model.push_text("销售额合计 85,000。")

    return TestClient(main.app)


def test_analyze_returns_the_full_payload(client, chart_csv):

    with open(chart_csv, "rb") as handle:

        response = client.post(
            "/analyze",
            files={"file": ("sales.csv", handle, "text/csv")},
            data={"question": "销售额合计"},
        )

    assert response.status_code == 200

    body = response.json()

    for key in ("final_analysis", "execution_results", "agent_trace",
                "verification", "knowledge_hits"):

        assert key in body, key

    assert isinstance(body["verification"], dict)


def test_stream_emits_frames_in_order(client, chart_csv):

    frames = []

    with client.stream(
        "POST",
        "/chat/stream",
        files={"file": ("sales.csv", open(chart_csv, "rb"), "text/csv")},
        data={"question": "销售额合计", "session_id": ""},
    ) as response:

        assert response.status_code == 200

        for line in response.iter_lines():

            if line and line.startswith("data: "):

                frames.append(json.loads(line[6:]))

    kinds = [frame.get("type") for frame in frames]

    assert kinds[0] == "start"
    assert kinds[-1] == "done"
    assert "report" in kinds
    assert "meta" in kinds

    meta = [frame for frame in frames if frame.get("type") == "meta"][0]

    assert "verification" in meta
    assert "knowledge_hits" in meta


def test_the_static_mounts_exist(client):

    for path in ("/", "/frontend/app.js", "/frontend/style.css"):

        assert client.get(path).status_code == 200, path
