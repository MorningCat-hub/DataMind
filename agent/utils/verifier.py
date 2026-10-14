"""
DataMind V4.4
报告数值核对

解决的问题：

    Reporter 是根据 execution_results 写报告的。
    但模型完全可能在报告里写一个「计算结果里根本没有」的数字，
    而且看上去很正常 —— 没有任何环节会发现。

    这是「结果可信度」链条上最关键的缺口：
    代码算对了，报告却可能说错。

做法：

    1. 从报告正文里抽出所有数值
    2. 从 execution_results / chart_results 里抽出「真实存在的数值」
    3. 把常见的合理派生也算进合法集合：
       每组的求和 / 均值 / 最大 / 最小、
       两两之和、两两之差、比值百分比
    4. 逐个核对，列出找不到依据的数值

判定口径：

    命中的叫「可追溯」，
    没命中的叫「无法追溯」—— 而不是「错误」。

    因为报告里的合理推算（合计、占比、同比）
    未必直接出现在计算结果里。

    这个模块只负责「把可疑的地方指出来」，
    判断对不对仍然交给用户。
"""

import bisect
import re
from typing import Any, Dict, List, Optional, Tuple


# ========================================
# 中文数量级单位
# ========================================

CHINESE_UNITS = {
    "千": 1e3,
    "万": 1e4,
    "亿": 1e8,
}


# 带千分位、小数、中文单位、百分号的数值
NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(千|万|亿)?"
    r"\s*(%|％)?"
)


# 日期与周期标签：
# 「2026-01」「2026/1/5」「2026年1月」
# 「2026Q1」「2026-W03」
#
# 这些是一个整体，不应该被拆成若干个数字去核对 ——
# 尤其是 2026Q1：如果被拆出「2026」，
# 它就会混进合法数值池里当噪声。
DATE_RE = re.compile(
    r"\d{4}\s*[-/]\s*W\d{1,2}"
    r"|\d{4}\s*Q[1-4]"
    r"|\d{4}\s*[-/年]\s*\d{1,2}"
    r"(\s*[-/月]\s*\d{1,2})?"
)


# 合法值集合的两两组合上限：
# 超过这个规模就不再生成派生值，避免组合爆炸。
MAX_SOURCE_VALUES = 300


# 百分比反推时，最多试多少个分母。
#
# 每试一个分母要做一次子集求和，所以要限制数量。
MAX_PERCENT_DENOMINATORS = 20


# 做子集求和搜索时最多考虑多少个数。
#
# 用的是折半枚举：n 个数每边只有 2^(n/2)。
#   n = 34 -> 每边 17 位 -> 13 万种组合，毫秒级
#   n = 40 -> 每边 20 位 -> 100 万种组合，明显变慢
#
# 而且只对「第一轮没匹配上」的少数几个数值做这件事。
MAX_SUBSET_VALUES = 34


# 子集求和的绝对容差上限。
#
# 原始数值匹配允许 1% 的相对误差
# （报告常写「约 17.5 万」来替代 175,182），
# 但子集求和是在核对精确算术：
# 如果模型把 175,182 写成 175,195，那是真错了，必须抓出来。
#
# 1% 的相对容差在 17 万这个量级会宽到 1700 多，
# 足以放过明显的算错，所以这里再加一道绝对上限。
MAX_SUBSET_TOLERANCE = 10.0


def _mask_dates(text: str) -> str:
    """
    把日期整体替换成等长的空白占位。

    用等长替换是为了让后面的 match position 仍然对应原文，
    这样截取的上下文还是准的。
    """

    return DATE_RE.sub(
        lambda m: " " * len(m.group(0)),
        text or "",
    )


def _to_value(raw: str, unit: Optional[str]) -> float:
    value = float(raw.replace(",", ""))

    if unit:
        value *= CHINESE_UNITS.get(unit, 1.0)

    return value


def _is_year_mention(
    text: str,
    end: int,
    raw: str,
    unit: Optional[str],
) -> bool:
    """
    判断这个数字是不是在说「年」。

    注意：

    这里必须靠「后面跟着『年』」来判断，
    **绝对不能**靠「1900-2100 的四位数」来判断。

    实测踩过这个坑：
    销售额 1935、6495 都落在这个区间里，
    于是它们被当成「年份」从数据里丢掉，
    而报告里带千分位的 1,935 又不会被当成年份 ——
    结果报告里完全正确的数字被标成「无法追溯」。
    """

    if unit is not None:

        return False

    if "," in raw or "." in raw or len(raw) != 4:

        return False

    following = text[end:end + 2].lstrip()

    return following.startswith("年")


def _is_list_marker(text: str, start: int, raw: str) -> bool:
    """
    行首的「1.」「2、」「3)」是列表编号，不是数据。
    """

    line_start = text.rfind("\n", 0, start) + 1

    if text[line_start:start].strip():
        return False

    following = text[start + len(raw):start + len(raw) + 1]

    return following in (".", "、", ")")


def _is_ordinal(text: str, start: int) -> bool:
    """
    「第 3 步」「第 2 张图表」里的序号。
    """

    return "第" in text[max(0, start - 3):start]


def _snippet(text: str, start: int, end: int, radius: int = 26) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)

    return text[left:right].replace("\n", " ").strip()


def extract_numbers(text: str) -> List[Dict[str, Any]]:
    """
    从正文里抽出所有数值，并标注哪些是「可以忽略」的。

    每一项：

        raw        原文写法
        value      归一化之后的数值（万/亿 已换算）
        percent    是否是百分比
        reason     忽略原因（year / ordinal / list_marker），None 表示需要核对
        context    出现位置附近的原文，方便用户定位
    """

    masked = _mask_dates(text or "")

    claims: List[Dict[str, Any]] = []

    for match in NUMBER_RE.finditer(masked):

        raw, unit, percent = (
            match.group(1),
            match.group(2),
            match.group(3),
        )

        value = _to_value(raw, unit)

        reason = None

        if _is_year_mention(masked, match.end(1), raw, unit):

            reason = "year"

        elif len(raw) <= 3 and _is_ordinal(masked, match.start()):

            reason = "ordinal"

        elif _is_list_marker(masked, match.start(), raw):

            reason = "list_marker"

        claims.append({
            "raw": match.group(0).strip(),
            "value": value,
            "percent": bool(percent),
            "reason": reason,
            "context": _snippet(text or "", match.start(), match.end()),
        })

    return claims


def _numbers_in(value: Any) -> List[float]:
    """
    从一个结果里抽出所有数值。

    结果通常是 pandas 的字符串表示，
    也可能是一段 print 输出。
    """

    if value is None:

        return []

    if isinstance(value, (int, float)) and not isinstance(value, bool):

        return [float(value)]

    text = _mask_dates(str(value))

    numbers: List[float] = []

    for match in NUMBER_RE.finditer(text):

        raw, unit, _percent = (
            match.group(1),
            match.group(2),
            match.group(3),
        )

        # 注意：
        # 这里**不做**年份过滤。
        #
        # 计算结果是数据，里面出现的四位数就是数据本身
        # （销售额 1935、6495 这种）。
        # 日期在 _mask_dates 里已经整体屏蔽掉了，
        # 所以不需要再靠「1900-2100」去猜。
        numbers.append(_to_value(raw, unit))

    return numbers


def collect_source_values(
    execution_results: Optional[List[Dict[str, Any]]],
    chart_results: Optional[List[Dict[str, Any]]],
) -> Tuple[List[float], List[List[float]]]:
    """
    抽出「真实存在的数值」。

    返回：

        (全部数值, 每个执行结果各自的分组)

    分组是为了后面能算「组内求和 / 均值」——
    报告里说「合计 850000」时，
    这个合计往往不在结果里，但它是这一组数值的和。
    """

    values: List[float] = []
    groups: List[List[float]] = []

    for item in execution_results or []:

        if not isinstance(item, dict):

            continue

        data = item.get("data") or {}

        group: List[float] = []

        for key in ("result", "output"):

            group.extend(_numbers_in(data.get(key)))

        if group:

            groups.append(group)

            values.extend(group)

    for chart in chart_results or []:

        if not isinstance(chart, dict):

            continue

        summary = chart.get("data_summary") or {}

        # 图表的数据点数量，报告里经常会被引用
        row_count = summary.get("row_count")

        if (
            isinstance(row_count, (int, float))
            and not isinstance(row_count, bool)
        ):

            values.append(float(row_count))

        for category in summary.get("categories") or []:

            group = _numbers_in(category)

            if group:

                groups.append(group)

                values.extend(group)

    return values, groups


def _all_subset_sums(values: List[float]) -> List[float]:
    """
    枚举一个列表的全部子集和（含空集 0）。

    用增量构造，比 itertools.combinations 快，也不用按长度分层。
    """

    sums = [0.0]

    for value in values:

        sums += [existing + value for existing in sums]

    return sums


def _is_subset_sum(
    values: List[float],
    target: float,
    tolerance_ratio: float = 0.01,
    absolute_floor: float = 0.5,
    absolute_cap: float = MAX_SUBSET_TOLERANCE,
) -> bool:
    """
    判断 target 能不能表示成 values 里若干个数之和。

    为什么需要：

        报告里的「华东合计 57,368」是它三个季度之和，
        「Q2 合计 69,619」是六个地区之和。

        这类边际合计非常常见，
        但它既不在原始结果里，
        也不是「两两相加」能覆盖的 ——
        实测就是因为这个，把两个完全正确的合计标成了「无法追溯」。

    算法：

        折半枚举（meet in the middle）。
        直接枚举 2^n 在 n=34 时不可能，
        折半之后每边只有 2^17 ≈ 13 万，完全可行。

        而且只在「第一轮没匹配上」时才调用，
        正常情况下根本走不到这里。

    顺带的好处：

        如果模型自己加错了，那个数不是任何子集的和，
        于是会被标出来 —— 等于顺便校验了它的算术。

    限制：

        只考虑正数（边际合计都是正数相加），
        且不超过 MAX_SUBSET_VALUES 个数。
    """

    numbers = [value for value in values if value > 0]

    if not numbers or len(numbers) > MAX_SUBSET_VALUES:

        return False

    # 相对 1%，但绝对误差有上限。
    #
    # 默认上限是 MAX_SUBSET_TOLERANCE：
    # 子集求和是在核对精确算术，算错了就该被抓住。
    #
    # 百分比反推时会传入更宽的上限，
    # 因为百分比本身是四舍五入过的
    # （76.90% 换成分子会差十几个），
    # 误差会随分母一起放大。
    tolerance = min(
        max(
            absolute_floor,
            abs(target) * tolerance_ratio,
        ),
        absolute_cap,
    )

    half = len(numbers) // 2

    left = _all_subset_sums(numbers[:half])

    right = sorted(_all_subset_sums(numbers[half:]))

    for value in left:

        remaining = target - value

        position = bisect.bisect_left(right, remaining)

        for index in (position - 1, position):

            if 0 <= index < len(right):

                if abs(right[index] - remaining) <= tolerance:

                    return True

    return False


def _is_percentage_of_something(
    pool: List[float],
    percent: float,
    denominators: List[float],
) -> bool:
    """
    百分比反推。

    报告里的「占比 76.90%」来自 134701 / 175182，
    而 134701 是三个地区之和 ——
    单独看这个百分比，它不在任何结果里，
    但把分子算出来就会发现它是合法的。

    所以：

        分子 = 百分比 / 100 * 分母

    再判断分子是不是若干个结果值之和。

    容差要跟着分母放大：
    百分比被四舍五入到两位之后，
    换算回分子会产生和分母同量级的误差。
    """

    for denominator in denominators:

        if not denominator:

            continue

        numerator = percent / 100 * denominator

        if not numerator:

            continue

        if _is_subset_sum(
            pool,
            numerator,
            absolute_cap=max(
                MAX_SUBSET_TOLERANCE,
                abs(denominator) * 0.001,
            ),
        ):

            return True

    return False


def build_derived_values(
    values: List[float],
    groups: List[List[float]],
) -> List[float]:
    """
    报告里合理的推算也要算作「可追溯」。

    包括：

        组内求和 / 均值 / 最大 / 最小
        两两之和、两两之差
        比值百分比
    """

    derived: List[float] = []

    for group in groups:

        if len(group) >= 2:

            total = sum(group)

            derived.append(total)
            derived.append(total / len(group))
            derived.append(max(group))
            derived.append(min(group))

            # 占比：
            # 「华东占比 33%」这种说法要和「组内值 / 组内总和」对上，
            # 只靠两两比值是算不出来的（总和本身不在原始值里）。
            if total:

                for value in group:

                    derived.append(value / total * 100)

    if len(values) > MAX_SOURCE_VALUES:

        # 数值太多时放弃两两组合，避免组合爆炸
        return derived

    for index, first in enumerate(values):

        for second in values[index + 1:]:

            derived.append(first + second)
            derived.append(first - second)
            derived.append(second - first)

            if second:

                derived.append(first / second * 100)

                # 「是第二名的 1.22 倍」用的是不带百分号的比值
                derived.append(first / second)

            if first:

                derived.append(second / first * 100)

                derived.append(second / first)

    return derived


class ValueIndex:
    """
    带容差的数值查询。

    报告里的数值经常是四舍五入过的
    （175182 写成 17.5 万），
    所以必须允许一定误差。
    """

    def __init__(
        self,
        values: List[float],
        tolerance_ratio: float = 0.01,
        absolute_floor: float = 0.5,
    ):

        self._values = sorted(set(values))

        self._tolerance_ratio = tolerance_ratio
        self._absolute_floor = absolute_floor

    def __len__(self) -> int:

        return len(self._values)

    def matches(self, target: float) -> bool:
        """
        目标值能否在合法集合里找到对应（允许容差）。
        """

        if not self._values:

            return False

        tolerance = max(
            self._absolute_floor,
            abs(target) * self._tolerance_ratio,
        )

        position = bisect.bisect_left(self._values, target)

        for candidate_index in (position - 1, position):

            if 0 <= candidate_index < len(self._values):

                candidate = self._values[candidate_index]

                if abs(candidate - target) <= tolerance:

                    return True

        return False


def verify_report_numbers(
    report: str,
    execution_results: Optional[List[Dict[str, Any]]] = None,
    chart_results: Optional[List[Dict[str, Any]]] = None,
    max_unmatched: int = 12,
) -> Dict[str, Any]:
    """
    核对报告里的数值。

    返回：

        checked           需要核对的数值个数
        matched           能追溯到的个数
        unmatched         追溯不到的明细（最多 max_unmatched 条）
        unmatched_count   追溯不到的总数
        source_count      results 里真实存在的数值个数
        passed            是否全部可追溯
    """

    claims = [
        claim
        for claim in extract_numbers(report or "")
        if claim["reason"] is None
    ]

    values, groups = collect_source_values(
        execution_results,
        chart_results,
    )

    index = ValueIndex(
        values + build_derived_values(values, groups)
    )

    matched = 0
    unmatched: List[Dict[str, Any]] = []

    # 子集求和搜索用的数值池。
    #
    # 这里必须去重：
    #
    # 同一张结果表经常被打印两次，
    # describe_dataset 的输出里还有行数、唯一值数、首行样例……
    # 这些重复值会把池子撑到超过 MAX_SUBSET_VALUES，
    # 于是子集求和直接放弃，正确的边际合计反被标成「无法追溯」。
    # （实测踩过：月度的 135,192 和季度的 39,990 就是这么被误报的。）
    #
    # 而且边际合计本来就是对一组「数字」求和，
    # 不需要同一个数出现两次。
    pool = sorted({
        value for value in values if value > 0
    })

    # 百分比反推用的分母：
    # 原始数值 + 每个分组的总和
    denominators = [
        value for value in values if value
    ] + [
        sum(group) for group in groups if len(group) >= 2
    ]

    denominators = sorted(
        set(denominators),
        reverse=True,
    )[:MAX_PERCENT_DENOMINATORS]

    for claim in claims:

        if index.matches(claim["value"]):

            matched += 1

            continue

        # ====================================
        # 第一轮没匹配上，再给一次机会：
        # 它是不是若干结果值之和？
        #
        # 「地区合计」「季度合计」这类边际合计
        # 就是把一组明细加起来得到的，
        # 两两求和覆盖不到，但它完全正确。
        # ====================================

        if _is_subset_sum(pool, claim["value"]):

            matched += 1

            continue

        # ====================================
        # 百分比再给一次机会：
        #
        # 「占比 76.90%」← 134701 / 175182
        # 分子往往是一堆数相加得到的，
        # 单独看它不在任何结果里。
        # ====================================

        if claim.get("percent") and _is_percentage_of_something(
            pool,
            claim["value"],
            denominators,
        ):

            matched += 1

            continue

        unmatched.append(claim)

    return {
        "checked": len(claims),
        "matched": matched,
        "unmatched": unmatched[:max_unmatched],
        "unmatched_count": len(unmatched),
        "source_count": len(values),
        "passed": not unmatched,
    }


def build_verification_notice(
    verification: Dict[str, Any],
) -> str:
    """
    生成追加到报告末尾的核对提示。

    这段由程序生成，不经过模型，
    所以内容一定和核对结果一致。
    """

    if not verification or verification.get("passed"):

        return ""

    lines = [
        "",
        "---",
        "",
        "## ⚠️ 数值核对提示",
        "",
        "以下数值没有在本次的计算结果中找到对应依据，请核对：",
        "",
    ]

    for claim in verification.get("unmatched") or []:

        lines.append(
            f"- `{claim.get('raw')}`"
            f"（出现在：{claim.get('context')}）"
        )

    remaining = (
        verification.get("unmatched_count", 0)
        - len(verification.get("unmatched") or [])
    )

    if remaining > 0:

        lines.append(f"- 另有 {remaining} 处未列出")

    lines.extend([
        "",
        f"其余 {verification.get('matched', 0)} 个数值"
        "均可与计算结果对应。",
        "",
        "注意：报告里的合计、占比、同比等推算未必直接出现在计算结果中，",
        "此提示只是标记出无法自动追溯的数值，不代表结论一定错误。",
        "",
    ])

    return "\n".join(lines)
