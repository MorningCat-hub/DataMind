"""
DataMind V4.6
业务口径检索

解决的问题：

    模型只能看到列名和类型。
    「净销售额」这类业务指标在数据里根本没有对应的列，
    模型要么报错，要么拿一列名字相近的字段凑一个数 ——
    结果看起来正常，但算的不是用户要的东西。

    这是数据分析里最隐蔽的一类错误：
    代码没报错，图也画出来了，数字也确实是算出来的，
    只是算的不是那个东西。

做法：

    从口径库里检索出和当前问题相关的条目，
    注入给 Planner（决定计划）和 Executor（决定工具参数）。

检索方式，以及为什么不用向量检索：

    1. 当前 API 供应商没有 embedding 接口（实测所有候选模型都是 404）
    2. 字典只有几十条，而且问题里通常直接出现指标名
    3. 中文没有空格，但**字符二元组重叠**不需要分词就能算
    4. 不需要新依赖，也没有每次查询的 embedding 费用
    5. 命中理由可解释（「命中别名 净销售」），适合放进 trace

    只有词法完全没命中时，才让模型从字典里挑一次，
    用来兜住「实际收入」→「净销售额」这类词汇鸿沟。

    向量检索要等到字典上百条、跨多个业务域、
    问法高度口语化的时候才有价值。
"""

import os
from typing import Any, Dict, List, Optional

import yaml

from services.llm_service import call_llm


# 默认口径库：<项目根>/knowledge/metrics.yaml
KNOWLEDGE_DIR = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    ),
    "knowledge",
)

DEFAULT_PATH = os.path.join(
    KNOWLEDGE_DIR,
    "metrics.yaml",
)


# 词法得分低于这个值就认为没命中
MIN_SCORE = 3.0

# 最多注入几条
MAX_HITS = 3

# 名称或别名整体命中的加分
NAME_HIT_SCORE = 10.0

# 问句二元组覆盖率换算成得分的系数
COVERAGE_WEIGHT = 6.0

# 覆盖率到这个比例才写进「命中理由」
COVERAGE_REASON = 0.3

# 口径涉及的字段在数据集里都存在时的加分
APPLICABLE_BONUS = 2.0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _bigrams(text: str) -> set:
    """
    中文没有空格，字符二元组是不用分词就能算重叠的办法。

    「净销售额」-> 净销 / 销售 / 售额
    """

    cleaned = "".join(
        char for char in _text(text).lower()
        if not char.isspace()
    )

    if not cleaned:

        return set()

    if len(cleaned) < 2:

        return {cleaned}

    return {
        cleaned[index:index + 2]
        for index in range(len(cleaned) - 1)
    }


def entry_search_text(entry: Dict[str, Any]) -> str:
    """
    把一条口径拼成用于检索的文本。
    """

    parts = [
        _text(entry.get("name")),
        _text(entry.get("definition")),
        _text(entry.get("formula")),
        " ".join(_text(item) for item in entry.get("aliases") or []),
        " ".join(_text(item) for item in entry.get("keywords") or []),
        " ".join(_text(item) for item in entry.get("requires") or []),
    ]

    return " ".join(part for part in parts if part)


def score_entry(
    question: str,
    entry: Dict[str, Any],
    columns: Optional[List[str]] = None,
):
    """
    给一条口径打分，并说明命中理由。

    返回：

        score    总分，用于排序
        reasons  命中理由
        lexical  纯词法得分，**用于判断算不算命中**

    为什么要把 lexical 单独拿出来：

        「所需字段齐全」是加分项，
        但它不应该自己把一条口径顶过命中阈值 ——
        否则一个词法上几乎不相干的口径
        （只是恰好字段都在数据集里）也会被当成命中，
        LLM 兜底就永远轮不到了。

        所以：阈值看 lexical，排序看 score。
    """

    score = 0.0

    lexical = 0.0

    reasons: List[str] = []

    lowered = _text(question).lower()

    names = [_text(entry.get("name"))] + [
        _text(item) for item in entry.get("aliases") or []
    ]

    for name in names:

        if name and name.lower() in lowered:

            score += NAME_HIT_SCORE

            lexical += NAME_HIT_SCORE

            reasons.append(f"命中名称「{name}」")

            break

    question_grams = _bigrams(question)

    entry_grams = _bigrams(entry_search_text(entry))

    if question_grams and entry_grams:

        overlap = question_grams & entry_grams

        coverage = len(overlap) / len(question_grams)

        if coverage:

            boost = coverage * COVERAGE_WEIGHT

            score += boost

            lexical += boost

            if coverage >= COVERAGE_REASON:

                reasons.append(f"关键词覆盖 {coverage:.0%}")

    requires = [
        _text(item) for item in entry.get("requires") or []
    ]

    if requires and columns and all(
        item in columns for item in requires
    ):

        # 只影响排序，不影响「算不算命中」
        score += APPLICABLE_BONUS

        reasons.append("所需字段齐全")

    return score, reasons, lexical


def _read_yaml(path: str) -> List[Dict[str, Any]]:
    """
    读一个口径文件。格式不对就返回空列表 ——
    口径库是可选的，没有它系统照常跑。
    """

    if not os.path.isfile(path):

        return []

    try:

        with open(path, "r", encoding="utf-8") as handle:

            data = yaml.safe_load(handle) or {}

    except Exception:  # noqa: BLE001

        return []

    entries = (
        data.get("entries")
        if isinstance(data, dict)
        else data
    )

    if not isinstance(entries, list):

        return []

    return [
        item
        for item in entries
        if isinstance(item, dict) and item.get("name")
    ]


def load_entries(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    读口径库。

    传 path 时只读那一个文件；
    不传时读知识库目录下的**所有** *.yaml ——
    这样不同领域可以各自一个包（metrics.yaml、ecommerce.yaml …）。

    同名条目**不去重**：
    同一个指标在不同领域里的口径本来就不同
    （「净销售额」在通用包要减退款，在电商包是 Quantity × UnitPrice），
    硬去重会随文件顺序随机丢一个。
    交给检索阶段按「所需字段在不在这个数据集里」来筛。
    """

    if path is not None:

        return _read_yaml(path)

    import glob

    targets = sorted(
        glob.glob(os.path.join(KNOWLEDGE_DIR, "*.yaml"))
        + glob.glob(os.path.join(KNOWLEDGE_DIR, "*.yml"))
    )

    if not targets:

        return _read_yaml(DEFAULT_PATH)

    entries: List[Dict[str, Any]] = []

    for target in targets:

        entries.extend(_read_yaml(target))

    return entries


def missing_requirements(
    entry: Dict[str, Any],
    columns: Optional[List[str]],
) -> List[str]:
    """
    这条口径需要的字段里，数据集缺了哪些。
    """

    requires = [
        _text(item)
        for item in entry.get("requires") or []
    ]

    if not requires or not columns:

        return []

    return [item for item in requires if item not in columns]


def is_applicable(
    entry: Dict[str, Any],
    columns: Optional[List[str]],
) -> bool:
    """
    这条口径在当前数据集上能不能执行。

    requires 可能是为另一个数据集写的。
    不筛的话，就会把「销售额 - 退款金额」这种
    在当前数据里凑不出来的公式注入给模型 ——
    那正是我们想避免的「拿相近字段凑」。
    """

    return not missing_requirements(entry, columns)


def _named_in_question(
    entry: Dict[str, Any],
    question: str,
) -> bool:
    """
    问题里有没有明确提到这条口径（名称或别名）。
    """

    lowered = _text(question).lower()

    for name in [_text(entry.get("name"))] + [
        _text(item) for item in entry.get("aliases") or []
    ]:

        if name and name.lower() in lowered:

            return True

    return False


def dataset_columns(file_path: Optional[str]) -> List[str]:
    """
    直接从 CSV 表头拿字段名。

    比从 dataset_info 那段文本里猜要可靠。
    """

    if not file_path or not os.path.isfile(file_path):

        return []

    try:

        import pandas as pd

        return [
            str(item)
            for item in pd.read_csv(file_path, nrows=0).columns
        ]

    except Exception:  # noqa: BLE001

        return []


def _llm_select(
    question: str,
    candidates: List[Dict[str, Any]],
) -> List[str]:
    """
    词法没命中时的兜底：让模型从字典里挑一次。

    只在词法完全没有结果时才调用，
    用来处理「实际收入」这种和「净销售额」用词不同的问法。
    """

    listing = "\n".join(
        "- " + entry["name"] + "：" + _text(entry.get("definition"))
        for entry in candidates
    )

    prompt = f"""
用户的问题：

{question}

====================
可选的业务口径
====================

{listing}

====================
要求
====================

判断上面哪些业务口径和这个问题有关。

只返回相关的口径名称，用逗号分隔。
如果一个都不相关，只返回「无」。
不要返回任何解释，不要返回其他文字。
"""

    try:

        answer = call_llm([
            {
                "role": "system",
                "content": (
                    "你只负责判断业务口径是否与问题相关，"
                    "严格按要求的格式返回。"
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ])

    except Exception:  # noqa: BLE001

        return []

    if not answer:

        return []

    cleaned = answer.strip().replace("，", ",")

    if "无" in cleaned and "," not in cleaned:

        return []

    known = {entry["name"] for entry in candidates}

    return [
        item.strip()
        for item in cleaned.split(",")
        if item.strip() in known
    ]


def retrieve(
    question: str,
    file_path: Optional[str] = None,
    entries: Optional[List[Dict[str, Any]]] = None,
    use_llm_fallback: bool = True,
    top_k: int = MAX_HITS,
) -> Dict[str, Any]:
    """
    检索和问题相关的业务口径。

    返回：

        hits    命中的条目 [{entry, score, reasons}]
        method  lexical / llm / none
    """

    pool = (
        entries
        if entries is not None
        else load_entries()
    )

    if not pool or not question:

        return {"hits": [], "method": "none"}

    columns = dataset_columns(file_path)

    # ====================================
    # 筛掉「为别的数据集写的」口径
    #
    # 但**问题里点名提到的**要留下，即使字段缺失 ——
    # 例如「毛利率」这条存在的意义，
    # 恰恰是告诉模型「你需要成本列，而这里没有，不要硬算」。
    # 把它筛掉，模型反而会去拿相近字段凑。
    # ====================================

    kept: List[Dict[str, Any]] = []

    skipped_inapplicable = 0

    for entry in pool:

        missing = missing_requirements(entry, columns)

        if not missing:

            kept.append(entry)

            continue

        if _named_in_question(entry, question):

            annotated = dict(entry)

            annotated["_missing_fields"] = missing

            kept.append(annotated)

            continue

        skipped_inapplicable += 1

    pool = kept

    if not pool:

        return {
            "hits": [],
            "method": "none",
            "skipped_inapplicable": skipped_inapplicable,
        }

    scored = []

    for entry in pool:

        score, reasons, lexical = score_entry(
            question,
            entry,
            columns,
        )

        # 注意：阈值看词法得分，不看总分
        if lexical >= MIN_SCORE:

            scored.append({
                "entry": entry,
                "score": round(score, 2),
                "lexical": round(lexical, 2),
                "reasons": reasons,
            })

    if scored:

        scored.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        return {
            "hits": scored[:top_k],
            "method": "lexical",
            "skipped_inapplicable": skipped_inapplicable,
        }

    if not use_llm_fallback:

        return {
            "hits": [],
            "method": "none",
            "skipped_inapplicable": skipped_inapplicable,
        }

    # ====================================
    # 词法没命中，交给模型判断一次
    # ====================================

    names = _llm_select(question, pool)

    hits = []

    for name in names[:top_k]:

        for entry in pool:

            if entry["name"] == name:

                hits.append({
                    "entry": entry,
                    "score": 0.0,
                    "reasons": ["模型判断相关"],
                })

                break

    return {
        "hits": hits,
        "method": "llm" if hits else "none",
        "skipped_inapplicable": skipped_inapplicable,
    }


def format_knowledge_block(hits) -> str:
    """
    把命中的口径渲染成注入提示词的文本。

    Planner 和 Executor 用同一份文本，
    这样计划和实际计算依据的口径是一致的。
    """

    if not hits:

        return ""

    lines = [
        "====================",
        "必须遵守的业务口径",
        "====================",
        "",
    ]

    for item in hits:

        entry = item["entry"]

        lines.append(
            "【" + entry["name"] + "】"
            + _text(entry.get("definition"))
        )

        if entry.get("formula"):

            lines.append(
                "  计算方式：" + _text(entry["formula"])
            )

        if entry.get("requires"):

            lines.append(
                "  涉及字段："
                + "、".join(
                    _text(field)
                    for field in entry["requires"]
                )
            )

        if entry.get("note"):

            lines.append("  说明：" + _text(entry["note"]))

        missing = entry.get("_missing_fields") or []

        if missing:

            lines.append(
                "  ⚠️ 本数据集缺少这些字段："
                + "、".join(str(item) for item in missing)
                + "。这条口径**无法计算**，"
                "必须明确告诉用户缺什么，不要用相近字段凑。"
            )

        lines.append("")

    lines.extend([
        "注意：",
        "1. 涉及以上指标时，必须严格按这里的口径计算和筛选，"
        "不要只看字段名想当然地算。",
        "2. 如果口径涉及的字段在数据集里不存在，"
        "要明确指出缺少哪个字段，不要用相近字段凑数。",
    ])

    return "\n".join(lines)


def summarize_hits(hits) -> List[Dict[str, Any]]:
    """
    给接口和前端用的精简信息。
    """

    return [
        {
            "name": item["entry"]["name"],
            "type": item["entry"].get("type", "metric"),
            "reasons": item.get("reasons") or [],
            "score": item.get("score", 0.0),
        }
        for item in hits
    ]
