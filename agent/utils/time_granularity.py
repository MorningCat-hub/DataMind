"""
DataMind V3.7
时间粒度工具

职责：

1. 统一「时间粒度」的取值与别名归一化
2. 从图表标题 / 说明里识别明确的时间关键词
3. 在字段确实是日期、但用户没有明确说粒度时，
   根据数据跨度自动选择一个「x 轴标签还读得清」的粒度

支持：

    year / quarter / month / week / day

为什么单独抽成一个模块：

    V3.6 之前，

    - Chart Planner 的 prompt 和校验都不带 time_granularity，
      所以「显式声明粒度」这条路径是死的；
    - Chart Executor 只能靠标题关键词猜，
      标题里出现「季度」但 x 轴不是日期字段时，整张图直接失败。

    归一化和推断逻辑集中在这里，两边共用。
"""

from typing import Optional


# ========================================
# 支持的粒度
# ========================================

SUPPORTED_GRANULARITIES = (
    "year",
    "quarter",
    "month",
    "week",
    "day",
)


# ========================================
# 别名归一化
# ========================================

_ALIASES = {
    "year": "year",
    "y": "year",
    "annual": "year",
    "yearly": "year",
    "年": "year",
    "年度": "year",
    "按年": "year",

    "quarter": "quarter",
    "q": "quarter",
    "quarterly": "quarter",
    "季": "quarter",
    "季度": "quarter",
    "按季": "quarter",

    "month": "month",
    "m": "month",
    "monthly": "month",
    "月": "month",
    "月份": "month",
    "月度": "month",
    "按月": "month",

    "week": "week",
    "w": "week",
    "weekly": "week",
    "周": "week",
    "周度": "week",
    "按周": "week",

    "day": "day",
    "d": "day",
    "daily": "day",
    "日": "day",
    "日期": "day",
    "按日": "day",
}


# ========================================
# 明确的时间关键词
#
# 注意：
# 「周期」「趋势」「变化」这类模糊说法故意不放在这里，
# 它们交给 select_by_span() 按真实数据跨度决定。
# ========================================

_KEYWORDS = (
    ("quarter", ("季度", "按季", "quarter", "quarterly")),
    ("year", ("年度", "按年", "每年", "yearly", "annual")),
    ("month", ("月份", "月度", "按月", "每月", "monthly")),
    ("week", ("每周", "按周", "周度", "weekly")),
    ("day", ("按日", "每日", "每天", "daily")),
)


# 粒度 -> pandas period 频率
PERIOD_FREQ = {
    "year": "Y",
    "quarter": "Q",
    "month": "M",
    "week": "W",
    "day": "D",
}


# 自动选粒度时，x 轴最多允许多少个点。
# 超过之后标签就挤成一团了（V3.6 那张图就是这个症状）。
MAX_BUCKETS = 20


def normalize(value) -> Optional[str]:
    """
    把任意写法归一化成标准粒度。

    无法识别时返回 None。
    """

    if value is None:
        return None

    key = str(value).strip().lower()

    if not key or key in ("none", "null", "nan"):
        return None

    return _ALIASES.get(key)


def detect_from_text(text) -> Optional[str]:
    """
    从标题 / 说明里识别「明确」的粒度关键词。

    识别不到时返回 None。
    """

    if not text:
        return None

    lowered = str(text).lower()

    for granularity, words in _KEYWORDS:

        for word in words:

            if word in lowered:
                return granularity

    return None


def select_by_span(dates) -> str:
    """
    根据日期跨度自动选择粒度。

    策略：

        在保证点的数量不超过 MAX_BUCKETS 的前提下，
        选择最细的粒度 —— 既能看清趋势，标签也不会挤成一团。

    参数：

        dates:
            已经解析好的 pandas datetime Series

    返回：

        year / quarter / month / week / day
    """

    valid = dates.dropna()

    if valid.empty:
        return "day"

    span_days = (
        valid.max() - valid.min()
    ).days

    # 跨度很短，逐日展示就够了
    if span_days <= 31:
        return "day"

    for granularity in (
        "day",
        "week",
        "month",
        "quarter",
        "year",
    ):

        freq = PERIOD_FREQ[granularity]

        buckets = (
            valid.dt.to_period(freq)
            .nunique()
        )

        if buckets <= MAX_BUCKETS:
            return granularity

    return "year"


def period_start(dates, granularity):
    """
    返回每个日期所属粒度区间的起始时间。

    折线图必须按真实时间排序，
    否则时间标签会被当成普通字符串排序：

        2026-Q1, 2026-Q2, ... 还好，
        但 2026-10 会排在 2026-2 前面。
    """

    freq = PERIOD_FREQ.get(granularity)

    if freq is None:
        return dates

    return dates.dt.to_period(freq).dt.start_time
