"""
DataMind 测试套件 —— 共享夹具

三条设计原则：

1. **完全离线**

   `no_real_llm` 是 autouse 夹具，它把 OpenAI 客户端的 create 换成会抛异常的桩。
   任何漏网的真实调用都会**立刻失败**，而不是偷偷花钱。
   需要真机的用例请标 `@pytest.mark.live`。

2. **不碰项目目录**

   需要写文件的地方一律用 pytest 的 `tmp_path`；
   `isolated_temp` 夹具会把项目里的临时目录常量重定向过去。

3. **不依赖当前工作目录**

   所有夹具都基于项目根目录定位。
"""

import json
import os
import sys

import pytest

# ========================================
# 项目根目录
# ========================================

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

if not os.path.isdir(os.path.join(PROJECT, "agent")):
    PROJECT = os.path.dirname(PROJECT)

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

PROJECT = os.path.abspath(PROJECT)

# ========================================
# 测试环境
#
# 先设好占位值再导入项目模块：dotenv 默认不会覆盖已存在的环境变量，
# 所以测试不会读到 .env 里的真实 key。
# ========================================

os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.deepseek.com")
os.environ.setdefault("MODEL_NAME", "deepseek-flash")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

from services import llm_service  # noqa: E402


# ========================================
# 离线保险
# ========================================

@pytest.fixture(autouse=True)
def isolated_conversation_db(tmp_path):
    """
    把对话数据库指到临时目录。

    V4.14 之后会话会落库。如果没有这个夹具，
    任何碰到会话仓储的用例都会写进项目真实的 data/datamind.db ——
    测试不该污染仓库里的数据。
    """

    from services import store

    store.set_store_dir(str(tmp_path / "conversations"))

    yield

    store.set_store_dir(None)


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """
    拦截任何真实的模型调用。

    测试通过夹具替换 `call_llm` 来伪造模型；
    如果哪条路径忘了替换，就会打到这里并直接失败 ——
    这样「离线」是被强制的，不是靠约定。
    """

    def forbidden(*args, **kwargs):

        raise AssertionError(
            "测试中不允许真实调用模型。"
            "请用 stub_llm / tool_model 夹具伪造模型，"
            "真机用例请标 @pytest.mark.live。"
        )

    monkeypatch.setattr(
        llm_service.client.chat.completions,
        "create",
        forbidden,
    )


# ========================================
# 路径与数据
# ========================================

@pytest.fixture(scope="session")
def project_root():
    return PROJECT


@pytest.fixture(scope="session")
def demo_csv():
    """含退款/折扣/订单状态的小数据集，用于业务口径用例。"""
    path = os.path.join(PROJECT, "eval", "datasets", "demo_orders.csv")
    assert os.path.isfile(path), path
    return path


@pytest.fixture(scope="session")
def sales_csv():
    """40 行销售数据，含日期/地区/品类，跨度 244 天。"""
    path = os.path.join(PROJECT, "eval", "datasets", "sales_2026.csv")
    assert os.path.isfile(path), path
    return path


@pytest.fixture(scope="session")
def chart_csv():
    """绘图链路用的 8 行数据。"""
    path = os.path.join(PROJECT, "test_chart_data.csv")
    assert os.path.isfile(path), path
    return path


@pytest.fixture
def write_csv(tmp_path):
    """在 tmp_path 里写一个 CSV，返回路径。"""

    def _write(text, name="data.csv"):

        path = tmp_path / name

        path.write_text(text, encoding="utf-8")

        return str(path)

    return _write


# ========================================
# 模型桩
# ========================================

# 所有会调用模型的模块
LLM_MODULES = (
    "agent.nodes.planner",
    "agent.nodes.executor",
    "agent.nodes.error_handler",
    "agent.nodes.task_failure",
    "agent.nodes.reviewer",
    "agent.nodes.reporter",
    "agent.utils.chart_planner",
    "agent.utils.knowledge",
    "tools.loader",
)


class LLMStub:
    """
    可编排的文本模型桩。

    用法：

        stub = stub_llm
        stub.reply("中的 Planner", json.dumps({"plan": [...]}))
        stub.reply("专业的数据分析师", "## 报告")

        stub.calls      # 收到过的 prompt 列表
    """

    def __init__(self):

        self.calls = []
        self.prompts = []

        self._rules = []

        self.default = "{}"

    def reply(self, keyword, response):

        """按关键字匹配 prompt，返回对应回复。"""

        self._rules.append((keyword, response))

        return self

    def __call__(self, messages, temperature=0.2, **kwargs):

        prompt = ""

        if messages:

            last = messages[-1]

            content = last.get("content") if isinstance(last, dict) else None

            if isinstance(content, str):

                prompt = content

        self.calls.append(prompt)

        self.prompts.append(list(messages))

        for keyword, response in self._rules:

            if keyword in prompt:

                return response

        return self.default


@pytest.fixture
def stub_llm(monkeypatch):
    """替换所有节点的 call_llm。"""

    import importlib

    stub = LLMStub()

    for name in LLM_MODULES:

        try:

            module = importlib.import_module(name)

        except ImportError:

            continue

        if hasattr(module, "call_llm"):

            monkeypatch.setattr(module, "call_llm", stub)

    return stub


class FakeFunction:

    def __init__(self, name, arguments):

        self.name = name

        self.arguments = (
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False)
        )


class FakeToolCall:

    def __init__(self, call_id, name, arguments):

        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    """
    伪造一次模型回复。

    reasoning_content 是必须的：
    当前模型跑在思考模式下，回填历史时不带上它会返回 400。
    """

    def __init__(self, content="", tool_calls=None, reasoning="思考中"):

        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning
        self.model_extra = {"reasoning_content": reasoning}


@pytest.fixture
def fake_tool_call():
    return FakeToolCall


@pytest.fixture
def fake_message():
    return FakeMessage


class ToolModelStub:
    """
    工具调用循环的模型桩：按脚本依次返回消息。
    """

    def __init__(self):

        self.script = []
        self.calls = []

    def push(self, message):

        self.script.append(message)

        return self

    def push_tool(self, *calls, content=""):

        self.script.append(FakeMessage(content=content, tool_calls=list(calls)))

        return self

    def push_text(self, content):

        self.script.append(FakeMessage(content=content))

        return self

    def __call__(self, messages, tools, temperature=0.2, tool_choice="auto"):

        self.calls.append({
            "messages": [dict(item) for item in messages],
            "tools": [item["function"]["name"] for item in tools],
            "tool_choice": tool_choice,
        })

        if self.script:

            return self.script.pop(0)

        return FakeMessage(content="（脚本用完了）")


@pytest.fixture
def tool_model(monkeypatch):
    """替换 Executor 的 call_llm_with_tools。"""

    import agent.nodes.executor as executor

    stub = ToolModelStub()

    monkeypatch.setattr(executor, "call_llm_with_tools", stub)

    return stub


# ========================================
# 临时目录隔离
# ========================================

@pytest.fixture
def isolated_temp(monkeypatch, tmp_path):
    """
    把项目的临时目录常量重定向到 tmp_path。

    这样测试不会往项目目录里写任何东西。
    """

    import main
    import agent.utils.chart_executor as chart_executor

    temp_dir = tmp_path / "temp"
    chart_dir = tmp_path / "temp_charts"

    temp_dir.mkdir(exist_ok=True)
    chart_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(main, "TEMP_FOLDER", str(temp_dir), raising=False)
    monkeypatch.setattr(main, "CHART_FOLDER", str(chart_dir), raising=False)
    monkeypatch.setattr(
        chart_executor, "CHART_OUTPUT_DIR", str(chart_dir), raising=False
    )

    return {"temp": str(temp_dir), "charts": str(chart_dir)}


# ========================================
# 小助手
# ========================================

@pytest.fixture
def trace_events():
    """从状态里取出 trace 事件名列表。"""

    def _events(state):

        return [
            event.get("event")
            for event in (state.get("agent_trace") or [])
        ]

    return _events


@pytest.fixture
def trace_titles():
    def _titles(state):

        return [
            (event.get("display") or {}).get("title")
            for event in (state.get("agent_trace") or [])
        ]

    return _titles


def base_state(**overrides):
    """
    一个可以直接喂给节点的初始状态。
    """

    state = {
        "question": "销售额合计是多少",
        "file_path": None,
        "dataset_info": "=== 数据集字段 ===\n['地区','销售额']",
        "plan": [],
        "current_step": 0,
        "execution_results": [],
        "execution_status": "idle",
        "pending_code": None,
        "last_execution_error": None,
        "current_task_retry_count": 0,
        "max_task_retries": 2,
        "code_history": [],
        "agent_trace": [],
        "error": None,
        "review_result": None,
        "retry_count": 0,
        "chart_plan": None,
        "chart_results": [],
        "final_report": None,
        "input_kind": "table",
        "input_context": None,
        "source_type": None,
        "dataset_notes": [],
        "history": [],
        "is_followup": False,
        "session_id": None,
        "source_name": None,
        "verification": None,
        "knowledge_context": None,
        "knowledge_hits": [],
    }

    state.update(overrides)

    return state


@pytest.fixture
def make_state():
    return base_state
