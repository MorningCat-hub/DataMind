"""会话记忆与 /chat/stream 流式输出（pytest 版）

这是 _scratch/v42_verify.py（自定义 check() 收集器 + sys.exit 的独立脚本）
的等价移植，测试意图一一保留：

    A. services/session_store.py：会话 / 数据集记忆 / 容量 / TTL
    B. agent.utils.history.format_history：历史压缩
    C. 单轮 /chat/stream：SSE 帧类型与顺序
    D. 多轮追问：沿用数据集、is_followup、历史进 Planner 的 prompt
    E. 换文件：会话重置（start 帧 reset=true）、轮次回到 1
    F. 错误帧：没有文件 / 空问题 / 不支持的格式
    G. /analyze 回归

与源脚本的差异（都是有意的）：

1. assert 取代 check() / sys.exit()，每个 check 都有对应断言。
2. 模型调用一律用 conftest 的 stub_llm / tool_model 伪造，
   不发任何真实请求（no_real_llm 兜底）。
3. 上传文件、归一化 CSV、生成的图表全部落在 pytest 的 tmp_path，
   不往项目目录写任何东西。
4. TestClient 直接当普通对象使用 —— 按设计**不会**触发 lifespan，
   所以启动时的临时文件清理不会跑（源脚本依赖的正是这一点）。
5. 源脚本里两条永远为真的断言（`run2["total"] > 0`、
   `"\n" not in ... or True`）换成了真正能失败的断言，见对应注释。
6. 源脚本注入 1.5s 停顿来证明「帧是边跑边推的」，但同时也注明
   httpx 的 ASGITransport 会把整个 body 缓冲起来、TestClient 里证明不了；
   这里保留「请求确实被这段停顿拖慢了 + 确实走了伪造的 Planner」这两条，
   但把停顿缩短到 0.5s，免得整套用例白白多等一秒多。
"""

import json
import os
import sys
import time

import pytest


# ============================================================
# 项目根目录：不依赖当前工作目录
#
# 这个文件最终会住在 <项目>/tests/ 下，所以先按「上一级就是项目根」推导；
# 现在它还在工作区的 _scratch/batch2/ 里，够不着项目，
# 于是允许用 DATAMIND_PROJECT 指定，再回落到已知位置。
# ============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if not os.path.isdir(os.path.join(PROJECT, "agent")):
    PROJECT = os.path.dirname(PROJECT)


def _looks_like_project(path):
    return os.path.isdir(os.path.join(path, "agent"))


if not _looks_like_project(PROJECT):
    PROJECT = os.path.abspath(
        os.environ.get("DATAMIND_PROJECT") or PROJECT
    )

if not _looks_like_project(PROJECT):
    _KNOWN_PROJECT = r"D:\pycharm\PycharmProject\datamind-v4.0"
    if _looks_like_project(_KNOWN_PROJECT):
        PROJECT = _KNOWN_PROJECT

if not _looks_like_project(PROJECT):
    raise RuntimeError(
        "找不到项目根目录（%s 下没有 agent/）。"
        "请用环境变量 DATAMIND_PROJECT 指向 datamind-v4.0。" % PROJECT
    )

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)


# 占位 key：import main 会 load_dotenv()，这里保证导入过程不依赖真实 key。
# conftest 里已经 setdefault 过一次，这里是双保险（setdefault 不会覆盖真值）。
os.environ.setdefault("OPENAI_API_KEY", "sk-verify-dummy")


from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from agent.utils.history import format_history  # noqa: E402
from services.session_store import SessionStore, has_dataset  # noqa: E402


# ============================================================
# 伪造的模型回复（等价于源脚本里的 fake_llm）
# ============================================================

PLAN_JSON = json.dumps(
    {
        "plan": [
            {"step": 1, "task": "汇总销售额", "tool": "run_stat_calc"},
        ]
    },
    ensure_ascii=False,
)

REVIEW_JSON = json.dumps(
    {"passed": True, "reason": "ok", "missing_tasks": []},
    ensure_ascii=False,
)

CHART_PLAN_JSON = json.dumps(
    {
        "need_chart": True,
        "charts": [
            {
                "chart_type": "bar",
                "title": "各地区销售额",
                "reason": "对比",
                "x_field": "地区",
                "y_field": "销售额",
                "aggregation": "sum",
                "priority": 1,
            }
        ],
        "reason": "需要柱状图",
    },
    ensure_ascii=False,
)

REPORT_MARKDOWN = "# 分析报告\n\n华东与华南销售额已完成汇总。"

# Executor 现在走原生 Tool Calling：这段代码由工具桩直接给出，
# 等价于源脚本里「让模型生成代码」那一步的结果。
EXECUTOR_CODE = "result = df['销售额'].sum()"

# 源脚本注入 1.5s；这里缩短，理由见模块 docstring。
STALL_SECONDS = 0.5

SALES_CSV = "地区,销售额\n华东,100\n华南,200\n"
PROFIT_CSV = "品类,利润\n电子,10\n家具,20\n"


# ============================================================
# 夹具：隔离
# ============================================================

@pytest.fixture
def fresh_sessions(monkeypatch):
    """
    每个用例一套全新的会话存储。

    main 里的 session_store 是全局单例，用例之间会互相污染，
    所以直接换掉 main.session_store（接口内部都走这个名字）。
    """

    store = SessionStore()

    monkeypatch.setattr(main, "session_store", store)

    return store


@pytest.fixture
def ingest_into_tmp(monkeypatch, isolated_temp):
    """
    让 dataset 节点把归一化 CSV 也写进 tmp_path。

    isolated_temp 只管 main.TEMP_FOLDER（上传文件落点）；
    归一化 CSV 走的是 tools.loader.ingest() 的 output_dir 默认参数，
    那是**定义时求值**的，改 tools.loader.NORMALIZED_DIR 没用，
    只能换成显式传参的包装，否则会往项目的 temp/ 里写文件。
    """

    import agent.nodes.dataset as dataset
    import tools.loader as loader

    def ingest(file_path, output_dir=None):

        return loader.ingest(
            file_path,
            output_dir or isolated_temp["temp"],
        )

    monkeypatch.setattr(dataset, "ingest", ingest)

    return ingest


@pytest.fixture
def chart_mount(monkeypatch, isolated_temp):
    """
    把 /charts 静态挂载也指向隔离目录。

    isolated_temp 改了 chart_executor.CHART_OUTPUT_DIR，
    但 /charts 的 StaticFiles 是 import main 时按当时的 CHART_FOLDER
    建好的，改常量不会跟着走，于是「图表 URL 能取到 PNG」会 404。
    """

    directory = isolated_temp["charts"]

    mounted = []

    for route in main.app.routes:

        if getattr(route, "path", None) != "/charts":
            continue

        static_app = getattr(route, "app", None)

        if static_app is None:
            continue

        monkeypatch.setattr(
            static_app,
            "all_directories",
            [directory],
            raising=False,
        )

        monkeypatch.setattr(
            static_app,
            "directory",
            directory,
            raising=False,
        )

        mounted.append(static_app)

    assert mounted, "main.app 里找不到 /charts 静态挂载"

    return directory


@pytest.fixture
def client(isolated_temp, ingest_into_tmp, chart_mount, fresh_sessions):
    """
    TestClient。

    注意：TestClient(main.app) 直接当普通对象用**不会**触发 lifespan，
    启动时的临时文件清理因此不会跑 —— 这是有意的设计，测试保持这样。
    """

    return TestClient(main.app)


# ============================================================
# 夹具：模型桩
# ============================================================

@pytest.fixture
def planner_stub(stub_llm):
    """
    按关键字伪造各节点的文本模型（等价于源脚本的 fake_llm）。

    规则顺序有意义：Planner 的 prompt 里同时出现了
    「中的 Planner」和「Chart Planner」，
    必须让 Planner 那条先命中，否则计划会被当成图表计划解析。
    """

    stub_llm.reply("中的 Planner", PLAN_JSON)

    # 下面两条是源脚本里生成代码用的关键字，
    # 当前链路不再经过它们，留着是为了万一走到 error_handler 自愈时也有合法回复。
    stub_llm.reply("数据分析工程师", EXECUTOR_CODE)
    stub_llm.reply("代码修复专家", EXECUTOR_CODE)

    stub_llm.reply("中的 Reviewer", REVIEW_JSON)
    stub_llm.reply("Chart Planner", CHART_PLAN_JSON)
    stub_llm.reply("专业的数据分析师", REPORT_MARKDOWN)

    stub_llm.default = "{}"

    return stub_llm


class _StallablePlanner:
    """给 Planner 的模型桩加一段可控停顿（用于计时断言）。"""

    def __init__(self, stub):

        self._stub = stub
        self.delay = 0.0

    def __call__(self, messages, temperature=0.2, **kwargs):

        if self.delay > 0:
            time.sleep(self.delay)

        return self._stub(messages, temperature=temperature, **kwargs)


@pytest.fixture
def stalled_planner(monkeypatch, planner_stub):
    """只包住 Planner：其它节点的桩保持原样。"""

    import agent.nodes.planner as planner

    stall = _StallablePlanner(planner_stub)

    monkeypatch.setattr(planner, "call_llm", stall)

    return stall


def script_tool_rounds(tool_model, fake_tool_call, rounds=1):
    """
    给工具桩排好一次任务需要的两步。

    Executor 每个任务两步：先调 run_python（拿到数据），
    再收尾输出文字。计划里只有一个任务，所以默认排一轮。
    """

    for index in range(rounds):

        tool_model.push_tool(
            fake_tool_call(
                f"call_{index + 1}",
                "run_python",
                {"code": EXECUTOR_CODE},
            )
        )

        tool_model.push_text("已完成计算。")

    return tool_model


# ============================================================
# 小助手：跑一次 /chat/stream
# ============================================================

def collect_frames(client, question, session_id="", upload=None):
    """
    请求 /chat/stream，收集所有 SSE 帧。

    upload 是 (路径, 上传时的文件名[, content-type])。
    """

    files = {}
    handle = None

    if upload is not None:

        if len(upload) == 3:
            path, name, media_type = upload
        else:
            path, name = upload
            media_type = "text/csv"

        handle = open(path, "rb")

        files = {"file": (name, handle, media_type)}

    frames = []
    content_type = ""

    started = time.monotonic()

    try:

        with client.stream(
            "POST",
            "/chat/stream",
            files=files,
            data={"question": question, "session_id": session_id},
        ) as response:

            content_type = response.headers.get("content-type", "")

            for line in response.iter_lines():

                if not line or not line.startswith("data: "):
                    continue

                frames.append(json.loads(line[6:]))

    finally:

        elapsed = time.monotonic() - started

        if handle is not None:
            handle.close()

    return {
        "frames": frames,
        "kinds": [frame.get("type") for frame in frames],
        "content_type": content_type,
        "elapsed": elapsed,
    }


def frames_of(run, kind):
    """取出某一类帧。"""

    return [frame for frame in run["frames"] if frame.get("type") == kind]


def frame_of(run, kind):
    """取出唯一的那一帧（没有就失败，报出实际帧序列）。"""

    matched = frames_of(run, kind)

    assert matched, f"没有 {kind} 帧，实际帧序列：{run['kinds']}"

    return matched[0]


# ============================================================
# 夹具：三轮对话
# ============================================================

@pytest.fixture
def first_turn(client, planner_stub, stalled_planner, tool_model,
               fake_tool_call, write_csv):
    """第一轮：带文件上传，并在 Planner 里注入一段停顿。"""

    csv_path = write_csv(SALES_CSV, name="sales.csv")

    script_tool_rounds(tool_model, fake_tool_call, rounds=1)

    stalled_planner.delay = STALL_SECONDS

    try:

        run = collect_frames(
            client,
            "各地区的销售额是多少",
            upload=(csv_path, "sales.csv"),
        )

    finally:

        stalled_planner.delay = 0.0

    run["csv_path"] = csv_path
    run["stall"] = STALL_SECONDS
    run["session_id"] = frame_of(run, "done").get("session_id")
    run["planner_prompts"] = [
        prompt
        for prompt in planner_stub.calls
        if "中的 Planner" in prompt
    ]

    return run


@pytest.fixture
def second_turn(client, first_turn, planner_stub, tool_model, fake_tool_call):
    """第二轮：不传文件，沿用第一轮的数据集。"""

    script_tool_rounds(tool_model, fake_tool_call, rounds=1)

    before = len(planner_stub.calls)

    run = collect_frames(
        client,
        "只看华东地区",
        session_id=first_turn["session_id"],
    )

    run["planner_prompts"] = [
        prompt
        for prompt in planner_stub.calls[before:]
        if "中的 Planner" in prompt
    ]

    run["session_id"] = frame_of(run, "done").get("session_id")

    return run


@pytest.fixture
def third_turn(client, second_turn, tool_model, fake_tool_call, write_csv):
    """第三轮：换一个文件，服务端应该重置这个会话。"""

    csv_path = write_csv(PROFIT_CSV, name="sales2.csv")

    script_tool_rounds(tool_model, fake_tool_call, rounds=1)

    run = collect_frames(
        client,
        "分析利润",
        session_id=second_turn["session_id"],
        upload=(csv_path, "sales2.csv"),
    )

    run["csv_path"] = csv_path
    run["previous_session_id"] = second_turn["session_id"]

    return run


# ============================================================
# A. 会话存储
# ============================================================

def test_create_returns_an_empty_session():

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    session = store.create()

    assert session["session_id"], session
    assert has_dataset(session) is False, session
    assert session["turn"] == 0, session


def test_remember_stores_dataset_turn_history_and_notes():

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    session = store.create()

    store.remember(
        session["session_id"],
        {
            "input_kind": "table",
            "file_path": "temp/data_x.csv",
            "dataset_info": "info",
            "dataset_notes": ["note"],
        },
        "问题一",
        "回答一",
    )

    saved = store.get(session["session_id"])

    assert has_dataset(saved) is True, saved
    assert saved["turn"] == 1, saved
    assert saved["history"] == [
        {"question": "问题一", "answer": "回答一"}
    ], saved["history"]
    assert saved["dataset_notes"] == ["note"], saved["dataset_notes"]


def test_remember_accumulates_history():

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    session = store.create()

    store.remember(
        session["session_id"],
        {"input_kind": "table", "file_path": "temp/data_x.csv"},
        "问题一",
        "回答一",
    )
    store.remember(
        session["session_id"],
        {"input_kind": "table"},
        "问题二",
        "回答二",
    )

    saved = store.get(session["session_id"])

    assert len(saved["history"]) == 2, saved["history"]
    assert saved["turn"] == 2, saved


def test_reset_dataset_clears_dataset_history_and_turn():

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    session = store.create()

    store.remember(
        session["session_id"],
        {
            "input_kind": "table",
            "file_path": "temp/data_x.csv",
            "dataset_info": "info",
            "dataset_notes": ["note"],
        },
        "问题一",
        "回答一",
    )

    store.reset_dataset(session["session_id"])

    fresh = store.get(session["session_id"])

    assert has_dataset(fresh) is False, fresh
    assert fresh["history"] == [], fresh["history"]
    assert fresh["turn"] == 0, fresh
    assert fresh["dataset_notes"] == [], fresh["dataset_notes"]


def test_session_cap_evicts_the_oldest_session():
    """源脚本：容量上限 3，再 create 6 次，会话数不能超。"""

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    oldest = store.create()["session_id"]

    for _ in range(6):
        store.create()

    assert store.count() <= 3, store.count()
    assert store.count() == 3, store.count()

    # 上限是靠「丢掉最久未活动的」实现的。
    #
    # 注意淘汰的是**内存缓存**：数据库是持久的，
    # 所以这一步之后 older 仍然能从磁盘读回来 —— 这是有意的。
    assert oldest not in store._sessions, "最久未活动的会话应该被移出内存"
    assert store.get(oldest) is not None, "但它仍然能从数据库恢复"
    assert store.count() == 3, "恢复不该把自己算进上限"


def test_unknown_and_empty_session_ids_return_none():

    store = SessionStore(max_sessions=3, ttl_seconds=3600)

    assert store.get("nope") is None
    assert store.get("") is None


def test_expired_sessions_are_purged_on_create():
    """
    TTL 回收。

    回收只在 create() 里顺带做（get() 不清理），
    所以这里直接把 last_active 拨回两小时，等价于「闲置超过 TTL」，
    不用真的 sleep。
    """

    store = SessionStore(max_sessions=10, ttl_seconds=3600)

    stale = store.create()["session_id"]

    store._sessions[stale]["last_active"] -= 7200

    fresh = store.create()["session_id"]

    assert stale not in store._sessions, "超过 TTL 的会话应该被移出内存"
    assert store.get(fresh) is not None
    assert store.count() == 1, store.count()

    # 内存回收了，但历史还在磁盘上
    assert store.get(stale) is not None, "数据库里的历史不该被 TTL 删掉"


# ============================================================
# B. 历史压缩
# ============================================================

def test_empty_history_gets_a_placeholder():

    assert "第一轮" in format_history([])


def test_long_answers_are_truncated():

    text = format_history([
        {"question": "各季度销售额", "answer": "A" * 900},
    ])

    assert "各季度销售额" in text, text
    assert len(text) < 600, len(text)
    assert "…" in text, "超长回答应该被截断而不是整段塞进 prompt"


def test_answer_newlines_are_flattened():
    """
    源脚本里这条是 `"\\n" not in ... or True`（永远为真）。

    这里换成真正能失败的断言：回答里的换行会被压成空格，
    免得多行报告把 prompt 的结构撑坏。
    """

    text = format_history([
        {"question": "问题", "answer": "第一行\n第二行"},
    ])

    assert "第一行 第二行" in text, text
    assert "第一行\n第二行" not in text, text


def test_only_the_last_few_turns_are_kept():

    history = [
        {"question": f"q{index}", "answer": f"a{index}"}
        for index in range(10)
    ]

    text = format_history(history)

    assert "q0" not in text, text
    assert "q9" in text, text


# ============================================================
# C. 单轮 SSE
# ============================================================

def test_first_turn_frame_order_and_types(first_turn):
    """
    帧类型与顺序，外加「这一轮确实跑了伪造的模型」。

    源脚本用 1.5s 停顿来证明「帧是边跑边推的」，
    同时注明 httpx 的 ASGITransport 会把整个 body 缓冲起来、
    TestClient 里证明不了推进时机；能证明的是
    「请求确实被这段停顿拖慢了，而且确实走到了伪造的 Planner」。
    """

    kinds = first_turn["kinds"]

    assert first_turn["content_type"].startswith("text/event-stream"), (
        first_turn["content_type"]
    )

    assert kinds[0] == "start", kinds[:3]
    assert kinds[-1] == "done", kinds[-3:]

    # 一帧 start 就够：前端「开新一轮」的状态只应该被触发一次
    assert kinds.count("start") == 1, kinds

    assert kinds.count("trace") >= 8, kinds.count("trace")

    for kind in ("report", "charts", "meta"):
        assert kind in kinds, sorted(set(kinds))

    last_trace = max(
        index for index, kind in enumerate(kinds) if kind == "trace"
    )
    first_report = min(
        index for index, kind in enumerate(kinds) if kind == "report"
    )

    assert last_trace < first_report, kinds

    # 走的是伪造的 Planner，而不是被缓存或短路
    assert any(
        "中的 Planner" in prompt
        for prompt in first_turn["planner_prompts"]
    ), first_turn["planner_prompts"][:2]

    assert first_turn["elapsed"] >= first_turn["stall"], (
        f"整轮耗时 {first_turn['elapsed']:.2f}s，"
        f"没算进注入的 {first_turn['stall']}s 停顿"
    )


def test_first_turn_report_chart_and_meta_payloads(client, first_turn):

    report = frame_of(first_turn, "report")

    assert "分析报告" in report["content"], report

    charts = frame_of(first_turn, "charts")["charts"]

    assert len(charts) == 1, charts

    chart_url = charts[0].get("chart_url", "")

    assert chart_url.startswith("/charts/"), chart_url

    image = client.get(chart_url)

    assert image.status_code == 200, chart_url
    assert image.headers.get("content-type", "").startswith("image/"), (
        image.headers.get("content-type")
    )
    assert image.content, "图表 URL 返回了空内容"

    meta = frame_of(first_turn, "meta")

    assert meta["source_type"] == "csv", meta

    done = frame_of(first_turn, "done")

    assert done["session_id"], done
    assert done["turn"] == 1, done

    titles = [
        frame["event"].get("title")
        for frame in frames_of(first_turn, "trace")
    ]

    assert any("读取" in str(title) for title in titles), titles[:4]


def test_first_start_frame_does_not_detach_the_in_flight_reply(first_turn):
    """
    历史回归（v4.2.1）：界面「完全没有回复」。

    第一轮的 start 帧带 reset=true（上传了文件），前端曾经据此清空对话区，
    而本轮的用户消息和助手气泡已经建好了 —— 清空把正在输出的气泡
    从文档上摘掉，之后所有流式帧都挂到孤儿节点上，
    用户看到的就是「完全没有回复，也看不到流式」。

    服务端能锁住的部分：
    reset=true 的 start 帧只出现一次、在最前面，
    而且它之后这一轮的所有帧都还在（流没有断在 start 上）。
    另一侧（前端不许清空）见 test_frontend_start_handler_*。
    """

    kinds = first_turn["kinds"]
    start = frame_of(first_turn, "start")

    assert start["reset"] is True, start
    assert kinds.count("start") == 1, kinds
    assert kinds.index("start") == 0, kinds

    # start 之后必须走完这一轮：帧不能断在 start 上
    for kind in ("trace", "report", "charts", "meta", "done"):
        assert kind in kinds, (kind, kinds)

    assert kinds[-1] == "done", kinds[-3:]


def test_frontend_start_handler_never_clears_the_message_list(project_root):
    """
    同一回归的前端一侧：case "start" 里不允许再出现清空动作。

    这条只能静态检查（pytest 里没有 DOM），做法与
    tests/test_temp_cleanup.py 一致：用源码文本锁住「不许再犯」的写法。
    """

    app_js = os.path.join(project_root, "frontend", "app.js")

    with open(app_js, encoding="utf-8") as handle:
        source = handle.read()

    assert 'case "start":' in source, app_js

    block = source.split('case "start":', 1)[1].split("break;", 1)[0]

    # 注释里提到这段历史是允许的，所以先去掉注释行
    code = "\n".join(
        line
        for line in block.splitlines()
        if not line.strip().startswith("//")
    )

    assert "innerHTML" not in code, code
    assert "removeChild" not in code, code
    assert "messageList" not in code, code


# ============================================================
# D. 多轮追问
# ============================================================

def test_followup_state_marks_is_followup_and_carries_history():
    """
    追问的 State 语义：is_followup 为真，
    数据集 / 来源 / 提示 / 历史都从会话里带过来；传了新文件就不是追问。
    """

    store = SessionStore()

    session = store.create()

    store.remember(
        session["session_id"],
        {
            "input_kind": "table",
            "file_path": "temp/data_x.csv",
            "dataset_info": "=== 数据集字段 ===",
            "dataset_notes": ["note"],
            "source_type": "csv",
        },
        "各地区的销售额是多少",
        "华东与华南销售额已完成汇总。",
    )

    saved = store.get(session["session_id"])

    state = main.build_initial_state("只看华东地区", saved, None)

    assert state["is_followup"] is True, state
    assert state["file_path"] == "temp/data_x.csv", state["file_path"]
    assert state["dataset_info"] == "=== 数据集字段 ===", state["dataset_info"]
    assert state["source_type"] == "csv", state["source_type"]
    assert state["dataset_notes"] == ["note"], state["dataset_notes"]
    assert state["history"] == [
        {
            "question": "各地区的销售额是多少",
            "answer": "华东与华南销售额已完成汇总。",
        }
    ], state["history"]

    # 带了新文件 = 新的分析上下文，不是追问
    replaced = main.build_initial_state(
        "分析利润",
        saved,
        "temp/data_new.csv",
    )

    assert replaced["is_followup"] is False, replaced
    assert replaced["file_path"] == "temp/data_new.csv", replaced["file_path"]


def test_followup_reuses_dataset_and_reaches_the_planner(first_turn, second_turn):
    """
    第二轮：不再上传数据集、沿用上一轮、历史进 Planner 的 prompt。

    源脚本这里有一条 `run2["total"] > 0`（永远为真，等于没测），
    换成真正能失败的断言：没有错误帧、start 帧 reset=false、
    trace 里出现「沿用上一轮」。
    """

    kinds = second_turn["kinds"]

    assert kinds[0] == "start" and kinds[-1] == "done", kinds[:3]
    assert "error" not in kinds, kinds

    start = frame_of(second_turn, "start")

    assert start["reset"] is False, "追问不应该重置会话"
    assert start["session_id"] == first_turn["session_id"], start

    done = frame_of(second_turn, "done")

    assert done["turn"] == 2, done

    titles = [
        str(frame["event"].get("title"))
        for frame in frames_of(second_turn, "trace")
    ]

    assert any("沿用上一轮" in title for title in titles), titles[:6]

    prompts = second_turn["planner_prompts"]

    assert prompts, "追问这一轮没有走到 Planner"

    prompt = prompts[-1]

    assert "之前的对话" in prompt, prompt[-400:]
    assert "各地区的销售额是多少" in prompt, "上一轮的问题没有进 prompt"
    assert "华东与华南销售额" in prompt, "上一轮回答的摘要没有进 prompt"
    assert "不要重复已经完成的分析" in prompt

    reports = frames_of(second_turn, "report")

    assert reports and reports[0]["content"], reports


# ============================================================
# E. 换文件重置
# ============================================================

def test_uploading_a_new_file_resets_the_session(first_turn, second_turn, third_turn):
    """
    第三轮换了文件：会话被重置、轮次回到 1、session_id 保持不变。

    注意 start 帧里的 turn 字段是「重置前的轮次 + 1」，
    与 done 帧的 turn 不一致（见交付说明里的疑点），所以这里不锁它。
    """

    start = frame_of(third_turn, "start")
    done = frame_of(third_turn, "done")

    assert start["reset"] is True, start
    assert start["session_id"] == first_turn["session_id"], start
    assert start["session_id"] == second_turn["session_id"], start

    assert done["turn"] == 1, done
    assert "error" not in third_turn["kinds"], third_turn["kinds"]

    # 数据集真被换了：重新读取文件，而不是沿用上一轮
    titles = [
        str(frame["event"].get("title"))
        for frame in frames_of(third_turn, "trace")
    ]

    assert not any("沿用上一轮" in title for title in titles), titles[:6]
    assert any("读取" in title for title in titles), titles[:6]


# ============================================================
# F. 错误帧
# ============================================================

def test_missing_file_and_session_returns_a_single_error_frame(client):

    run = collect_frames(client, "你好")

    assert run["kinds"] == ["error"], run["kinds"]

    assert run["content_type"].startswith("text/event-stream"), (
        run["content_type"]
    )


def test_empty_question_returns_an_error_frame(client, fresh_sessions):
    """
    空问题必须立刻回错误帧，而不是静默关闭流。

    这里手工造成一个「有数据集的有效会话」，
    不用先跑一整轮模型（源脚本是复用第一轮的 session_id）。
    """

    session = fresh_sessions.create()

    fresh_sessions.remember(
        session["session_id"],
        {
            "input_kind": "table",
            "file_path": "temp/data_x.csv",
            "dataset_info": "info",
        },
        "问题一",
        "回答一",
    )

    run = collect_frames(
        client,
        "",
        session_id=session["session_id"],
    )

    assert run["frames"], "空问题也要给一帧错误，不能没有任何帧"
    assert run["frames"][0]["type"] == "error", run["frames"][:1]
    assert "请输入" in run["frames"][0].get("message", ""), run["frames"][:1]


def test_unsupported_file_returns_an_error_frame_with_a_hint(client, tmp_path):

    pdf = tmp_path / "x.pdf"

    pdf.write_bytes(b"%PDF-1.4")

    run = collect_frames(
        client,
        "分析",
        upload=(str(pdf), "x.pdf", "application/pdf"),
    )

    assert run["frames"], "不支持的文件也要给一帧错误"

    first = run["frames"][0]

    assert first["type"] == "error", first
    assert "支持的格式" in first.get("message", ""), first


# ============================================================
# G. /analyze 回归
# ============================================================

def test_analyze_still_returns_the_legacy_payload(
    client,
    planner_stub,
    tool_model,
    fake_tool_call,
    write_csv,
):

    csv_path = write_csv(SALES_CSV, name="analyze_sales.csv")

    script_tool_rounds(tool_model, fake_tool_call, rounds=1)

    with open(csv_path, "rb") as handle:

        response = client.post(
            "/analyze",
            files={"file": ("sales.csv", handle, "text/csv")},
            data={"question": "各地区销售额"},
        )

    assert response.status_code == 200, response.text

    body = response.json()

    assert body.get("final_analysis"), body
    assert isinstance(body.get("chart_results"), list), body.get("chart_results")
    assert isinstance(body.get("agent_trace_view"), list), body.get("agent_trace_view")
    assert body.get("source_type") == "csv", body.get("source_type")
