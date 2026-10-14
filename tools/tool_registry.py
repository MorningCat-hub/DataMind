"""
DataMind V4.5
工具注册表（原生 Tool Calling）

工具集：

    describe_dataset   看数据集的列、类型、缺失值
    aggregate_metric   按维度聚合一个数值字段（主力工具）
    describe_metric    单个数值字段的分布
    run_python         逃生通道，覆盖工具集之外的复杂分析

为什么要有类型化工具：

    V4.5 之前 Executor 是「让模型写一段 Python 代码，我们 exec 执行」。
    模型只是写了一行代码，我们照着跑 ——
    「模型决定调用哪个工具」这件事情并没有真正发生。

    现在把高频分析动作做成带 JSON Schema 的工具：
    模型返回结构化的 tool_calls，我们执行对应的函数，
    把结果作为 role="tool" 回填。

    好处：

    1. Tool Calling 变成真的机制，而不是一个工具名字符串
    2. 常规分析不再经过 exec，攻击面显著缩小
    3. 每次调用都有名字和参数，可以完整记录、展示、审查

注意：

    时间粒度的标签格式和 agent/utils/chart_executor.py 里的
    实现有少量重复。两边的需求不同（一边是绘图标签，一边是聚合键），
    暂时各自保留，后续可以统一到 agent/utils/time_granularity.py。
"""

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from agent.utils.time_granularity import (
    PERIOD_FREQ,
    normalize as normalize_granularity,
    period_start,
)


# ========================================
# 限制
# ========================================

# 聚合结果最多返回多少行（避免超长 observation 撑爆上下文）
MAX_AGGREGATE_ROWS = 50

# run_python 的输出最多保留多少字符
MAX_PYTHON_OUTPUT = 4000


# ========================================
# 工具声明
# ========================================

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "describe_dataset",
            "description": (
                "查看数据集的列名、数据类型、缺失值数量和行数。"
                "在动手计算之前先用它确认有哪些字段可用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_metric",
            "description": (
                "按某个维度聚合一个数值字段。"
                "例如「各地区的销售额合计」「每个月的订单数量」。"
                "如果分组字段是日期，可以用 time_granularity 按年/季/月/周/日聚合。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": ["string", "array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "分组字段名。可以传一个字段名，"
                            "也可以传字段名数组做交叉分组"
                            "（例如 [\"地区\", \"订单日期\"]）；"
                            "整体汇总（不带分组）时传 null"
                        ),
                    },
                    "metric": {
                        "type": "string",
                        "description": "要聚合的数值字段名",
                    },
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "mean", "count", "min", "max"],
                        "description": "聚合方式",
                    },
                    "time_granularity": {
                        "type": ["string", "null"],
                        "enum": [
                            "year", "quarter", "month", "week", "day", None
                        ],
                        "description": (
                            "group_by 是日期字段时按这个粒度聚合；"
                            "否则传 null"
                        ),
                    },
                },
                "required": ["metric", "agg"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_metric",
            "description": (
                "查看单个数值字段的分布：数量、均值、标准差、"
                "最小值、四分位数、最大值。适合回答「分布/波动」类问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": "数值字段名",
                    },
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "执行一段 Pandas 分析代码。"
                "只在上面几个工具覆盖不了的时候使用"
                "（例如多个字段的四则运算、透视、自定义过滤）。"
                "DataFrame 变量固定为 df，最终结果必须赋值给 result，"
                "可以使用 pd。不要读取文件，不要 import。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码",
                    },
                },
                "required": ["code"],
            },
        },
    },
]


TOOL_NAMES = [item["function"]["name"] for item in TOOL_SCHEMAS]


# ========================================
# 加载
# ========================================

def _load(file_path: str) -> pd.DataFrame:
    return pd.read_csv(file_path)


# ========================================
# 格式化
# ========================================

def _format_number(value: Any) -> str:
    """
    数值带上千分位，方便阅读。

    核对器能正确处理千分位，所以这里不需要为了它做妥协。
    """

    if value is None:
        return ""

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, int):
        return f"{value:,}"

    if isinstance(value, float):

        if pd.isna(value):
            return ""

        if float(value).is_integer():
            return f"{int(value):,}"

        return f"{value:,.2f}"

    return str(value)


def _format_table(
    headers: List[str],
    rows: List[List[Any]],
) -> str:
    """渲染成等宽文本表格。"""

    rendered = [[_format_number(cell) for cell in row] for row in rows]

    widths = [len(str(header)) for header in headers]

    for row in rendered:

        for index, cell in enumerate(row):

            if index < len(widths):

                widths[index] = max(widths[index], len(cell))

    lines = [
        "  ".join(
            str(header).ljust(widths[index])
            for index, header in enumerate(headers)
        )
    ]

    for row in rendered:

        lines.append(
            "  ".join(
                cell.ljust(widths[index])
                for index, cell in enumerate(row)
                if index < len(widths)
            )
        )

    return "\n".join(lines)


def _normalize_group_by(value: Any) -> List[str]:
    """
    把分组字段统一成列表。

    模型可以传单个字段名，
    也可以传字段数组做交叉分组（地区 × 季度）。
    """

    if value is None:

        return []

    if isinstance(value, str):

        return [value] if value.strip() else []

    if isinstance(value, (list, tuple)):

        return [
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]

    return []


def _require_columns(
    df: pd.DataFrame,
    columns: List[str],
) -> Optional[Dict[str, Any]]:
    """
    检查字段是否存在；不存在就返回一个给模型看的错误。
    """

    missing = [
        name for name in columns
        if name is not None and name not in df.columns
    ]

    if not missing:

        return None

    return {
        "type": "ColumnNotFound",
        "message": (
            f"数据集里没有字段：{'、'.join(missing)}。"
            f"可用字段：{'、'.join(str(c) for c in df.columns)}"
        ),
    }


# ========================================
# 时间粒度
# ========================================

def _to_datetime(values: pd.Series) -> pd.Series:
    """
    解析日期。

    数值列一律不当作日期 ——
    pandas 会把整数当成 1970 年的纳秒时间戳。
    """

    if pd.api.types.is_numeric_dtype(values):

        return pd.Series(
            pd.NaT,
            index=values.index,
            dtype="datetime64[ns]",
        )

    if pd.api.types.is_datetime64_any_dtype(values):

        return values

    try:

        return pd.to_datetime(
            values,
            errors="coerce",
            format="mixed",
        )

    except TypeError:

        return pd.to_datetime(values, errors="coerce")


def _build_labels(
    dates: pd.Series,
    granularity: str,
) -> pd.Series:
    """把日期转成指定粒度的分组标签。"""

    if granularity == "year":

        labels = dates.dt.strftime("%Y")

    elif granularity == "quarter":

        labels = dates.dt.to_period("Q").astype(str)

    elif granularity == "month":

        labels = dates.dt.to_period("M").astype(str)

    elif granularity == "week":

        iso = dates.dt.isocalendar()

        labels = (
            iso["year"].astype(str)
            + "-W"
            + iso["week"].astype(str).str.zfill(2)
        )

    elif granularity == "day":

        labels = dates.dt.strftime("%Y-%m-%d")

    else:

        raise ValueError(f"不支持的时间粒度：{granularity}")

    return labels.where(dates.notna(), None)


# ========================================
# 各工具实现
# ========================================

def describe_dataset(
    file_path: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:

    df = _load(file_path)

    rows = []

    for column in df.columns:

        series = df[column]

        rows.append([
            str(column),
            str(series.dtype),
            int(series.isna().sum()),
            int(series.nunique(dropna=True)),
            _format_number(series.iloc[0]) if len(series) else "",
        ])

    observation = (
        f"数据集共 {len(df):,} 行，{len(df.columns)} 列。\n\n"
        + _format_table(
            ["字段", "类型", "缺失", "唯一值", "首行样例"],
            rows,
        )
    )

    return {
        "ok": True,
        "observation": observation,
        "summary": f"数据集 {len(df)} 行 × {len(df.columns)} 列",
    }


def aggregate_metric(
    file_path: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:

    """
    按一到多个维度聚合一个数值字段。

    group_by 可以是一个字段名，也可以是字段名数组：
    ["地区", "订单日期"] 会得到「地区 × 时间」的交叉结果。
    """

    df = _load(file_path)

    metric = arguments.get("metric")

    agg = arguments.get("agg")

    group_keys = _normalize_group_by(
        arguments.get("group_by")
    )

    granularity = normalize_granularity(
        arguments.get("time_granularity")
    )

    if agg not in ("sum", "mean", "count", "min", "max"):

        return _error(
            "BadAggregation",
            f"不支持的聚合方式：{agg}。"
            "可用：sum / mean / count / min / max",
        )

    problem = _require_columns(
        df,
        [metric] + group_keys,
    )

    if problem:

        return _error(problem["type"], problem["message"])

    notes: List[str] = []

    working = df

    sort_column = None

    date_key = None

    # ====================================
    # 时间粒度：在分组字段里找出真正的日期字段
    # ====================================

    if group_keys and granularity:

        for key in group_keys:

            dates = _to_datetime(working[key])

            if not dates.isna().all():

                date_key = key

                break

        if date_key is None:

            notes.append(
                "分组字段里没有可以解析成日期的字段"
                "（" + "、".join(group_keys) + "），"
                "已忽略时间粒度，按原值分组。"
            )

            granularity = None

        else:

            dates = _to_datetime(working[date_key])

            working = working.copy()

            working[date_key] = _build_labels(
                dates,
                granularity,
            )

            working = working.dropna(
                subset=[date_key]
            )

            working["_time_sort"] = period_start(
                dates,
                granularity,
            )

            sort_column = "_time_sort"

    # ====================================
    # 聚合
    # ====================================

    if agg == "count":

        if group_keys:

            result = (
                working.groupby(
                    group_keys, as_index=False, sort=False
                )
                .size()
                .rename(columns={"size": "记录数"})
            )

            value_column = "记录数"

        else:

            result = pd.DataFrame({"记录数": [len(working)]})

            value_column = "记录数"

    else:

        if not pd.api.types.is_numeric_dtype(working[metric]):

            converted = pd.to_numeric(
                working[metric],
                errors="coerce",
            )

            if converted.isna().all():

                return _error(
                    "NotNumeric",
                    f"字段「{metric}」不是数值字段，"
                    f"当前类型是 {working[metric].dtype}。"
                    f"如果是计数需求，请改用 agg=count。",
                )

            working = working.assign(**{metric: converted})

            notes.append(
                f"字段「{metric}」原本不是数值类型，已尝试转换。"
            )

        if group_keys:

            grouped = working.groupby(
                group_keys,
                as_index=False,
                sort=False,
            )[metric]

            result = getattr(grouped, agg)()

            if sort_column:

                sort_map = (
                    working[group_keys + [sort_column]]
                    .drop_duplicates(subset=group_keys)
                )

                result = result.merge(
                    sort_map, on=group_keys, how="left"
                )

                result = result.sort_values(
                    group_keys[:1] + [sort_column]
                ).drop(columns=[sort_column])

            else:

                result = result.sort_values(
                    metric, ascending=False
                )

        else:

            value = getattr(working[metric], agg)()

            result = pd.DataFrame({metric: [value]})

        value_column = metric

    total_rows = len(result)

    truncated = total_rows > MAX_AGGREGATE_ROWS

    shown = result.head(MAX_AGGREGATE_ROWS)

    headers = list(shown.columns)

    body = shown.values.tolist()

    description = (
        "按「" + " × ".join(group_keys) + "」分组"
        if group_keys
        else "整体汇总"
    )

    if granularity and date_key:

        description += (
            f"（「{date_key}」按{granularity}粒度）"
        )

    observation = (
        f"{description}，对「{metric}」取 {agg}：\n\n"
        + _format_table([str(h) for h in headers], body)
    )

    if truncated:

        observation += (
            f"\n\n（共 {total_rows} 行，只展示前 "
            f"{MAX_AGGREGATE_ROWS} 行）"
        )

    if notes:

        observation += "\n\n提示：" + "；".join(notes)

    return {
        "ok": True,
        "observation": observation,
        "summary": f"{description} 得到 {total_rows} 行",
    }


def describe_metric(
    file_path: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:

    df = _load(file_path)

    metric = arguments.get("metric")

    problem = _require_columns(df, [metric])

    if problem:

        return _error(problem["type"], problem["message"])

    series = pd.to_numeric(df[metric], errors="coerce")

    if series.isna().all():

        return _error(
            "NotNumeric",
            f"字段「{metric}」不是数值字段，无法做分布统计。",
        )

    stats = [
        ("记录数", int(series.notna().sum())),
        ("缺失数", int(series.isna().sum())),
        ("均值", float(series.mean())),
        ("标准差", float(series.std())),
        ("最小值", float(series.min())),
        ("25%", float(series.quantile(0.25))),
        ("中位数", float(series.median())),
        ("75%", float(series.quantile(0.75))),
        ("最大值", float(series.max())),
    ]

    observation = (
        f"字段「{metric}」的分布：\n\n"
        + _format_table(
            ["统计量", "值"],
            [[name, value] for name, value in stats],
        )
    )

    return {
        "ok": True,
        "observation": observation,
        "summary": f"{metric} 的分布统计完成",
    }


def run_python(
    file_path: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:

    from tools.python_tool import run_stat_calc

    code = arguments.get("code")

    if not isinstance(code, str) or not code.strip():

        return _error("BadArgument", "code 不能为空")

    outcome = run_stat_calc(
        file_path=file_path,
        expression=code,
    )

    if not outcome.get("success"):

        error = outcome.get("error") or {}

        return {
            "ok": False,
            "observation": (
                "代码执行失败："
                f"{error.get('type', 'UnknownError')}: "
                f"{error.get('message', '')}"
            ),
            "summary": f"run_python 失败：{error.get('type')}",
            "error": {
                "type": error.get("type", "UnknownError"),
                "message": error.get("message", ""),
            },
        }

    data = outcome.get("data") or {}

    text = ""

    if data.get("output"):

        text += str(data["output"]).strip() + "\n"

    text += "result = " + str(data.get("result"))

    if len(text) > MAX_PYTHON_OUTPUT:

        text = text[:MAX_PYTHON_OUTPUT] + "\n…（输出过长，已截断）"

    return {
        "ok": True,
        "observation": text,
        "summary": "run_python 执行成功",
    }


def _error(error_type: str, message: str) -> Dict[str, Any]:
    """
    工具失败时也要返回一段 observation ——
    模型需要看到错误原因才能自己纠正。
    """

    return {
        "ok": False,
        "observation": f"调用失败：{error_type}: {message}",
        "summary": f"{error_type}: {message}",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


# ========================================
# 分发
# ========================================

_DISPATCH = {
    "describe_dataset": describe_dataset,
    "aggregate_metric": aggregate_metric,
    "describe_metric": describe_metric,
    "run_python": run_python,
}


def parse_arguments(raw: Any) -> Dict[str, Any]:
    """
    解析模型给的参数。

    模型偶尔会给出不合法或空的 JSON，
    这里统一兜成空字典，让工具自己去报「缺参数」。
    """

    if isinstance(raw, dict):

        return raw

    if not raw:

        return {}

    try:

        parsed = json.loads(raw)

    except (TypeError, ValueError):

        return {}

    return parsed if isinstance(parsed, dict) else {}


def execute_tool_call(
    name: str,
    arguments: Dict[str, Any],
    file_path: str,
) -> Dict[str, Any]:
    """
    执行一次工具调用。

    无论成功失败，都会返回 observation 和 summary，
    这样调用方总能给模型一个回复（协议要求每个
    tool_call_id 都必须有对应的 tool 消息）。
    """

    handler = _DISPATCH.get(name)

    if handler is None:

        return _error(
            "UnknownTool",
            f"没有名为「{name}」的工具。"
            f"可用工具：{'、'.join(TOOL_NAMES)}",
        )

    try:

        return handler(file_path, arguments or {})

    except FileNotFoundError:

        return _error("FileNotFound", f"数据文件不存在：{file_path}")

    except Exception as exc:  # noqa: BLE001

        return _error(type(exc).__name__, str(exc))
