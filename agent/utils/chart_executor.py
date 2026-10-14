
"""
DataMind V3.5.7
Chart Executor

职责：
1. 根据图表计划处理数据
2. 支持普通类别聚合和时间粒度聚合
3. 生成 matplotlib 图表
4. 保存 PNG 图片
5. 返回结构化执行结果

支持：
bar / horizontal_bar / line / pie / histogram / scatter

新增：
- 年、季度、月、周、日时间聚合
- 自动识别季度分析
- 时间顺序排序
- 返回聚合后的数据摘要
"""

import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from agent.utils.time_granularity import (
    detect_from_text as detect_granularity_from_text,
    normalize as normalize_time_granularity,
    period_start as time_period_start,
    select_by_span as select_granularity_by_span,
)


CHART_OUTPUT_DIR = "temp_charts"


# V3.7：
# 用来区分「调用方没有传粒度」和「调用方明确要求不做时间聚合」。
#   None              -> 明确不做时间聚合
#   _AUTO_GRANULARITY -> 没传，按老逻辑自己推断（兼容直接调用）
_AUTO_GRANULARITY = object()


plt.rcParams["font.sans-serif"] = [
    "SimHei",
    "Microsoft YaHei",
    "Arial Unicode MS",
    "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False


# =========================================
# 数据读取
# =========================================

def load_dataset(file_path: str) -> pd.DataFrame:
    if not file_path:
        raise ValueError("数据集路径为空")

    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"数据集文件不存在：{file_path}"
        )

    extension = os.path.splitext(file_path)[1].lower()

    if extension == ".csv":
        return pd.read_csv(file_path)

    if extension in [".xlsx", ".xls"]:
        return pd.read_excel(file_path)

    raise ValueError(
        f"暂不支持的数据格式：{extension}"
    )


# =========================================
# 统一返回结构
# =========================================

def build_success_result(
    chart_path: str,
    chart_type: str,
    title: str,
    description: str,
    data_summary: Dict[str, Any],
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "success": True,
        "chart_path": chart_path,
        "chart_type": chart_type,
        "title": title,
        "description": description,
        "data_summary": data_summary,
        "error": None,
        "warnings": warnings or [],
    }


def build_error_result(
    error_type: str,
    error_message: str,
    chart_type: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "chart_path": None,
        "chart_type": chart_type,
        "title": title,
        "description": None,
        "data_summary": None,
        "error": {
            "type": error_type,
            "message": error_message,
        },
        "warnings": [],
    }


# =========================================
# 字段检查
# =========================================

def validate_fields(
    df: pd.DataFrame,
    chart_plan: Dict[str, Any],
) -> None:
    chart_type = chart_plan.get("chart_type")
    x_field = chart_plan.get("x_field")
    y_field = chart_plan.get("y_field")

    if not x_field:
        raise ValueError("图表计划缺少 x_field")

    if x_field not in df.columns:
        raise KeyError(
            f"数据集中不存在字段：{x_field}"
        )

    if chart_type == "histogram":
        return

    if not y_field:
        raise ValueError("图表计划缺少 y_field")

    if y_field not in df.columns:
        raise KeyError(
            f"数据集中不存在字段：{y_field}"
        )


# =========================================
# 时间粒度识别
# =========================================

def detect_time_granularity(
    chart_plan: Dict[str, Any],
) -> Optional[str]:
    """
    推断图表计划里的时间粒度。

    V3.7 改动：

    1. 优先使用 Chart Planner 显式给出的 time_granularity；
    2. 否则从标题 / 说明里识别明确的时间关键词；
    3. 「周期」「趋势」这类模糊说法故意不在这里猜，
       交给 resolve_time_granularity() 结合真实数据跨度决定。
    """

    explicit = normalize_time_granularity(
        chart_plan.get("time_granularity")
    )

    if explicit:
        return explicit

    context = " ".join([
        str(chart_plan.get("title", "")),
        str(chart_plan.get("reason", "")),
    ])

    return detect_granularity_from_text(context)


# =========================================
# 日期转换
# =========================================

def parse_dates(values: pd.Series) -> pd.Series:
    """
    把一列解析成 datetime。

    V3.7：

    - 显式使用 format="mixed"，避免 pandas 逐元素推断格式时
      刷出 "Could not infer format" 警告；
    - 数值列一律不当作日期处理。
      pd.to_datetime 会把整数当成 1970 年的纳秒时间戳，
      把数量、单价这类字段错误地变成日期。
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

        # 老版本 pandas 不支持 format="mixed"
        return pd.to_datetime(
            values,
            errors="coerce",
        )


def looks_like_date_column(
    values: pd.Series,
    threshold: float = 0.8,
) -> bool:
    """
    判断这一列是否「确实就是日期」。

    只有在真的能解析成日期时才启用时间聚合。
    """

    if pd.api.types.is_datetime64_any_dtype(values):

        return True

    if pd.api.types.is_numeric_dtype(values):

        return False

    parsed = parse_dates(values)

    if parsed.isna().all():

        return False

    return parsed.notna().mean() >= threshold


def resolve_time_granularity(
    df: pd.DataFrame,
    chart_plan: Dict[str, Any],
) -> Optional[str]:
    """
    结合真实数据决定这张图到底要不要按时间聚合、按什么粒度聚合。

    返回 None 表示不做时间聚合。

    规则（V3.7）：

    1. x_field 必须真实存在，并且确实能解析成日期；
       否则一律不做时间聚合；
    2. 优先使用 Chart Planner 显式给出的粒度，
       其次使用标题 / 说明里明确的时间关键词；
    3. 都没有时（例如标题只写了「不同周期」），
       按日期跨度自动选一个 x 轴标签还读得清的粒度。
    """

    x_field = chart_plan.get("x_field")

    if not x_field or x_field not in df.columns:

        return None

    if not looks_like_date_column(df[x_field]):

        return None

    explicit = detect_time_granularity(chart_plan)

    if explicit:

        return explicit

    dates = parse_dates(df[x_field])

    if dates.dropna().empty:

        return None

    return select_granularity_by_span(dates)


def build_time_labels(
    values: pd.Series,
    granularity: str,
) -> pd.Series:
    """
    将日期字段转换为指定粒度的分组标签。
    """

    dates = parse_dates(values)

    if dates.isna().all():

        raise ValueError(
            "无法将 x_field 解析为有效日期，"
            "无法进行时间粒度聚合"
        )

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
        raise ValueError(
            f"不支持的时间粒度：{granularity}"
        )

    # 无法解析的日期保留为空值，后续聚合时排除
    return labels.where(dates.notna(), None)


# =========================================
# 数据聚合
# =========================================

def aggregate_data(
    df: pd.DataFrame,
    chart_plan: Dict[str, Any],
    granularity: Any = _AUTO_GRANULARITY,
) -> Tuple[pd.DataFrame, List[str]]:
    """
    聚合成可以直接绘图的数据。

    V3.7 改动：

    - 时间粒度由外部解析好后传进来。
      图表说明和数据摘要用的是同一个值，
      不会再出现「说明写着按季度聚合、实际上根本没聚合」；
    - 时间标签转换失败时降级为不聚合，并记录一条 warning，
      不再让整张图失败；
    - 一旦做了时间聚合，就必须真的分组：
      否则同一个时间标签会出现多个点，折线图会来回跳。

    返回：

        (聚合后的数据, warnings)
    """

    warnings: List[str] = []

    chart_type = chart_plan.get("chart_type")
    x_field = chart_plan.get("x_field")
    y_field = chart_plan.get("y_field")
    aggregation = chart_plan.get("aggregation", "none")

    # 兼容直接调用本函数的旧用法
    if granularity is _AUTO_GRANULARITY:

        granularity = detect_time_granularity(chart_plan)

    # 分布图和散点图保留原始观测
    if chart_type in ["histogram", "scatter"]:

        return df.copy(), warnings

    working = df.copy()

    applied_granularity = None

    if granularity:

        try:

            original_dates = parse_dates(
                working[x_field]
            )

            working[x_field] = build_time_labels(
                working[x_field],
                granularity,
            )

            working = working.dropna(
                subset=[x_field]
            )

            # 为折线图按真实时间顺序排列提供排序键
            working["_time_sort"] = time_period_start(
                original_dates,
                granularity,
            )

            applied_granularity = granularity

        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:

            # 降级：不做时间聚合，但图表照样画出来
            warnings.append(
                "时间聚合失败，已降级为不聚合："
                f"{type(exc).__name__}: {exc}"
            )

            working = df.copy()

            applied_granularity = None

    # 做了时间聚合却没有聚合方式时，默认按 sum 汇总。
    # 否则同一个时间标签会留下多行，折线图会来回跳。
    if applied_granularity and aggregation == "none":

        aggregation = "sum"

        warnings.append(
            "图表计划没有给出聚合方式，"
            "但使用了时间粒度，已自动按 sum 聚合。"
        )

    # 统计数量
    if aggregation == "count":

        result = (
            working.groupby(
                x_field,
                as_index=False,
                sort=False,
            )
            .size()
            .rename(columns={"size": "count"})
        )

        # 保持原有绘图接口使用的 y_field 名称
        if y_field != "count":

            result = result.rename(
                columns={"count": y_field}
            )

    elif aggregation in ["sum", "mean"]:

        # 避免非数值内容导致统计结果异常
        working[y_field] = pd.to_numeric(
            working[y_field],
            errors="coerce",
        )

        working = working.dropna(
            subset=[y_field]
        )

        grouped = working.groupby(
            x_field,
            as_index=False,
            sort=False,
        )[y_field]

        if aggregation == "sum":
            result = grouped.sum()
        else:
            result = grouped.mean()

    else:

        result = working[[x_field, y_field]].copy()

    # 时间维度排序，保证折线图不会按字符串乱序
    if (
        applied_granularity
        and "_time_sort" in working.columns
    ):

        sort_map = (
            working[[x_field, "_time_sort"]]
            .drop_duplicates(subset=[x_field])
        )

        result = result.merge(
            sort_map,
            on=x_field,
            how="left",
        )

        result = result.sort_values(
            "_time_sort"
        ).drop(
            columns=["_time_sort"]
        )

    return result.reset_index(drop=True), warnings


# =========================================
# 图表描述与数据摘要
# =========================================

def build_chart_description(
    chart_type: str,
    chart_plan: Dict[str, Any],
    granularity: Any = _AUTO_GRANULARITY,
) -> str:
    """
    生成图表说明。

    V3.7：

    粒度由外部传入，保证说明文字和真实聚合方式一致。
    """

    x_field = chart_plan.get("x_field")
    y_field = chart_plan.get("y_field")
    aggregation = chart_plan.get("aggregation", "none")

    if granularity is _AUTO_GRANULARITY:

        granularity = detect_time_granularity(chart_plan)

    if granularity:

        granularity_names = {
            "year": "年度",
            "quarter": "季度",
            "month": "月份",
            "week": "周",
            "day": "日期",
        }

        dimension = granularity_names.get(
            granularity,
            granularity,
        )

        return (
            f"按{dimension}对“{x_field}”进行时间聚合，"
            f"并以{aggregation}方式汇总“{y_field}”，"
            f"用于比较不同时间段的数值变化。"
        )

    descriptions = {
        "bar": (
            f"展示不同 {x_field} 之间的 "
            f"{y_field} 对比，聚合方式为 {aggregation}。"
        ),
        "horizontal_bar": (
            f"以水平柱状图展示不同 {x_field} 之间的 "
            f"{y_field} 对比，聚合方式为 {aggregation}。"
        ),
        "line": (
            f"展示 {x_field} 与 {y_field} 之间的变化关系。"
        ),
        "pie": (
            f"展示不同 {x_field} 在 {y_field} 中的占比情况。"
        ),
        "histogram": (
            f"展示 {x_field} 的数据分布情况。"
        ),
        "scatter": (
            f"展示 {x_field} 与 {y_field} 之间的关系分布。"
        ),
    }

    return descriptions.get(
        chart_type,
        "展示数据分析结果。",
    )


def build_data_summary(
    data: pd.DataFrame,
    chart_plan: Dict[str, Any],
    granularity: Any = _AUTO_GRANULARITY,
) -> Dict[str, Any]:
    """
    生成数据摘要。

    V3.7：

    time_granularity 记录的是「实际生效」的粒度，
    而不是 Chart Planner 原本想要的那个。
    """

    if granularity is _AUTO_GRANULARITY:

        granularity = detect_time_granularity(chart_plan)

    return {
        "row_count": int(len(data)),
        "x_field": chart_plan.get("x_field"),
        "y_field": chart_plan.get("y_field"),
        "aggregation": chart_plan.get(
            "aggregation", "none"
        ),
        "time_granularity": granularity,
        "categories": (
            data[chart_plan["x_field"]]
            .astype(str)
            .tolist()
            if chart_plan.get("x_field") in data.columns
            else []
        ),
    }


# =========================================
# 图表绘制
# =========================================

def draw_bar_chart(data, chart_plan):
    x_field = chart_plan["x_field"]
    y_field = chart_plan["y_field"]

    plt.figure(figsize=(10, 6))
    plt.bar(
        data[x_field].astype(str),
        data[y_field],
    )
    plt.title(chart_plan.get("title", "柱状图"))
    plt.xlabel(x_field)
    plt.ylabel(y_field)
    plt.xticks(rotation=30)
    plt.tight_layout()


def draw_horizontal_bar_chart(data, chart_plan):
    x_field = chart_plan["x_field"]
    y_field = chart_plan["y_field"]

    plt.figure(figsize=(10, 6))
    plt.barh(
        data[x_field].astype(str),
        data[y_field],
    )
    plt.title(chart_plan.get("title", "水平柱状图"))
    plt.xlabel(y_field)
    plt.ylabel(x_field)
    plt.tight_layout()


def draw_line_chart(data, chart_plan):
    x_field = chart_plan["x_field"]
    y_field = chart_plan["y_field"]

    plt.figure(figsize=(10, 6))
    plt.plot(
        data[x_field].astype(str),
        data[y_field],
        marker="o",
    )
    plt.title(chart_plan.get("title", "折线图"))
    plt.xlabel(x_field)
    plt.ylabel(y_field)
    plt.xticks(rotation=30)
    plt.tight_layout()


def draw_pie_chart(data, chart_plan):
    x_field = chart_plan["x_field"]
    y_field = chart_plan["y_field"]

    plt.figure(figsize=(8, 8))
    plt.pie(
        data[y_field],
        labels=data[x_field].astype(str),
        autopct="%1.1f%%",
        startangle=90,
    )
    plt.title(chart_plan.get("title", "饼图"))
    plt.tight_layout()


def draw_histogram(data, chart_plan):
    x_field = chart_plan["x_field"]

    plt.figure(figsize=(10, 6))
    plt.hist(
        pd.to_numeric(
            data[x_field],
            errors="coerce",
        ).dropna(),
        bins=10,
    )
    plt.title(chart_plan.get("title", "直方图"))
    plt.xlabel(x_field)
    plt.ylabel("频数")
    plt.tight_layout()


def draw_scatter_chart(data, chart_plan):
    x_field = chart_plan["x_field"]
    y_field = chart_plan["y_field"]

    plt.figure(figsize=(10, 6))
    plt.scatter(
        data[x_field],
        data[y_field],
    )
    plt.title(chart_plan.get("title", "散点图"))
    plt.xlabel(x_field)
    plt.ylabel(y_field)
    plt.tight_layout()


# =========================================
# 主执行函数
# =========================================

def execute_chart(
    file_path: str,
    chart_plan: Dict[str, Any],
) -> Dict[str, Any]:
    chart_type = None
    title = None
    granularity = None
    warnings: List[str] = []

    try:
        if not chart_plan:
            return build_error_result(
                "ValueError",
                "图表计划为空",
            )

        chart_type = chart_plan.get("chart_type")
        title = chart_plan.get("title", "数据分析图表")

        supported_types = {
            "bar",
            "horizontal_bar",
            "line",
            "pie",
            "histogram",
            "scatter",
        }

        if chart_type not in supported_types:
            return build_error_result(
                "ValueError",
                f"不支持的图表类型：{chart_type}",
                chart_type,
                title,
            )

        df = load_dataset(file_path)

        validate_fields(
            df,
            chart_plan,
        )

        # V3.7：
        # 只有 x_field 真的能解析成日期时才做时间聚合，
        # 并且优先采用 Chart Planner 显式给出的粒度。
        # 这样标题里写了「季度」但 x 轴是文本字段时，
        # 图表会照常画出来，而不是整张图失败。
        granularity = resolve_time_granularity(
            df,
            chart_plan,
        )

        data, warnings = aggregate_data(
            df,
            chart_plan,
            granularity,
        )

        if data.empty:
            return build_error_result(
                "ValueError",
                "处理后的数据为空，无法绘图",
                chart_type,
                title,
            )

        if chart_type == "bar":
            draw_bar_chart(data, chart_plan)
        elif chart_type == "horizontal_bar":
            draw_horizontal_bar_chart(data, chart_plan)
        elif chart_type == "line":
            draw_line_chart(data, chart_plan)
        elif chart_type == "pie":
            draw_pie_chart(data, chart_plan)
        elif chart_type == "histogram":
            draw_histogram(data, chart_plan)
        elif chart_type == "scatter":
            draw_scatter_chart(data, chart_plan)

        os.makedirs(
            CHART_OUTPUT_DIR,
            exist_ok=True,
        )

        filename = f"chart_{uuid.uuid4().hex}.png"
        chart_path = os.path.join(
            CHART_OUTPUT_DIR,
            filename,
        )

        plt.savefig(
            chart_path,
            dpi=150,
            bbox_inches="tight",
        )

        description = build_chart_description(
            chart_type,
            chart_plan,
            granularity,
        )

        data_summary = build_data_summary(
            data,
            chart_plan,
            granularity,
        )

        return build_success_result(
            chart_path=chart_path,
            chart_type=chart_type,
            title=title,
            description=description,
            data_summary=data_summary,
            warnings=warnings,
        )

    except Exception as e:
        return build_error_result(
            error_type=type(e).__name__,
            error_message=str(e),
            chart_type=chart_type,
            title=title,
        )

    finally:
        plt.close("all")

