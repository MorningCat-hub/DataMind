"""
对话持久化（services/store.py）

核心诉求只有一条：**重启之后对话还在。**

存法是「一个会话一个 JSON 文件」，所以顺便也验证：
文件是合法 JSON、一次会话一个文件、会话 id 不能跑到目录外面去。
"""

import os

import pytest

from services import store


@pytest.fixture(autouse=True)
def fresh_store(tmp_path):

    store.reset_store(str(tmp_path / "conversations"))

    yield

    store.reset_store(str(tmp_path / "conversations"))


def make_session(session_id="s1", question="各地区的销售额", answer="报告正文"):

    return {
        "session_id": session_id,
        "dataset_info": "字段：地区、销售额",
        "file_path": "/tmp/data.csv",
        "input_context": None,
        "source_type": "csv",
        "source_name": "sales.csv",
        "input_kind": "table",
        "dataset_notes": [],
        "history": [{"question": question, "answer": answer}],
        "turn": 1,
        "is_followup": False,
        "last_active": 123.0,
    }


def test_a_conversation_round_trips():

    store.save_conversation(make_session())

    loaded = store.load_conversation("s1")

    assert loaded is not None
    assert loaded["history"][0]["question"] == "各地区的销售额"
    assert loaded["file_path"] == "/tmp/data.csv"


def test_a_missing_conversation_returns_none():

    assert store.load_conversation("nope") is None


def test_conversations_list_newest_first():

    store.save_conversation(make_session("old"))
    store.save_conversation(make_session("new", question="第二个问题"))

    items = store.list_conversations()

    assert len(items) == 2
    assert items[0]["session_id"] == "new"


def test_the_list_carries_what_the_sidebar_needs():

    store.save_conversation(make_session())

    item = store.list_conversations()[0]

    assert item["title"] == "各地区的销售额"
    assert item["turn"] == 1
    assert item["source_name"] == "sales.csv"
    assert item["has_dataset"] is True
    assert item["updated_at"]


def test_the_title_comes_from_the_first_question():

    store.save_conversation(make_session(question="华东的净销售额是多少"))

    assert store.list_conversations()[0]["title"] == "华东的净销售额是多少"


def test_a_long_question_is_truncated():

    store.save_conversation(make_session(question="问" * 120))

    assert len(store.list_conversations()[0]["title"]) <= 40


def test_saving_twice_updates_instead_of_duplicating():

    store.save_conversation(make_session())

    session = make_session()

    session["history"].append({"question": "追问", "answer": "第二轮"})
    session["turn"] = 2

    store.save_conversation(session)

    assert store.count_conversations() == 1
    assert store.load_conversation("s1")["turn"] == 2
    assert len(store.load_conversation("s1")["history"]) == 2


def test_saving_does_not_reset_created_at():

    store.save_conversation(make_session())

    first = store.list_conversations()[0]["created_at"]

    store.save_conversation(make_session())

    assert store.list_conversations()[0]["created_at"] == first


def test_rename():

    store.save_conversation(make_session())

    assert store.rename_conversation("s1", "  华东分析  ") is True

    assert store.list_conversations()[0]["title"] == "华东分析"


def test_rename_rejects_an_empty_title():

    store.save_conversation(make_session())

    assert store.rename_conversation("s1", "   ") is False


def test_rename_a_missing_conversation():

    assert store.rename_conversation("nope", "x") is False


def test_delete():

    store.save_conversation(make_session())

    assert store.delete_conversation("s1") is True
    assert store.count_conversations() == 0
    assert store.load_conversation("s1") is None


def test_a_broken_file_does_not_crash():

    store.save_conversation(make_session())

    path = os.path.join(store.store_dir(), "s1.json")

    with open(path, "w", encoding="utf-8") as handle:

        handle.write("{ 这不是 JSON")

    assert store.load_conversation("s1") is None
    assert store.list_conversations() == []


def test_a_session_id_cannot_escape_the_directory():

    assert store.delete_conversation("../../etc/passwd") is False
    assert store.load_conversation("../secrets") is None
    assert store.save_conversation(
        {"session_id": "../evil", "history": []}
    ) is None
    assert not os.path.exists(
        os.path.join(os.path.dirname(store.store_dir()), "evil.json")
    )


def test_the_store_directory_is_reused_not_recreated(tmp_path):

    path = str(tmp_path / "reuse")

    store.set_store_dir(path)
    store.save_conversation(make_session("a"))

    store.set_store_dir(path)

    assert store.load_conversation("a") is not None

    store.reset_store(str(tmp_path / "other"))


def test_export_markdown_contains_every_turn():

    session = make_session()

    session["history"].append({"question": "追问一下", "answer": "第二份报告"})

    store.save_conversation(session)

    text = store.export_markdown("s1")

    assert "各地区的销售额" in text
    assert "追问一下" in text
    assert "第二份报告" in text
    assert "第 2 轮" in text


def test_export_a_missing_conversation_returns_none():

    assert store.export_markdown("nope") is None


# ========================================
# 会话仓储层的持久化
# ========================================

def test_the_session_store_reloads_from_disk():
    """
    这是整个功能的核心：
    内存里没有的会话，要从数据库里恢复出来。
    """

    from services.session_store import SessionStore

    first = SessionStore()

    session = first.create("keepme")

    session["history"].append({"question": "问题", "answer": "回答"})
    session["turn"] = 1

    # 走一次真实的 remember：它内部会落库
    first.remember(
        "keepme",
        {
            "dataset_info": "字段",
            "file_path": "/tmp/x.csv",
            "source_type": "csv",
            "source_name": "x.csv",
            "input_kind": "table",
        },
        "问题",
        "回答",
    )

    # 换一个全新的仓储实例（等价于服务重启）
    second = SessionStore()

    restored = second.get("keepme")

    assert restored is not None, "重启后必须能读回来"
    assert restored["history"][0]["question"] == "问题"


def test_dropping_a_session_removes_it_from_disk():

    from services.session_store import SessionStore

    single = SessionStore()

    single.create("gone")

    assert store.load_conversation("gone") is not None

    single.drop("gone")

    assert store.load_conversation("gone") is None


def test_one_file_per_conversation():

    store.save_conversation(make_session("a"))
    store.save_conversation(make_session("b"))

    files = sorted(
        name for name in os.listdir(store.store_dir())
        if name.endswith(".json")
    )

    assert files == ["a.json", "b.json"]


def test_the_file_is_readable_json_with_the_session_inside():

    import json

    store.save_conversation(make_session())

    path = os.path.join(store.store_dir(), "s1.json")

    payload = json.load(open(path, encoding="utf-8"))

    assert payload["title"] == "各地区的销售额"
    assert payload["session"]["history"][0]["answer"] == "报告正文"


def test_creating_a_session_persists_it_immediately():

    from services.session_store import SessionStore

    single = SessionStore()

    session = single.create("fresh")

    assert session["session_id"] == "fresh"
    assert store.load_conversation("fresh") is not None


# ========================================
# 存储不可用时的降级
#
# 实测踩到：存储目录建不出来时，会话列表接口直接 500。
# 更要命的是 session_store.create() 会调 save_conversation，
# 所以存储出问题会把**分析本身**一起拖垮。
# 历史记录是便利功能，绝不该有这种后果。
# ========================================

def test_reads_return_empty_when_the_directory_is_unusable(tmp_path):
    """目录位置被一个同名文件占住，就建不出目录。"""

    blocked = tmp_path / "blocked"

    blocked.write_text("我不是目录", encoding="utf-8")

    store.set_store_dir(str(blocked / "conversations"))

    assert store.list_conversations() == []
    assert store.count_conversations() == 0
    assert store.load_conversation("s1") is None


def test_writes_are_dropped_instead_of_raising(tmp_path):

    blocked = tmp_path / "blocked"

    blocked.write_text("我不是目录", encoding="utf-8")

    store.set_store_dir(str(blocked / "conversations"))

    # 不能抛异常：session_store.create() 走的就是这条路
    store.save_conversation(make_session())

    assert store.count_conversations() == 0


def test_creating_a_session_survives_unusable_storage(tmp_path):
    """这条是关键：存储坏了，分析链路必须照常跑。"""

    from services.session_store import SessionStore

    blocked = tmp_path / "blocked"

    blocked.write_text("我不是目录", encoding="utf-8")

    store.set_store_dir(str(blocked / "conversations"))

    sessions = SessionStore()

    session = sessions.create("still-works")

    assert session["session_id"] == "still-works"
    assert sessions.get("still-works") is not None

    # 追问也要能记住
    sessions.remember("still-works", {}, "问题", "回答")

    assert sessions.get("still-works")["turn"] == 1


def test_rename_and_delete_report_failure_quietly(tmp_path):

    blocked = tmp_path / "blocked"

    blocked.write_text("我不是目录", encoding="utf-8")

    store.set_store_dir(str(blocked / "conversations"))

    assert store.rename_conversation("s1", "新名字") is False
    assert store.delete_conversation("s1") is False
    assert store.export_markdown("s1") is None
