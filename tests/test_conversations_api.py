"""
会话历史接口（V4.15）

会话记录存在 data/conversations/ 下一个会话一个 JSON 文件。
这组用例走真实的 HTTP 路由（TestClient），
数据库（这里其实是目录）由 conftest 的夹具指到临时目录。
"""

import pytest

from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """
    只加载 app，不触发模型调用。

    main 模块导入时会建 FastAPI 实例；lifespan 里的清理
    对临时目录无副作用。
    """

    import main as main_module

    with TestClient(main_module.app) as instance:

        yield instance


@pytest.fixture
def seeded():
    """造一条有数据、有两轮问答的会话。"""

    from services import store
    from services.session_store import SessionStore

    sessions = SessionStore()

    sessions.create("conv-1")

    sessions.remember(
        "conv-1",
        {
            "dataset_info": "字段：地区、销售额",
            "file_path": "/tmp/sales.csv",
            "source_type": "csv",
            "source_name": "sales.csv",
            "input_kind": "table",
        },
        "各地区的销售额是多少",
        "# 报告\n\n华东最高",
    )

    sessions.remember(
        "conv-1",
        {},
        "那华东占多少",
        "华东占 38%。",
    )

    return store


# ========================================
# 列表
# ========================================

def test_the_list_starts_empty(client):

    body = client.get("/conversations").json()

    assert body["conversations"] == []
    assert body["total"] == 0


def test_the_list_shows_the_conversation(client, seeded):

    body = client.get("/conversations").json()

    assert body["total"] == 1

    item = body["conversations"][0]

    assert item["session_id"] == "conv-1"
    assert item["title"] == "各地区的销售额是多少"
    assert item["turn"] == 2
    assert item["source_name"] == "sales.csv"
    assert item["has_dataset"] is True


# ========================================
# 读取
# ========================================

def test_reading_a_conversation_returns_every_turn(client, seeded):

    body = client.get("/conversations/conv-1").json()

    assert body["session_id"] == "conv-1"
    assert len(body["history"]) == 2
    assert body["history"][0]["question"] == "各地区的销售额是多少"
    assert body["history"][1]["answer"] == "华东占 38%。"
    assert body["source_name"] == "sales.csv"


def test_reading_a_missing_conversation_is_404(client):

    assert client.get("/conversations/nope").status_code == 404


# ========================================
# 改名
# ========================================

def test_renaming(client, seeded):

    response = client.post(
        "/conversations/conv-1/rename",
        data={"title": "  华东季度分析  "},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "华东季度分析"

    body = client.get("/conversations").json()

    assert body["conversations"][0]["title"] == "华东季度分析"


def test_renaming_to_blank_is_rejected(client, seeded):

    response = client.post(
        "/conversations/conv-1/rename",
        data={"title": "   "},
    )

    assert response.status_code == 400


def test_renaming_a_missing_conversation_is_404(client):

    response = client.post(
        "/conversations/nope/rename",
        data={"title": "x"},
    )

    assert response.status_code == 404


# ========================================
# 删除
# ========================================

def test_deleting_removes_it_everywhere(client, seeded):

    assert client.get("/conversations/conv-1").status_code == 200

    assert client.delete("/conversations/conv-1").status_code == 200

    assert client.get("/conversations/conv-1").status_code == 404
    assert client.get("/conversations").json()["total"] == 0


# ========================================
# 导出
# ========================================

def test_export_returns_a_markdown_download(client, seeded):

    response = client.get("/conversations/conv-1/export")

    assert response.status_code == 200
    assert "markdown" in response.headers["content-type"]

    assert "attachment" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]

    text = response.text

    assert "各地区的销售额是多少" in text
    assert "华东占 38%。" in text
    assert "第 2 轮" in text


def test_exporting_a_missing_conversation_is_404(client):

    assert client.get("/conversations/nope/export").status_code == 404


# ========================================
# 落库时机
# ========================================

def test_creating_a_session_shows_up_in_the_list(client):

    from services.session_store import SessionStore

    SessionStore().create("brand-new")

    body = client.get("/conversations").json()

    assert body["total"] == 1
    assert body["conversations"][0]["session_id"] == "brand-new"
