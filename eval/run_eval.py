"""
DataMind V4.7
Agent 评测脚本

判定原则：

    只看 Agent **真实算出来的数值**，
    不让另一个模型去读报告打分。

    「听起来对」和「算得对」是两件事。
    如果让模型判断模型，一份自信的错误报告也会得高分，
    那评测就没有意义了。

    所以主指标是确定性的：
    从 execution_results 里取出真实算出的所有数值，
    看正确答案在不在里面。

用法：

    python eval/run_eval.py                    跑全部
    python eval/run_eval.py --tag knowledge    按标签跑
    python eval/run_eval.py --id net-sales     只跑一个
    python eval/run_eval.py --limit 3          只跑前三个
    python eval/run_eval.py --check            只做离线校验，不花钱
    python eval/run_eval.py --compare          和上一次结果对比

注意：

    评测时把 temperature 固定成 0，
    减少跑与跑之间的随机差异，这样基线对比才有意义。
    这和线上默认值（0.2）不同，属于评测惯例。
"""

import argparse
import importlib
import json
import os
import sys
import time
from datetime import datetime

PROJECT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import yaml

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT, ".env"), override=True)

from agent.utils.verifier import _numbers_in  # noqa: E402
from agent.utils.knowledge import (  # noqa: E402
    dataset_columns,
    load_entries,
)

CASES_PATH = os.path.join(PROJECT, "eval", "cases.yaml")
DATASET_DIR = os.path.join(PROJECT, "eval", "datasets")
RESULT_DIR = os.path.join(PROJECT, "eval", "results")

EXPECT_KEYS = {
    "numbers",
    "report_numbers",
    "tolerance",
    "status",
    "require_tools",
    "forbid_tools",
    "knowledge",
    "min_charts",
    "must_mention",
    "must_mention_any",
    "verification",
}

# 评测时固定温度
EVAL_TEMPERATURE = 0.0

COUNTING = {"llm_calls": 0}

# 所有会调用模型的模块
LLM_MODULES = [
    "agent.nodes.planner",
    "agent.nodes.executor",
    "agent.nodes.error_handler",
    "agent.nodes.task_failure",
    "agent.nodes.reviewer",
    "agent.nodes.reporter",
    "agent.utils.chart_planner",
    "agent.utils.knowledge",
    "tools.loader",
]


# ========================================
# 用例
# ========================================

def load_cases(path=None):
    with open(path or CASES_PATH, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    cases = data.get("cases") if isinstance(data, dict) else data
    return cases if isinstance(cases, list) else []


def validate_cases(cases):
    """
    离线校验用例文件，这样改用例不用先花钱跑一遍。
    """

    problems = []

    seen = set()

    for index, case in enumerate(cases):

        label = case.get("id") or "第 %d 条" % (index + 1)

        if not case.get("id"):
            problems.append("%s：缺少 id" % label)
            continue

        if case["id"] in seen:
            problems.append("%s：id 重复" % label)

        seen.add(case["id"])

        if not case.get("question"):
            problems.append("%s：缺少 question" % label)

        dataset = case.get("dataset")

        if not dataset:
            problems.append("%s：缺少 dataset" % label)

        elif not os.path.isfile(os.path.join(DATASET_DIR, dataset)):
            problems.append("%s：数据集不存在 %s" % (label, dataset))

        expect = case.get("expect") or {}

        if not isinstance(expect, dict):
            problems.append("%s：expect 必须是对象" % label)
            continue

        for key in expect:

            if key not in EXPECT_KEYS:
                problems.append("%s：不认识的期望字段 %s" % (label, key))

        if not expect:
            problems.append("%s：expect 是空的，这条用例不会判定任何东西"
                            % label)

        for key in ("numbers", "report_numbers", "require_tools",
                    "forbid_tools", "knowledge", "must_mention",
                    "must_mention_any"):

            if key in expect and not isinstance(expect[key], list):
                problems.append("%s：%s 必须是列表" % (label, key))

    return problems


# ========================================
# 调用计数 + 固定温度
# ========================================

def instrument():
    """
    给所有会调用模型的地方套一层：
    计数 + 固定 temperature。
    """

    def wrap_plain(func):

        def wrapped(messages, temperature=0.2, **kwargs):
            COUNTING["llm_calls"] += 1
            return func(messages, temperature=EVAL_TEMPERATURE)

        return wrapped

    def wrap_tools(func):

        def wrapped(messages, tools, temperature=0.2,
                    tool_choice="auto", **kwargs):
            COUNTING["llm_calls"] += 1
            return func(
                messages,
                tools,
                temperature=EVAL_TEMPERATURE,
                tool_choice=tool_choice,
            )

        return wrapped

    patched = []

    for name in LLM_MODULES:

        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001
            continue

        if hasattr(module, "call_llm"):
            module.call_llm = wrap_plain(module.call_llm)
            patched.append(name + ".call_llm")

        if hasattr(module, "call_llm_with_tools"):
            module.call_llm_with_tools = wrap_tools(
                module.call_llm_with_tools
            )
            patched.append(name + ".call_llm_with_tools")

    return patched


# ========================================
# 运行
# ========================================

def initial_state(question, file_path):

    return {
        "question": question,
        "file_path": file_path,
        "dataset_info": None,
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


def collect_numbers(results):
    numbers = []
    for item in results or []:
        data = item.get("data") or {}
        numbers.extend(_numbers_in(data.get("result")))
        numbers.extend(_numbers_in(data.get("output")))
    return numbers


def collect_tools(results):
    tools = []
    for item in results or []:
        for call in (item.get("data") or {}).get("tool_calls") or []:
            if call.get("name"):
                tools.append(call["name"])
    return tools


def run_case(graph, case):

    dataset = os.path.join(DATASET_DIR, case["dataset"])

    COUNTING["llm_calls"] = 0

    started = time.monotonic()

    error = None

    try:

        final = graph.invoke(
            initial_state(case["question"], dataset)
        )

    except Exception as exc:  # noqa: BLE001

        final = {}
        error = "%s: %s" % (type(exc).__name__, str(exc)[:200])

    duration = time.monotonic() - started

    results = final.get("execution_results") or []

    report = final.get("final_report") or ""

    verification = final.get("verification") or {}

    return {
        "id": case["id"],
        "dataset": case["dataset"],
        "question": case["question"],
        "tags": case.get("tags") or [],
        "duration": round(duration, 1),
        "llm_calls": COUNTING["llm_calls"],
        "status": final.get("execution_status"),
        # 只有抛异常才算「执行错误」。
        #
        # 注意：verify 节点在发现无法追溯的数值时会写 state["error"]，
        # 那是「提请关注」的信号，不是执行失败。
        # 它已经由 verification_passed 单独度量，
        # 不应该短路掉其它期望的判定。
        #
        # 第一次跑评测时就是踩了这个坑：
        # 6 条用例因为这个原因被判失败，
        # 而它们的数字其实全都算对了。
        "error": error,
        "state_warning": final.get("error"),
        "verification_unmatched": [
            item.get("raw")
            for item in verification.get("unmatched") or []
        ],
        "report": report,
        "numbers": collect_numbers(results),
        "report_numbers": _numbers_in(report),
        "tools": collect_tools(results),
        "knowledge": [
            item.get("name")
            for item in (final.get("knowledge_hits") or [])
        ],
        "charts": len(final.get("chart_results") or []),
        "verification_passed": bool(verification.get("passed")),
    }


# ========================================
# 判定
# ========================================

def found(numbers, target, tolerance):

    return any(
        abs(value - target) <= tolerance
        for value in numbers
    )


def judge(case, run):

    expect = case.get("expect") or {}

    tolerance = expect.get("tolerance", 0.5)

    reasons = []
    passed = True

    if run.get("error"):

        return False, ["执行出错：%s" % str(run["error"])[:120]]

    # --- 执行状态
    want_status = expect.get("status")

    if want_status:

        if run.get("status") != want_status:
            passed = False
            reasons.append(
                "执行状态是 %s，期望 %s"
                % (run.get("status"), want_status)
            )

    elif not run.get("report"):

        passed = False
        reasons.append("没有生成报告")

    # --- 真实算出的数值
    for target in expect.get("numbers") or []:

        if not found(run["numbers"], target, tolerance):
            passed = False
            reasons.append("没有算出 %s" % target)

    # --- 报告里的数值
    for target in expect.get("report_numbers") or []:

        if not found(run["report_numbers"], target, tolerance):
            passed = False
            reasons.append("报告里没有 %s" % target)

    # --- 数值核对（可选）
    #
    # 默认不判定：核对结果是「提请关注」，
    # 不是「算错了」。要严格要求的用例可以显式打开。
    want_verification = expect.get("verification")

    if want_verification is not None:

        if bool(run.get("verification_passed")) != bool(want_verification):

            passed = False

            reasons.append(
                "数值核对%s，期望%s"
                % (
                    "通过" if run.get("verification_passed") else "未通过",
                    "通过" if want_verification else "未通过",
                )
            )

    # --- 工具
    for name in expect.get("require_tools") or []:

        if name not in run["tools"]:
            passed = False
            reasons.append("没有调用 %s" % name)

    for name in expect.get("forbid_tools") or []:

        if name in run["tools"]:
            passed = False
            reasons.append("不应该调用 %s" % name)

    # --- 业务口径
    for name in expect.get("knowledge") or []:

        if name not in run["knowledge"]:
            passed = False
            reasons.append("没有应用口径 %s" % name)

    # --- 图表
    minimum = expect.get("min_charts")

    if minimum is not None and run["charts"] < minimum:
        passed = False
        reasons.append(
            "只生成了 %d 张图，期望至少 %d 张"
            % (run["charts"], minimum)
        )

    # --- 报告措辞
    report = run.get("report") or ""

    for text in expect.get("must_mention") or []:

        if text not in report:
            passed = False
            reasons.append("报告里没有提到「%s」" % text)

    alternatives = expect.get("must_mention_any") or []

    if alternatives and not any(text in report for text in alternatives):
        passed = False
        reasons.append(
            "报告里没有出现以下任何一个：%s"
            % "、".join(alternatives)
        )

    return passed, reasons


# ========================================
# 指标
# ========================================

def summarize(runs):

    total = len(runs)

    if not total:
        return {}

    passed = [run for run in runs if run["passed"]]

    by_tag = {}

    for run in runs:

        for tag in run.get("tags") or ["(无标签)"]:

            bucket = by_tag.setdefault(tag, [0, 0])

            bucket[1] += 1

            if run["passed"]:
                bucket[0] += 1

    def average(key):
        return round(
            sum(run[key] for run in runs) / total,
            2,
        )

    exec_runs = [
        run for run in runs if "run_python" in run["tools"]
    ]

    return {
        "total": total,
        "passed": len(passed),
        "pass_rate": round(len(passed) / total * 100, 1),
        "by_tag": {
            tag: "%d/%d" % (value[0], value[1])
            for tag, value in sorted(by_tag.items())
        },
        "completed": len([run for run in runs if run.get("report")]),
        "avg_duration": average("duration"),
        "avg_llm_calls": average("llm_calls"),
        "avg_tools": round(
            sum(len(run["tools"]) for run in runs) / total, 2
        ),
        "exec_rate": round(len(exec_runs) / total * 100, 1),
        "verification_rate": round(
            len([r for r in runs if r["verification_passed"]]) / total * 100,
            1,
        ),
    }


def compare_with(previous, runs):
    """
    和上一次结果对比 —— 这是这个模块存在的真正理由：
    改动之后，知道有没有变差。
    """

    old = {item["id"]: item for item in previous.get("runs") or []}

    new_failures = []
    new_passes = []
    unchanged = 0

    for run in runs:

        past = old.get(run["id"])

        if past is None:
            continue

        if past.get("passed") and not run["passed"]:
            new_failures.append(run["id"])

        elif not past.get("passed") and run["passed"]:
            new_passes.append(run["id"])

        else:
            unchanged += 1

    return {
        "baseline": previous.get("saved_at"),
        "new_failures": new_failures,
        "new_passes": new_passes,
        "unchanged": unchanged,
    }


def latest_result(exclude=None):

    if not os.path.isdir(RESULT_DIR):
        return None

    files = sorted(
        name for name in os.listdir(RESULT_DIR)
        if name.endswith(".json") and name != exclude
    )

    if not files:
        return None

    with open(os.path.join(RESULT_DIR, files[-1]), "r",
              encoding="utf-8") as handle:
        return json.load(handle)


# ========================================
# 输出
# ========================================

def print_table(runs):

    print()
    print("%-16s %-6s %-7s %-7s %-5s %s"
          % ("用例", "结果", "耗时", "LLM", "工具", "说明"))
    print("-" * 96)

    for run in runs:

        tools = ",".join(sorted(set(run["tools"]))) or "-"

        if len(tools) > 30:
            tools = tools[:28] + ".."

        note = "；".join(run["reasons"])[:44]

        print("%-16s %-6s %-7s %-7s %-5s %s"
              % (
                  run["id"],
                  "通过" if run["passed"] else "失败",
                  "%.1fs" % run["duration"],
                  run["llm_calls"],
                  tools,
                  note,
              ))


def print_metrics(metrics):

    print()
    print("=" * 96)
    print("指标")
    print("=" * 96)

    print("通过率          : %d/%d = %s%%"
          % (metrics["passed"], metrics["total"], metrics["pass_rate"]))
    print("完成率          : %d/%d"
          % (metrics["completed"], metrics["total"]))
    print("平均耗时        : %ss" % metrics["avg_duration"])
    print("平均 LLM 调用   : %s" % metrics["avg_llm_calls"])
    print("平均工具调用    : %s" % metrics["avg_tools"])
    print("exec 使用率     : %s%%（走了 run_python 的用例占比）"
          % metrics["exec_rate"])
    print("数值核对通过率  : %s%%" % metrics["verification_rate"])
    print("按标签          : %s"
          % "，".join("%s %s" % item for item in metrics["by_tag"].items()))


def main():

    parser = argparse.ArgumentParser(description="DataMind Agent 评测")

    parser.add_argument("--tag", action="append", default=[],
                        help="只跑这些标签，可以重复")

    parser.add_argument("--id", action="append", default=[],
                        help="只跑这些用例 id，可以重复")

    parser.add_argument("--limit", type=int, default=0,
                        help="最多跑几条")

    parser.add_argument("--check", action="store_true",
                        help="只做离线校验，不调用模型")

    parser.add_argument("--compare", action="store_true",
                        help="和上一次结果对比")

    parser.add_argument("--no-save", action="store_true",
                        help="不保存本次结果")

    args = parser.parse_args()

    cases = load_cases()

    if not cases:

        print("没有读到用例：%s" % CASES_PATH)

        return 1

    problems = validate_cases(cases)

    print("用例文件：%s" % CASES_PATH)
    print("用例数量：%d" % len(cases))

    if problems:

        print()
        print("用例校验未通过：")

        for problem in problems:
            print("  - %s" % problem)

        return 1

    print("用例校验：通过")

    entries = load_entries()
    print("业务口径：%d 条" % len(entries))

    if args.check:

        print()
        print("--check 模式，不调用模型。")
        print("可用标签：%s"
              % "，".join(sorted({
                  tag for case in cases for tag in case.get("tags") or []
              })))

        return 0

    selected = cases

    if args.tag:

        wanted = set(args.tag)
        selected = [
            case for case in selected
            if wanted & set(case.get("tags") or [])
        ]

    if args.id:

        wanted = set(args.id)
        selected = [
            case for case in selected if case["id"] in wanted
        ]

    if args.limit:

        selected = selected[:args.limit]

    if not selected:

        print("没有选中任何用例。")

        return 1

    patched = instrument()

    print("已固定 temperature=%s，计数模块 %d 个"
          % (EVAL_TEMPERATURE, len(patched)))

    from agent.graph import build_graph

    graph = build_graph()

    print()
    print("开始评测，共 %d 条用例" % len(selected))

    runs = []

    for index, case in enumerate(selected, 1):

        print("  [%d/%d] %s ..." % (index, len(selected), case["id"]),
              end="", flush=True)

        run = run_case(graph, case)

        passed, reasons = judge(case, run)

        run["passed"] = passed
        run["reasons"] = reasons

        runs.append(run)

        print(" %s (%.1fs)" % ("通过" if passed else "失败", run["duration"]))

    print_table(runs)

    metrics = summarize(runs)

    print_metrics(metrics)

    payload = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": EVAL_TEMPERATURE,
        "metrics": metrics,
        "runs": runs,
    }

    comparison = None

    if args.compare:

        previous = latest_result()

        if previous:

            comparison = compare_with(previous, runs)

            payload["comparison"] = comparison

            print()
            print("=" * 96)
            print("与基线对比（基线：%s）" % comparison["baseline"])
            print("=" * 96)

            print("新失败      : %s"
                  % ("、".join(comparison["new_failures"]) or "无"))

            print("新通过      : %s"
                  % ("、".join(comparison["new_passes"]) or "无"))

            print("状态不变    : %d" % comparison["unchanged"])

            if comparison["new_failures"]:
                print()
                print("⚠ 有用例从通过变成失败，请检查这次改动。")

        else:

            print()
            print("没有找到历史结果，这次作为基线。")

    if not args.no_save:

        os.makedirs(RESULT_DIR, exist_ok=True)

        name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"

        path = os.path.join(RESULT_DIR, name)

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        print()
        print("结果已保存：%s" % os.path.relpath(path, PROJECT))

    failed = [run["id"] for run in runs if not run["passed"]]

    return 1 if failed else 0


if __name__ == "__main__":

    sys.exit(main())
