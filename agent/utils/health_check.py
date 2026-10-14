"""
DataMind V4.11
数据体检

解决的问题：

    通用 Agent 拿到数据就直接算，然后给你一个**自信的错误结论**。
    例如「12 月销售额环比下降 70%」——
    而实际上 12 月只覆盖了 9 天，那个比较根本不成立。

    这不是模型不够聪明，是它**不知道这份数据的限制**。

做法：

    在分析之前用**确定性规则**（纯 pandas，不经过模型）体检数据，
    产出「发现项」（finding），每项带严重度：

        block  结论不能下 —— 报告若基于它下结论就是错的
        warn   可以下，但必须声明前提
        info   值得注意

    发现项会：

    1. 注入 Planner / Executor 的提示词（先知道有什么坑）
    2. 注入 Reporter 的提示词（必须如实披露）
    3. 交给结论核对器，在报告生成后**程序化**检查有没有踩坑

为什么必须是确定性的：

    体检结论要用来**拦截报告里的错误结论**。
    如果体检本身是模型判断的，那它和报告一样可能出错，
    用它去拦报告就成了拿一个猜测去校验另一个猜测。
"""

import os
import statistics
from typing import Any, Dict, List, Optional

import pandas as pd


# 严重度
SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


# 缺失比例超过这个值，就认为该维度不足以支撑「按它拆分」的分析
MISSING_DIMENSION_THRESHOLD = 0.05

# 单一取值占比超过这个值，认为跨该维度对比的结论很脆弱
CONCENTRATION_THRESHOLD = 0.80

# 最后一个期间的覆盖天数低于此前典型值的这个比例，就认为期间不完整
PARTIAL_PERIOD_RATIO = 0.8

# 行数太少就不做这些检查了，避免小样本噪声
MIN_ROWS_FOR_CHECKS = 20

# 一列里有这个比例的取值能解析成日期，就认为它是日期列
DATE_PARSE_THRESHOLD = 0.9

# 判断「这一列是不是日期」时只看前面这么多行，避免大表上反复全量解析
DATE_DETECT_SAMPLE = 5000


# ========================================
# 列类型判断
# ========================================

def _parse_dates(series: pd.Series) -> pd.Series:
    """
    尽量把一列解析成日期。
    """

    if pd.api.types.is_datetime64_any_dtype(series):

        return series

    if pd.api.types.is_numeric_dtype(series):

        # 数值列不能被当成日期：
        # pandas 会把整数当 1970 年的纳秒时间戳
        return pd.Series(
            pd.NaT,
            index=series.index,
            dtype="datetime64[ns]",
        )

    try:

        return pd.to_datetime(series, errors="coerce", format="mixed")

    except TypeError:

        return pd.to_datetime(series, errors="coerce")


def _looks_like_dates(series: pd.Series) -> bool:
    """
    抽样判断这一列是不是日期。

    注意（实测踩过）：

        项目里的数据一律会被**归一化成 CSV** 再进来，
        所以日期列大概率是**字符串**（例如 "2011-12-01 08:26:00"），
        而不是 datetime 类型。

        如果只认 datetime 类型，
        「期间不完整」这个最重要的检查就永远不会触发。
    """

    if pd.api.types.is_datetime64_any_dtype(series):

        return True

    if pd.api.types.is_numeric_dtype(series):

        return False

    sample = series.dropna()

    if sample.empty:

        return False

    sample = sample.head(DATE_DETECT_SAMPLE)

    parsed = _parse_dates(sample)

    rate = parsed.notna().sum() / max(len(sample), 1)

    return rate >= DATE_PARSE_THRESHOLD and parsed.nunique() >= 3


def _date_columns(df: pd.DataFrame) -> List[str]:

    return [
        str(column)
        for column in df.columns
        if _looks_like_dates(df[column])
    ]


def _numeric_columns(df: pd.DataFrame) -> List[str]:

    return [
        str(column)
        for column in df.columns
        if pd.api.types.is_numeric_dtype(df[column])
        and not pd.api.types.is_bool_dtype(df[column])
    ]


def _label_columns(df: pd.DataFrame) -> List[str]:

    return [
        str(column)
        for column in df.columns
        if not pd.api.types.is_numeric_dtype(df[column])
        and not pd.api.types.is_datetime64_any_dtype(df[column])
    ]


def _looks_like_identifier(name: str) -> bool:

    lowered = name.lower()

    return any(
        token in lowered
        for token in ("id", "customer", "client", "user", "客户", "用户", "会员")
    )


def _looks_like_price(name: str) -> bool:

    lowered = name.lower()

    return any(
        token in lowered
        for token in ("price", "amount", "revenue", "sales", "price",
                      "单价", "金额", "销售额", "价格")
    )


def _looks_like_quantity(name: str) -> bool:

    lowered = name.lower()

    return any(
        token in lowered
        for token in ("quantity", "qty", "数量", "件数", "销量")
    )


# ========================================
# 各项检查
# ========================================

def check_partial_period(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查最后一个自然月是不是不完整的。

    这是最容易被误读成「暴跌」的坑：
    数据往往截到某个月的中途，而模型会把它当成一个完整月份去比较。
    """

    findings: List[Dict[str, Any]] = []

    for column in _date_columns(df):

        dates = _parse_dates(df[column])

        if dates.isna().all() or dates.notna().sum() < MIN_ROWS_FOR_CHECKS:

            continue

        periods = dates.dt.to_period("M")

        counts = periods.value_counts().sort_index()

        if len(counts) < 3:

            continue

        coverage: Dict[Any, int] = {}

        bounds: Dict[Any, Any] = {}

        for period in counts.index:

            inside = dates[periods == period]

            coverage[period] = int((inside.max() - inside.min()).days) + 1

            bounds[period] = (inside.min(), inside.max())

        last = counts.index[-1]

        previous = [coverage[item] for item in counts.index[:-1]]

        typical = statistics.median(previous)

        last_span = coverage[last]

        if not typical or last_span >= typical * PARTIAL_PERIOD_RATIO:

            continue

        start, end = bounds[last]

        findings.append({
            "id": "partial_last_period",
            "kind": "partial_period",
            "severity": SEVERITY_BLOCK,
            "title": "最后一个期间不完整（{} 只覆盖 {} 天）".format(
                last, last_span
            ),
            "detail": (
                "字段「{}」的最后一个自然月 {} 只覆盖 {} 天"
                "（{} ~ {}，{} 行），"
                "而此前各月的典型覆盖天数是 {:.0f} 天。"
                "因此任何把 {} 与完整月份相比的结论都不可比。"
            ).format(
                column, last, last_span,
                start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                int(counts[last]), typical, last,
            ),
            "evidence": {
                "column": column,
                "period": str(last),
                "span_days": last_span,
                "typical_span_days": typical,
                "rows": int(counts[last]),
            },
            "period_label": str(last),
            "period_column": column,
        })

    return findings


def check_negative_values(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查负值（退货/冲销）。

    有退货的数据里，「销售额合计」有两个都说得通的口径：
    含退货的毛额，和扣除退货的净额。报告必须先声明用的是哪个。
    """

    findings: List[Dict[str, Any]] = []

    for column in _numeric_columns(df):

        negatives = df[df[column] < 0]

        if negatives.empty:

            continue

        share = len(negatives) / max(len(df), 1)

        if len(negatives) < 5 or share < 0.0005:

            continue

        hints: List[str] = []

        for candidate in _label_columns(df):

            name = str(candidate)

            # 只看「像单号/客户号」的列：
            # 否则任何一段文本只要以 C 开头都会被报成「取消/退货」，
            # 那是纯噪声（实测 Description / Country 都被误报过）
            if not (
                _looks_like_identifier(name)
                or any(
                    token in name.lower()
                    for token in ("invoice", "order", "发票", "单号", "订单")
                )
            ):

                continue

            marked = (
                negatives[candidate]
                .astype(str)
                .str.upper()
                .str.startswith("C")
                .sum()
            )

            if marked:

                hints.append(
                    "其中 {} 行的「{}」以 C 开头（取消/退货）".format(
                        int(marked), name
                    )
                )

        findings.append({
            "id": "negative_values:{}".format(column),
            "kind": "negative_values",
            "severity": SEVERITY_BLOCK,
            "title": "「{}」存在负值（{} 行）".format(
                column, format(len(negatives), ",")
            ),
            "detail": (
                "字段「{}」有 {} 行是负数，占 {:.1f}%。{}"
                "这意味着「{}」的合计存在两种口径："
                "含退货的毛额，与扣除退货的净额。"
                "结论必须先声明用的是哪一种，否则数字没有意义。"
            ).format(
                column, format(len(negatives), ","), share * 100,
                "；".join(hints) + "。" if hints else "",
                column,
            ),
            "evidence": {
                "column": column,
                "negative_rows": int(len(negatives)),
                "share": round(share, 4),
                "cancel_hints": hints,
            },
            "metric_column": column,
        })

    return findings


def check_missing_key_dimension(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查关键维度（客户/用户）的缺失。

    做复购率、留存率这类分析时，缺失客户 ID 的行根本没被算进去，
    但报告往往不会提这一点，读者会以为覆盖率是 100%。
    """

    findings: List[Dict[str, Any]] = []

    for column in df.columns:

        name = str(column)

        if not _looks_like_identifier(name):

            continue

        missing = int(df[column].isna().sum())

        if not missing:

            continue

        share = missing / max(len(df), 1)

        if share < MISSING_DIMENSION_THRESHOLD:

            continue

        findings.append({
            "id": "missing_dimension:{}".format(name),
            "kind": "missing_dimension",
            "severity": SEVERITY_WARN,
            "title": "「{}」缺失 {:.1f}%".format(name, share * 100),
            "detail": (
                "字段「{}」缺失 {} 行（{:.1f}%）。"
                "任何按客户拆分、复购率、留存率一类的分析，"
                "都只能覆盖剩下 {:.1f}% 的数据；"
                "报告必须声明这个覆盖率，否则读者会以为是全量。"
            ).format(name, format(missing, ","), share * 100, (1 - share) * 100),
            "evidence": {
                "column": name,
                "missing_rows": missing,
                "share": round(share, 4),
            },
        })

    return findings


def check_non_positive_price(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查单价/金额的零值与负值（赠品、调整项）。
    """

    findings: List[Dict[str, Any]] = []

    for column in _numeric_columns(df):

        if not _looks_like_price(column):

            continue

        bad = df[df[column] <= 0]

        if len(bad) < 5:

            continue

        zeros = int((df[column] == 0).sum())

        negatives = int((df[column] < 0).sum())

        findings.append({
            "id": "non_positive_price:{}".format(column),
            "kind": "non_positive_price",
            "severity": SEVERITY_WARN,
            "title": "「{}」有 {} 行 <= 0".format(column, format(len(bad), ",")),
            "detail": (
                "字段「{}」有 {} 行小于等于 0（其中等于 0 的 {} 行，"
                "小于 0 的 {} 行），通常代表赠品、内部调整或数据录入问题。"
                "涉及收入/价格的计算需要说明如何处理这些行。"
            ).format(column, format(len(bad), ","), format(zeros, ","),
                     format(negatives, ",")),
            "evidence": {
                "column": column,
                "zero_rows": zeros,
                "negative_rows": negatives,
            },
        })

    return findings


def check_dimension_concentration(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查维度的集中度。

    如果某个取值占了绝大多数，那么「跨该维度对比」的结论会很脆弱：
    剩下的成员样本量太小，波动大，不能拿来下结论。
    """

    findings: List[Dict[str, Any]] = []

    total = max(len(df), 1)

    for column in _label_columns(df):

        name = str(column)

        if _looks_like_identifier(name):

            continue

        counts = df[column].value_counts(dropna=True)

        if counts.empty or len(counts) < 2 or len(counts) > 200:

            continue

        top_value = counts.index[0]

        top_share = counts.iloc[0] / total

        if top_share < CONCENTRATION_THRESHOLD:

            continue

        findings.append({
            "id": "concentration:{}".format(name),
            "kind": "concentration",
            "severity": SEVERITY_WARN,
            "title": "「{}」高度集中（「{}」占 {:.1f}%）".format(
                name, top_value, top_share * 100
            ),
            "detail": (
                "字段「{}」有 {} 个取值，但「{}」一个就占了 {:.1f}%。"
                "跨「{}」做对比时，其余成员合计只有 {:.1f}%，"
                "样本量差距很大，相关结论需要谨慎。"
            ).format(name, len(counts), top_value, top_share * 100,
                     name, (1 - top_share) * 100),
            "evidence": {
                "column": name,
                "top_value": str(top_value),
                "top_share": round(float(top_share), 4),
                "distinct": int(len(counts)),
            },
        })

    return findings


def check_empty_or_constant_column(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    检查全空或恒定值的列：这些列不能用来做任何对比。
    """

    findings: List[Dict[str, Any]] = []

    for column in df.columns:

        name = str(column)

        series = df[column]

        if series.isna().all():

            findings.append({
                "id": "empty_column:{}".format(name),
                "kind": "empty_column",
                "severity": SEVERITY_WARN,
                "title": "「{}」整列为空".format(name),
                "detail": "字段「{}」全部为空，无法用于任何分析。".format(name),
                "evidence": {"column": name},
            })

        elif series.nunique(dropna=True) <= 1:

            findings.append({
                "id": "constant_column:{}".format(name),
                "kind": "constant_column",
                "severity": SEVERITY_INFO,
                "title": "「{}」只有一个取值".format(name),
                "detail": (
                    "字段「{}」只有一个取值，无法用于分组对比。".format(name)
                ),
                "evidence": {"column": name},
            })

    return findings


# ========================================
# 汇总
# ========================================

CHECKS = (
    check_partial_period,
    check_negative_values,
    check_missing_key_dimension,
    check_non_positive_price,
    check_dimension_concentration,
    check_empty_or_constant_column,
)

SEVERITY_ORDER = {
    SEVERITY_BLOCK: 0,
    SEVERITY_WARN: 1,
    SEVERITY_INFO: 2,
}


def run_health_checks(
    file_path: str,
    max_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """
    跑一遍全部体检。

    注意（实测踩过）：

        这里**默认不截断行数**。

        数据通常按时间排序，「期间不完整」这个最重要的坑
        **恰好只存在于数据的尾部**。
        如果只看前 N 行，最后那个不完整的月份根本不会进入体检范围，
        最重要的检查就永远不会触发。

        几十万行对 pandas 来说没有压力，
        所以默认读全量；调用方确实需要限流时再显式传 max_rows。
    """

    if not file_path or not os.path.isfile(file_path):

        return {
            "findings": [],
            "summary": "",
            "ok": False,
            "error": "文件不存在：{}".format(file_path),
        }

    try:

        df = pd.read_csv(file_path, nrows=max_rows)

    except Exception as exc:  # noqa: BLE001

        return {
            "findings": [],
            "summary": "",
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
        }

    findings: List[Dict[str, Any]] = []

    for check in CHECKS:

        try:

            findings.extend(check(df))

        except Exception:  # noqa: BLE001

            # 单项检查失败不该让体检整体挂掉
            continue

    findings.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 9))

    blocks = [item for item in findings if item["severity"] == SEVERITY_BLOCK]

    warns = [item for item in findings if item["severity"] == SEVERITY_WARN]

    if blocks:

        summary = "发现 {} 个必须先声明/规避的问题，{} 个需要注意".format(
            len(blocks), len(warns)
        )

    elif warns:

        summary = "发现 {} 个需要注意的数据问题".format(len(warns))

    else:

        summary = "没有发现明显的数据质量问题"

    return {
        "findings": findings,
        "summary": summary,
        "ok": True,
        "error": None,
        "rows": int(len(df)),
    }


def format_health_block(findings: List[Dict[str, Any]]) -> str:
    """
    渲染成注入提示词的文本。

    Planner / Executor / Reporter 共用同一份，避免口径不一致。
    """

    if not findings:

        return ""

    lines = [
        "====================",
        "数据体检结论（必须在分析中遵守）",
        "====================",
        "",
    ]

    for item in findings:

        label = {
            SEVERITY_BLOCK: "【必须规避】",
            SEVERITY_WARN: "【需要声明】",
            SEVERITY_INFO: "【仅供参考】",
        }.get(item["severity"], "")

        lines.append("{}{}".format(label, item["title"]))
        lines.append("  " + item["detail"])
        lines.append("")

    lines.extend([
        "注意：",
        "1. 【必须规避】的问题会直接让相关结论不成立；"
        "如果用户的要求触及它，要在报告里明确说明不能这样比，"
        "并给出可行的替代口径（例如改用日均、或排除不完整期间）。",
        "2. 【需要声明】的问题不阻止分析，但报告必须写明前提和覆盖率。",
        "3. 不要凭空猜测数据限制，只依据上面列出的内容。",
    ])

    return "\n".join(lines)


def format_health_notice(findings: List[Dict[str, Any]]) -> str:
    """
    渲染成面向用户的一段说明（挂在报告前）。
    """

    blocking = [
        item for item in findings if item["severity"] == SEVERITY_BLOCK
    ]

    if not blocking:

        return ""

    lines = [
        "> **数据体检提示**：本次分析的数据存在以下限制，结论已相应处理。",
        ">",
    ]

    for item in blocking:

        lines.append("> - {}".format(item["title"]))

    return "\n".join(lines)


def summarize_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    给接口和前端用的精简信息。
    """

    return [
        {
            "id": item["id"],
            "kind": item.get("kind"),
            "severity": item["severity"],
            "title": item["title"],
            "detail": item["detail"],
        }
        for item in findings
    ]
