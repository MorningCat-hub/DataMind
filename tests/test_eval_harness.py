"""DataMind V4.7「评测体系自身」的 pytest 测试。

这是 _scratch/v47_verify.py（一个自定义 check() 收集器 + sys.exit 的独立脚本）
的等价移植，测试意图一一保留：

    A. 用例文件能用，坏用例文件会被拒
    B. 答案键用 pandas 独立复算，必须和 cases.yaml 里写的一致
    C. judge() 对每种期望类型（数值 / 工具 / 口径 / 图表数 / 执行状态 /
       报告措辞 / 报告数值 / 异常）的通过与不通过两条路
    D. summarize() 的各项指标，compare_with() 的基线对比
    E. 以子进程跑 `python eval/run_eval.py --check`：退出码、输出内容、
       不调用模型、不落盘
    F. 第一次真机评测发现的修复：verify 节点的「提请关注」不短路判定、
       verification 可显式要求、校验器的「倍数 / 百分比反推」不再误报

全部离线，不发任何模型请求（--check 本身也不调用模型）。

项目根目录：

    和源脚本一样，优先按「这个文件将来会住在 <项目>/tests/ 下」来推导，
    推导不出来再用环境变量 DATAMIND_PROJECT，最后回落到已知位置。

可选：

    DATAMIND_ORIGINAL_SALES 指向「还带着订单状态列的原始 sales CSV」时，
    会额外跑源脚本里那两条 fixture 溯源断言；没有提供就 skip ——
    仓库里并不存在这份原始文件（源脚本是把它当第二个命令行参数传的）。
"""

import importlib.util
import os
import subprocess
import sys

import pandas as pd
import pytest


# ============================================================
# 路径：不依赖当前工作目录
# ============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)          # tests/ 的上一级（未来它会放在项目的 tests/ 下）
if not os.path.isdir(os.path.join(PROJECT, "eval")):
    PROJECT = os.path.dirname(PROJECT)   # 兜底

# 这个文件当前住在工作区的 _scratch/batch1/ 下，上面两层兜底还够不着项目，
# 所以再加两条：环境变量指定，以及一个已知位置的缺省值。
DEFAULT_PROJECT = r"D:\pycharm\PycharmProject\datamind-v4.0"


def _looks_like_project(path):
    return os.path.isfile(os.path.join(path, "eval", "run_eval.py"))


if not _looks_like_project(PROJECT):
    PROJECT = os.path.abspath(
        os.environ.get("DATAMIND_PROJECT") or DEFAULT_PROJECT
    )

if not _looks_like_project(PROJECT):
    raise RuntimeError(
        "找不到项目根目录（在 %s 下没有 eval/run_eval.py）。"
        "请设置环境变量 DATAMIND_PROJECT 指向 datamind-v4.0。" % PROJECT
    )

EVAL = os.path.join(PROJECT, "eval")
DATASETS = os.path.join(EVAL, "datasets")
RUNNER = os.path.join(EVAL, "run_eval.py")

# 要 import agent.*，项目根必须在 sys.path 上
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# 占位 key：import run_eval 会 load_dotenv() 并 import agent 模块，
# 但不会发请求。这里只是保证导进过程不会因为缺 key 而炸。
os.environ.setdefault("OPENAI_API_KEY", "sk-verify-dummy")


# ============================================================
# 加载被测对象
# ============================================================

def _load_runner():
    """按路径加载 eval/run_eval.py（它不是包）。"""

    spec = importlib.util.spec_from_file_location("run_eval", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = module
    spec.loader.exec_module(module)
    return module


# run_eval 在 import 时会 os.chdir(PROJECT)。
# 它自己的所有路径常量都是基于 __file__ 的绝对路径，
# 所以这里把工作目录还回去，免得污染整个 pytest 会话。
_CWD = os.getcwd()
try:
    run_eval = _load_runner()
finally:
    os.chdir(_CWD)

# 注意：run_eval 在 import 时执行 load_dotenv(..., override=True)，
# 会把上面 setdefault 的占位值覆盖成项目 .env 里的真实 key。
# 这些测试本来就不发请求，但为了让「万一有人不小心真的调了模型」
# 变成立刻失败、而不是真的花钱，这里把 key 明确钉回占位值。
os.environ["OPENAI_API_KEY"] = "sk-verify-dummy"

# _numbers_in 用来模拟真实 runner：报告里的数值是从报告正文抽出来的
from agent.utils.verifier import _numbers_in  # noqa: E402
from agent.utils.verifier import verify_report_numbers  # noqa: E402

CASES = run_eval.load_cases()

NUMERIC_CASE_IDS = [
    case["id"]
    for case in CASES
    if (case.get("expect") or {}).get("numbers")
]

ORIGINAL_SALES = os.environ.get("DATAMIND_ORIGINAL_SALES") or ""


# ============================================================
# A. 用例校验
# ============================================================

@pytest.fixture(scope="module")
def cases():
    return CASES


@pytest.fixture(scope="module")
def cases_by_id(cases):
    return {case["id"]: case for case in cases}


@pytest.mark.parametrize(
    "relative_path",
    [
        pytest.param("eval/run_eval.py", id="runner"),
        pytest.param("eval/cases.yaml", id="case-file"),
        pytest.param("eval/datasets/sales_2026.csv", id="sales-dataset"),
        pytest.param("eval/datasets/demo_orders.csv", id="demo-dataset"),
    ],
)
def test_project_artifact_exists(relative_path):
    path = os.path.join(PROJECT, relative_path)
    assert os.path.isfile(path), "缺少文件：%s" % path


def test_results_directory_exists():
    directory = os.path.join(EVAL, "results")
    assert os.path.isdir(directory), "缺少目录：%s" % directory


def test_twelve_cases_are_defined(cases):
    assert len(cases) == 12, "用例数量是 %d" % len(cases)


def test_the_shipped_case_file_validates(cases):
    problems = run_eval.validate_cases(cases)
    assert problems == [], problems


def test_every_case_has_an_id_question_and_dataset(cases):
    bad = [
        case for case in cases
        if not (case.get("id") and case.get("question") and case.get("dataset"))
    ]
    assert not bad, bad


def test_every_case_has_a_non_empty_expectation(cases):
    bad = [case.get("id") for case in cases if not case.get("expect")]
    assert not bad, bad


def test_every_case_has_at_least_one_tag(cases):
    bad = [case.get("id") for case in cases if not case.get("tags")]
    assert not bad, bad


# 源脚本里 6 条「坏用例」检查。
# 每条都是「这份用例文件必须被拒」，并且指出被拒的理由。
BAD_CASES = [
    pytest.param(
        [
            {"id": "a", "question": "q", "dataset": "sales_2026.csv",
             "expect": {"numbers": [1]}},
            {"id": "a", "question": "q", "dataset": "sales_2026.csv",
             "expect": {"numbers": [1]}},
        ],
        "重复",
        id="duplicate-id",
    ),
    pytest.param(
        [{"id": "b", "question": "q", "dataset": "nope.csv",
          "expect": {"numbers": [1]}}],
        "数据集不存在",
        id="missing-dataset",
    ),
    pytest.param(
        [{"id": "c", "question": "q", "dataset": "sales_2026.csv",
          "expect": {"numberss": [1]}}],
        "不认识的期望字段",
        id="typo-in-expectation-key",
    ),
    pytest.param(
        [{"id": "d", "question": "q", "dataset": "sales_2026.csv",
          "expect": {}}],
        "不会判定",
        id="empty-expectation",
    ),
    pytest.param(
        [{"id": "e", "question": "q", "dataset": "sales_2026.csv"}],
        None,          # 源脚本只要求「非空」，没要求具体措辞
        id="missing-expectation",
    ),
    pytest.param(
        [{"id": "f", "question": "q", "dataset": "sales_2026.csv",
          "expect": {"numbers": 1}}],
        "必须是列表",
        id="non-list-expectation-value",
    ),
]


@pytest.mark.parametrize("bad_cases, expected_problem", BAD_CASES)
def test_validate_cases_rejects_a_bad_case_file(bad_cases, expected_problem):
    problems = run_eval.validate_cases(bad_cases)
    assert problems, "坏用例没有被拒：%r" % (bad_cases,)
    if expected_problem is not None:
        assert any(expected_problem in problem for problem in problems), problems


# ============================================================
# B. 答案键用 pandas 独立复算
# ============================================================

@pytest.fixture(scope="module")
def sales():
    return pd.read_csv(os.path.join(DATASETS, "sales_2026.csv"))


@pytest.fixture(scope="module")
def demo():
    return pd.read_csv(os.path.join(DATASETS, "demo_orders.csv"))


@pytest.fixture(scope="module")
def answer_key(sales, demo):
    """独立复算出来的正确答案，不看 cases.yaml。"""

    done = demo[demo["订单状态"] == "已完成"].copy()
    done["净"] = done["销售额"] - done["退款金额"] - done["折扣金额"]

    sales_dates = pd.to_datetime(sales["订单日期"])

    region_key = sorted(
        float(value) for value in sales.groupby("地区")["销售额"].sum()
    )

    return {
        "sum-total": [float(sales["销售额"].sum())],
        "by-region": region_key,
        "row-count": [float(len(sales))],
        "avg-sale": [round(float(sales["销售额"].mean()), 2)],
        "category-filter": [
            float(sales[sales["品类"] == "电子产品"]["销售额"].sum())
        ],
        "quarterly": sorted(
            float(value) for value in
            sales.groupby(sales_dates.dt.to_period("Q").astype(str))["销售额"].sum()
        ),
        "monthly": sorted(
            float(value) for value in
            sales.groupby(sales_dates.dt.to_period("M").astype(str))["销售额"].sum()
        ),
        "net-sales": [float(done["净"].sum())],
        "aov": [round(float(done["净"].sum()) / len(done), 2)],
        "net-by-region": sorted(
            float(value) for value in done.groupby("地区")["净"].sum()
        ),
        "typed-tool-only": region_key,
    }


@pytest.mark.parametrize("case_id", NUMERIC_CASE_IDS)
def test_answer_key_matches_pandas(case_id, cases_by_id, answer_key):
    case = cases_by_id[case_id]

    assert case_id in answer_key, "没有为 %s 准备独立答案键" % case_id

    declared = sorted(float(value) for value in case["expect"]["numbers"])
    computed = answer_key[case_id]

    same = len(declared) == len(computed) and all(
        abs(a - b) < 0.01 for a, b in zip(declared, computed)
    )

    assert same, "declared=%s computed=%s" % (
        [round(value, 2) for value in declared][:4],
        [round(value, 2) for value in computed][:4],
    )


def test_the_answer_key_covers_every_case_with_numbers(cases, answer_key):
    missing = [
        case["id"] for case in cases
        if case["expect"].get("numbers") and case["id"] not in answer_key
    ]
    assert not missing, missing


def test_the_sales_fixture_dropped_the_status_column(sales):
    assert "订单状态" not in sales.columns, list(sales.columns)


@pytest.mark.parametrize(
    "fixture_name, expected_rows",
    [
        pytest.param("sales_2026.csv", 40, id="sales-rows"),
        pytest.param("demo_orders.csv", 8, id="demo-rows"),
    ],
)
def test_fixture_row_counts(fixture_name, expected_rows):
    frame = pd.read_csv(os.path.join(DATASETS, fixture_name))
    assert len(frame) == expected_rows, "%s 有 %d 行，期望 %d 行" % (
        fixture_name, len(frame), expected_rows,
    )


# 源脚本的可选分支：拿一份「还带着订单状态列的原始 sales CSV」和 fixture 对账。
# 仓库里没有这份文件，所以这里用环境变量喂 —— 没给就 skip，不假装测过。
skip_without_original = pytest.mark.skipif(
    not os.path.isfile(ORIGINAL_SALES),
    reason="未提供 DATAMIND_ORIGINAL_SALES（带订单状态列的原始 sales CSV）",
)


@skip_without_original
def test_dropping_the_status_column_did_not_change_the_numbers(sales):
    original = pd.read_csv(ORIGINAL_SALES)
    assert abs(
        float(original["销售额"].sum()) - float(sales["销售额"].sum())
    ) < 0.01, "original=%.0f fixture=%.0f" % (
        original["销售额"].sum(), sales["销售额"].sum(),
    )


@skip_without_original
def test_the_fixture_has_the_same_row_count_as_the_source(sales):
    original = pd.read_csv(ORIGINAL_SALES)
    assert len(original) == len(sales), "%d vs %d" % (len(original), len(sales))


# ============================================================
# C. judge()：每种期望类型的通过与不通过
# ============================================================

def make_run(**overrides):
    """造一个和 run_case() 返回值同形状的 run。"""

    run = {
        "id": "x",
        "duration": 10.0,
        "llm_calls": 6,
        "status": "success",
        "error": None,
        "report": "报告内容",
        "numbers": [],
        "report_numbers": [],
        "tools": [],
        "knowledge": [],
        "charts": 0,
        "verification_passed": True,
    }

    run.update(overrides)

    # 和真实 runner 一样：报告里的数值是从报告正文抽出来的
    run["report_numbers"] = _numbers_in(run.get("report") or "")

    return run


# expect 字典 / run 覆盖项 / 期望的 passed / 失败理由里必须出现的文字
JUDGE_CASES = [
    pytest.param({"numbers": [100]}, {"numbers": [100.0, 200.0]},
                 True, None, id="matching-number-passes"),
    pytest.param({"numbers": [100]}, {"numbers": [100.4]},
                 True, None, id="number-within-tolerance-passes"),
    pytest.param({"numbers": [100], "tolerance": 2}, {"numbers": [101.5]},
                 True, None, id="explicit-tolerance-is-honoured"),
    pytest.param({"numbers": [100]}, {"numbers": [120.0]},
                 False, "100", id="wrong-number-fails-and-names-the-value"),
    pytest.param({"forbid_tools": ["run_python"]},
                 {"tools": ["aggregate_metric", "run_python"]},
                 False, "run_python", id="forbidden-tool-fails-and-names-it"),
    pytest.param({"require_tools": ["aggregate_metric"]},
                 {"tools": ["run_python"]},
                 False, "aggregate_metric", id="missing-required-tool-fails"),
    pytest.param({"knowledge": ["净销售额"]}, {"knowledge": ["客单价"]},
                 False, "净销售额", id="missing-definition-fails-and-names-it"),
    pytest.param({"knowledge": ["净销售额"]}, {"knowledge": ["净销售额"]},
                 True, None, id="applied-definition-passes"),
    pytest.param({"min_charts": 1}, {"charts": 0},
                 False, None, id="too-few-charts-fails"),
    pytest.param({"min_charts": 1}, {"charts": 2},
                 True, None, id="enough-charts-passes"),
    pytest.param({"status": "success"}, {"status": "exhausted"},
                 False, None, id="wrong-status-fails"),
    pytest.param({"status": "success"}, {"status": "success", "report": ""},
                 True, None, id="explicit-status-does-not-require-a-report"),
    pytest.param({}, {"report": ""},
                 False, None, id="no-report-fails-without-an-explicit-status"),
    pytest.param({}, {"error": "KeyError: boom"},
                 False, "boom", id="execution-error-fails-immediately"),
    pytest.param({"must_mention": ["成本"]},
                 {"report": "数据集中没有成本字段"},
                 True, None, id="required-word-passes"),
    pytest.param({"must_mention_any": ["不存在", "缺少"]},
                 {"report": "数据集里缺少该字段"},
                 True, None, id="must-mention-any-passes-on-an-alternative"),
    pytest.param({"must_mention_any": ["不存在", "缺少"]},
                 {"report": "成本为 12345 元"},
                 False, None, id="must-mention-any-fails-when-none-appears"),
    pytest.param({"report_numbers": [11350]},
                 {"report": "净销售额为 11,350 元"},
                 True, None, id="report-number-read-with-thousands-separator"),
]


@pytest.mark.parametrize(
    "expect, overrides, expected_passed, reason_text", JUDGE_CASES
)
def test_judge(expect, overrides, expected_passed, reason_text):
    passed, reasons = run_eval.judge(
        {"id": "x", "expect": expect},
        make_run(**overrides),
    )

    assert passed is expected_passed, reasons

    if reason_text is not None:
        assert any(reason_text in reason for reason in reasons), reasons


# ============================================================
# D. 指标与基线对比
# ============================================================

@pytest.fixture(scope="module")
def runs():
    return [
        {"id": "a", "passed": True, "duration": 10.0, "llm_calls": 5,
         "tools": ["aggregate_metric"], "tags": ["aggregate"],
         "verification_passed": True, "report": "x"},
        {"id": "b", "passed": False, "duration": 20.0, "llm_calls": 7,
         "tools": ["run_python"], "tags": ["aggregate", "time"],
         "verification_passed": False, "report": "y"},
        {"id": "c", "passed": True, "duration": 30.0, "llm_calls": 9,
         "tools": ["run_python", "aggregate_metric"], "tags": ["time"],
         "verification_passed": True, "report": "z"},
    ]


@pytest.fixture(scope="module")
def metrics(runs):
    return run_eval.summarize(runs)


@pytest.mark.parametrize(
    "key, expected",
    [
        pytest.param("pass_rate", 66.7, id="pass-rate"),
        pytest.param("passed", 2, id="passed-count"),
        pytest.param("total", 3, id="total-count"),
        pytest.param("completed", 3, id="completed-count"),
        pytest.param("avg_duration", 20.0, id="avg-duration"),
        pytest.param("avg_llm_calls", 7.0, id="avg-llm-calls"),
        pytest.param("exec_rate", 66.7, id="exec-rate"),
        pytest.param("verification_rate", 66.7, id="verification-rate"),
    ],
)
def test_summarize_metric(key, expected, metrics):
    assert metrics[key] == expected, "metrics[%r] = %r（期望 %r）" % (
        key, metrics[key], expected,
    )


@pytest.mark.parametrize(
    "tag, expected",
    [
        pytest.param("aggregate", "1/2", id="aggregate-tag"),
        pytest.param("time", "1/2", id="time-tag"),
    ],
)
def test_summarize_by_tag(tag, expected, metrics):
    assert metrics["by_tag"][tag] == expected, metrics["by_tag"]


def _previous_baseline(flags=(True, True, True)):
    return {
        "saved_at": "2026-10-12 10:00:00",
        "runs": [
            {"id": case_id, "passed": passed}
            for case_id, passed in zip(("a", "b", "c"), flags)
        ],
    }


@pytest.mark.parametrize(
    "field, expected",
    [
        pytest.param("new_failures", ["b"], id="new-failures"),
        pytest.param("new_passes", [], id="no-spurious-new-passes"),
        pytest.param("unchanged", 2, id="unchanged-count"),
        pytest.param("baseline", "2026-10-12 10:00:00", id="baseline-timestamp"),
    ],
)
def test_compare_with_baseline(field, expected, runs):
    comparison = run_eval.compare_with(_previous_baseline(), runs)
    assert comparison[field] == expected, comparison


def test_compare_with_detects_a_newly_passing_case(runs):
    comparison = run_eval.compare_with(
        _previous_baseline((False, True, True)), runs
    )
    assert comparison["new_passes"] == ["a"], comparison


# ============================================================
# E. 离线校验：不花钱、不落盘
# ============================================================

def _result_files():
    directory = os.path.join(EVAL, "results")
    if not os.path.isdir(directory):
        return set()
    return set(os.listdir(directory))


@pytest.fixture(scope="module")
def offline_check():
    """以子进程跑一次 --check，退出码 / 输出 / 落盘都只看这一次运行。"""

    env = dict(os.environ)

    # 子进程默认按系统 locale 编码写 stdout（中文 Windows 上是 GBK）。
    # 这里显式要求 UTF-8，否则下面按 UTF-8 解码会变成乱码，
    # 输出断言就会因为编码而不是因为行为失败。
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("OPENAI_API_KEY", "sk-verify-dummy")

    files_before = _result_files()

    completed = subprocess.run(
        [sys.executable, RUNNER, "--check", "--no-save"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )

    return {
        "completed": completed,
        "files_before": files_before,
        "files_after": _result_files(),
    }


def test_offline_check_exits_zero(offline_check):
    completed = offline_check["completed"]
    assert completed.returncode == 0, "rc=%s\n%s" % (
        completed.returncode, (completed.stderr or "")[-500:],
    )


def test_offline_check_reports_the_case_count(offline_check):
    stdout = offline_check["completed"].stdout or ""
    assert "用例数量：12" in stdout, stdout[:300].replace("\n", " ")


def test_offline_check_reports_the_dictionary_size(offline_check):
    """条数不写死：知识库会随领域包增长。"""

    stdout = offline_check["completed"].stdout or ""

    assert "业务口径：" in stdout, stdout[:360].replace("\n", " ")

    import importlib.util
    import os as _os

    here = _os.path.dirname(_os.path.abspath(__file__))
    project = _os.path.dirname(here)

    if not _os.path.isdir(_os.path.join(project, "agent")):
        project = _os.path.dirname(project)

    spec = importlib.util.spec_from_file_location(
        "run_eval_for_count",
        _os.path.join(project, "eval", "run_eval.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    expected = len(module.load_entries())

    assert "%d 条" % expected in stdout, (
        "口径条数应当来自真实知识库，期望 %d 条" % expected
    )


def test_offline_check_lists_the_tags(offline_check):
    stdout = offline_check["completed"].stdout or ""
    assert "knowledge" in stdout, stdout[:360].replace("\n", " ")


def test_offline_check_does_not_call_the_model(offline_check):
    stdout = offline_check["completed"].stdout or ""
    assert "开始评测" not in stdout, stdout[:360].replace("\n", " ")


def test_offline_check_writes_no_result_file(offline_check):
    assert offline_check["files_after"] == offline_check["files_before"], (
        "eval/results/ 多出了文件：%s"
        % sorted(offline_check["files_after"] - offline_check["files_before"])
    )


# ============================================================
# F. 第一次真机评测发现的修复
# ============================================================

# --- 1) verify 节点的「提请关注」不应该短路判定

VERIFICATION_JUDGE_CASES = [
    pytest.param(
        {"numbers": [100]},
        {"numbers": [100.0],
         "state_warning": "数值核对：1 / 5 个数值无法追溯到计算结果"},
        True,
        None,
        id="a-verification-warning-does-not-fail-the-case",
    ),
    pytest.param(
        {"numbers": [100]},
        {"numbers": [100.0], "error": "RuntimeError: boom"},
        False,
        None,
        id="a-real-exception-still-fails-the-case",
    ),
    pytest.param(
        {"verification": True},
        {"verification_passed": True},
        True,
        None,
        id="verification-can-be-required-explicitly",
    ),
    pytest.param(
        {"verification": True},
        {"verification_passed": False},
        False,
        None,
        id="a-required-but-failed-verification-fails",
    ),
]


@pytest.mark.parametrize(
    "expect, overrides, expected_passed, reason_text", VERIFICATION_JUDGE_CASES
)
def test_judge_treats_verification_as_a_warning_not_a_failure(
    expect, overrides, expected_passed, reason_text
):
    passed, reasons = run_eval.judge(
        {"id": "x", "expect": expect},
        make_run(**overrides),
    )

    assert passed is expected_passed, reasons

    if reason_text is not None:
        assert any(reason_text in reason for reason in reasons), reasons


def test_a_verification_warning_is_carried_outside_error():
    """state["error"] 走 state_warning，不走 run["error"]，后者只给异常。"""

    run = make_run(
        numbers=[100.0],
        state_warning="数值核对：1 / 5 个数值无法追溯到计算结果",
    )

    assert run["state_warning"]
    assert run["error"] is None


def test_verification_is_a_known_expectation_key():
    assert "verification" in run_eval.EXPECT_KEYS


# --- 2) 校验器：倍数 + 百分比反推

REGION_SOURCE = [{
    "status": "success",
    "data": {"result": (
        "地区  销售额\n华东  57368\n东北  47115\n华中  30218\n"
        "西南  19081\n华南  14031\n华北  7369"
    ), "output": ""},
}]

NET_SOURCE = [{
    "status": "success",
    "data": {"result": (
        "销售额合计 12600\n退款合计 600\n折扣合计 650\n净销售额 11350"
    ), "output": ""},
}]


@pytest.mark.parametrize(
    "report, source, expected_passed",
    [
        pytest.param(
            "华东约为东北的 1.22 倍。", REGION_SOURCE, True,
            id="a-ratio-written-as-倍-is-traceable",
        ),
        pytest.param(
            "华东、东北、华中合计 134701，占全部的约 76.90%。",
            REGION_SOURCE, True,
            id="a-percentage-over-a-3-term-sub-total-is-traceable",
        ),
        pytest.param(
            "退款与折扣合计占销售额的约 9.9%。", NET_SOURCE, True,
            id="a-percentage-over-a-pairwise-sum-is-traceable",
        ),
        pytest.param(
            "华东约为东北的 5.5 倍。", REGION_SOURCE, False,
            id="a-fabricated-ratio-is-still-caught",
        ),
        pytest.param(
            "华东、东北、华中合计 134701，占全部的约 91.30%。",
            REGION_SOURCE, False,
            id="a-fabricated-percentage-is-still-caught",
        ),
    ],
)
def test_verify_report_numbers(report, source, expected_passed):
    result = verify_report_numbers(report, source, [])
    assert result["passed"] is expected_passed, [
        claim["raw"] for claim in result["unmatched"]
    ]


# 池子被重复值撑爆时，正确的边际合计会被误报（真机踩过）
NOISY_SOURCE = [{
    "status": "success",
    "data": {"result": " ".join([
        "rows 40 cols 9",
        "0 1 0 38 0 10 0 4 0 6 0 8 0 16",
        "699 0 36 2796",
    ] * 3) + " " + " ".join([
        "34879", "23276", "7418", "27379", "23817", "18423",
        "9485", "29344", "1161",
    ] * 2) + " 175182 40 9 9 175182 40 4379.55 40"},
}]


def test_a_marginal_total_survives_a_pool_full_of_duplicates():
    result = verify_report_numbers(
        "上半年（1-6 月）合计 135192，第三季度合计 39990。",
        NOISY_SOURCE, [])
    assert result["passed"] is True, [
        claim["raw"] for claim in result["unmatched"]
    ]


# --- 3) 字段缺失时不要自己编定义

@pytest.fixture(scope="module")
def executor_source():
    path = os.path.join(PROJECT, "agent", "nodes", "executor.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_the_executor_is_told_not_to_invent_a_definition(executor_source):
    assert "不要用其他字段凑出一个定义" in executor_source


def test_the_rule_appears_in_both_the_system_and_the_task_prompt(executor_source):
    count = executor_source.count("不要用其他字段凑出一个定义")
    assert count >= 2, "只出现了 %d 次" % count
