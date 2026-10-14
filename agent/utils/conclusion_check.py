"""
DataMind V4.12
结论级核对

解决的问题：

    数值核对器检查的是「报告里的数字能不能追溯到计算结果」。
    但还有一类错误它抓不到：

        数字全都对，**结论却是错的**。

    例如在真实数据上：

        体检发现：2011-12 只覆盖 9 天（完整月份约 30 天）
        报告说：  「12 月销售额环比下降 70%」

        数字确实是从计算结果里来的，但那个比较根本不成立。

    通用 Agent 做不到这件事 —— 它根本不知道 12 月是短的。

做法：

    把数据体检里 severity = block 的发现项，逐条变成**可程序检查的规则**，
    在报告生成之后检查有没有踩坑。踩了就：

    1. 在 trace 里标黄
    2. 由**程序**在报告末尾追加纠正说明（不经过模型）

设计原则（很重要）：

    **保守**。一句话只有在「做了比较」且「没有自己声明这个限制」时，
    才算违规。

    否则它会去指责报告里那句本来正确的声明 ——
    「12 月数据不完整，不可比」是**正确**的披露，不该被拦。
"""

import re
from typing import Any, Dict, List, Optional


# ========================================
# 词表
# ========================================

# 表示「做了比较」
COMPARISON_WORDS = (
    "环比", "同比", "相比", "对比", "较上", "较去",
    "下降", "上升", "增长", "减少", "回落", "暴跌", "骤降",
    "下滑", "上涨", "涨幅", "降幅", "变化",
)

# 表示「已经声明了这个限制」——出现这些词就不算违规
ACKNOWLEDGEMENT_WORDS = (
    "不完整", "仅覆盖", "只覆盖", "只有", "仅有", "不可比", "不宜",
    "不能直接比", "无法直接比", "数据限制", "已按", "已排除", "已剔除",
    "剔除", "日均", "仅供参考", "口径",
)

# 表示「声明了收入/数量的口径」
CALIBER_WORDS = (
    "净额", "净销售", "扣", "排除", "剔除", "毛额", "含退货", "不含退货",
    "含取消", "剔除退货", "口径",
)

# 表示「做了客户维度的留存/复购分析」
CUSTOMER_ANALYSIS_WORDS = (
    "复购", "回购", "留存", "retention", "重复购买", "会员",
)

# 表示「声明了覆盖率」
COVERAGE_WORDS = (
    "覆盖", "缺失", "仅", "只有", "占比", "有效客户", "%的数据",
)

TOTAL_WORDS = (
    "合计", "总计", "总额", "总共", "总和", "累计",
)


# ========================================
# 工具
# ========================================

def iter_claims(report: str):
    """
    把报告切成（句子, 上下文）对。

    上下文用在 Markdown 表格行上：

        | 月份 | 环比 |
        |---|---|
        | 2011-12 | -70% |

    数据行里没有「环比」两个字，比较字样在**表头**里。
    而表格里放一个不可比的百分比，恰恰是违规最常见的表现形式，
    所以表格行要继承它表头的字样一起去判断。
    """

    lines = (report or "").splitlines()

    header = ""

    pending_header = ""

    out = []

    for index, raw in enumerate(lines):

        line = raw.strip()

        if not line:

            continue

        if line.startswith("|"):

            # 分隔行（|---|---|）之前的那个 | 行是表头
            if set(line.replace("|", "").replace(" ", "")) <= {"-", ":"}:

                header = pending_header

                continue

            if not header:

                pending_header = line

            out.append((line, header))

            continue

        # 表格结束
        header = ""

        pending_header = ""

        for piece in re.split(r"[。！？；!?;]", line):

            piece = piece.strip()

            if piece:

                out.append((piece, ""))

    return out


def split_sentences(report: str) -> List[str]:
    """
    把报告切成句子（只要文本，丢掉上下文）。
    """

    return [text for text, _context in iter_claims(report)]


def _has_number(text: str) -> bool:

    return bool(re.search(r"\d", text))


def _has_percent(text: str) -> bool:

    return bool(re.search(r"%|％|个?百分点|成", text))


def _acknowledged(text: str) -> bool:

    return any(word in text for word in ACKNOWLEDGEMENT_WORDS)


def period_variants(label: str) -> List[str]:
    """
    一个期间标签在报告里可能的写法。

    "2011-12" -> 2011-12 / 2011年12月 / 2011/12 / 12 月
    """

    variants = {label}

    match = re.match(r"(\d{4})[-/年](\d{1,2})", label)

    if match:

        year, month = match.group(1), int(match.group(2))

        variants.update({
            "{}-{:02d}".format(year, month),
            "{}/{}".format(year, month),
            "{}年{}月".format(year, month),
            "{} 年 {} 月".format(year, month),
            "{}月".format(month),
            "{} 月".format(month),
        })

    return sorted(variants, key=len, reverse=True)


# ========================================
# 规则
# ========================================

def check_partial_period(report: str, finding: Dict[str, Any]):
    """
    期间不完整时，不许拿它和完整期间做比较。
    """

    label = finding.get("period_label")

    if not label:

        return []

    violations = []

    for sentence, context in iter_claims(report):

        combined = sentence + " " + (context or "")

        if not any(variant in sentence for variant in period_variants(label)):

            continue

        # 比较字样也可能在表头里（「| 月份 | 环比 |」）
        if not any(word in combined for word in COMPARISON_WORDS):

            continue

        # 已经自己声明了这个限制 -> 这是正确披露，不是违规
        if _acknowledged(sentence):

            continue

        # 比较类结论一般带数字或百分比
        if not (_has_percent(sentence) or _has_number(sentence)):

            continue

        violations.append({
            "finding_id": finding.get("id"),
            "kind": "partial_period",
            "severity": "block",
            "claim": sentence[:220],
            "reason": (
                "{} 只覆盖 {} 天，不是完整期间（{}）。"
                "把它与完整期间做比较，结论不成立。"
            ).format(
                label,
                finding.get("evidence", {}).get("span_days", "?"),
                finding.get("evidence", {}).get("typical_span_days", "?"),
            ),
            "suggestion": (
                "改用日均口径，或把 {} 排除在比较范围之外，"
                "或明确标注该期间不完整后再给出结论。"
            ).format(label),
        })

    return violations


def check_undeclared_caliber(report: str, finding: Dict[str, Any]):
    """
    有退货/负值的数据上给了总额，却通篇没声明用的是哪个口径。

    这是**整篇报告级**的检查（只报一次），不是逐句检查 ——
    逐句判断「这句话有没有声明口径」太容易误报。
    """

    column = finding.get("evidence", {}).get("column") or finding.get(
        "metric_column"
    )

    has_total = any(word in report for word in TOTAL_WORDS)

    has_number = _has_number(report)

    if not (has_total and has_number):

        return []

    if any(word in report for word in CALIBER_WORDS):

        return []

    return [{
        "finding_id": finding.get("id"),
        "kind": "undeclared_caliber",
        "severity": "block",
        "claim": "报告给出了总额，但没有声明口径",
        "reason": (
            "数据里「{}」存在负值（退货/冲销 {} 行），"
            "合计因此有两种口径：含退货的毛额，和扣除退货的净额。"
            "报告通篇没有声明用的是哪一种。"
        ).format(
            column,
            format(
                finding.get("evidence", {}).get("negative_rows", 0), ","
            ),
        ),
        "suggestion": (
            "在结论里明确写出「本次为扣除退货后的净额」或"
            "「本次为含退货的毛额」。"
        ),
    }]


def check_customer_coverage(report: str, finding: Dict[str, Any]):
    """
    客户维度缺失严重时，做复购/留存分析必须声明覆盖率。
    """

    if not any(word in report for word in CUSTOMER_ANALYSIS_WORDS):

        return []

    if any(word in report for word in COVERAGE_WORDS):

        return []

    column = finding.get("evidence", {}).get("column")

    return [{
        "finding_id": finding.get("id"),
        "kind": "undeclared_coverage",
        "severity": "warn",
        "claim": "报告做了客户维度分析，但没有声明覆盖率",
        "reason": (
            "字段「{}」缺失 {:.1f}%，"
            "客户维度的结论只能覆盖其余部分，"
            "报告必须声明覆盖率。"
        ).format(
            column,
            (finding.get("evidence", {}).get("share") or 0) * 100,
        ),
        "suggestion": (
            "在结论里写明「本分析仅覆盖有客户 ID 的 {:.1f}% 数据」。"
        ).format(
            (1 - (finding.get("evidence", {}).get("share") or 0)) * 100
        ),
    }]


RULES = {
    "partial_period": check_partial_period,
    "negative_values": check_undeclared_caliber,
    "missing_dimension": check_customer_coverage,
}


# ========================================
# 入口
# ========================================

def check_conclusions(
    report: str,
    findings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    核对报告结论有没有和数据限制冲突。

    返回：

        checked     参与核对的发现项数量
        violations  违规明细
        passed      是否全部通过
        notice      追加到报告末尾的纠正说明（由程序生成）
    """

    blocking = [
        item for item in (findings or [])
        if item.get("severity") == "block"
    ]

    relevant = [
        item for item in (findings or [])
        if item.get("kind") in RULES
    ]

    violations: List[Dict[str, Any]] = []

    for finding in relevant:

        rule = RULES.get(finding.get("kind"))

        if rule is None:

            continue

        try:

            violations.extend(rule(report or "", finding))

        except Exception:  # noqa: BLE001

            # 单条规则出错不该让整个核对挂掉
            continue

    # 去重（同一句话可能被多条规则命中）
    seen = set()

    unique = []

    for item in violations:

        key = (item["kind"], item["claim"])

        if key in seen:

            continue

        seen.add(key)

        unique.append(item)

    return {
        "checked": len(relevant),
        "blocking_findings": len(blocking),
        "violations": unique,
        "violation_count": len(unique),
        "passed": not unique,
        "notice": build_conclusion_notice(unique),
    }


def build_conclusion_notice(violations: List[Dict[str, Any]]) -> str:
    """
    生成追加到报告末尾的纠正说明。

    这段由程序生成，不经过模型，
    所以它一定和核对结果一致。
    """

    if not violations:

        return ""

    lines = [
        "",
        "---",
        "",
        "## ⚠️ 结论核对提示",
        "",
        "以下结论与本次数据的已知限制冲突，由系统自动标出：",
        "",
    ]

    for item in violations:

        lines.append("- **{}**".format(item["reason"]))

        lines.append("  - 报告原文：`{}`".format(item["claim"]))

        lines.append("  - 建议：{}".format(item["suggestion"]))

        lines.append("")

    lines.append(
        "这段提示由程序生成，不经过模型，"
        "所以它一定和体检结果一致。"
    )

    lines.append("")

    return "\n".join(lines)


def summarize_conclusions(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    给接口和前端用的精简信息。
    """

    return {
        "passed": bool(result.get("passed")),
        "checked": int(result.get("checked") or 0),
        "violation_count": int(result.get("violation_count") or 0),
        "kinds": sorted({
            item["kind"] for item in result.get("violations") or []
        }),
        "violations": [
            {
                "kind": item["kind"],
                "severity": item["severity"],
                "claim": item["claim"],
                "reason": item["reason"],
            }
            for item in result.get("violations") or []
        ],
    }
