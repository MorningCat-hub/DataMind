from agent.state import AgentState

from agent.utils.history import format_history

from services.llm_service import (
    call_llm
)


def build_charts_summary(
    chart_results
):
    """
    把全部图表结果整理成给模型看的文字摘要。

    V3.6.2：

    刻意不包含 chart_path 等本地磁盘路径。
    报告里写本地路径只会产生一张打不开的坏图片。

    V3.9：

    支持多张图表 —— 生成几张就展示几张。
    """

    chart_results = [
        item
        for item in (chart_results or [])
        if isinstance(item, dict)
    ]

    if not chart_results:

        return "本次分析没有生成图表。"

    blocks = []

    for index, chart_result in enumerate(
        chart_results, start=1
    ):

        if chart_result.get("success") is not True:

            error = chart_result.get("error") or {}

            blocks.append(
                f"第 {index} 张图表生成失败："
                f"{error.get('message', '未知原因')}"
            )

            continue

        lines = [
            f"第 {index} 张图表",
            f"图表类型：{chart_result.get('chart_type')}",
            f"图表标题：{chart_result.get('title')}",
            f"图表说明：{chart_result.get('description')}",
        ]

        data_summary = chart_result.get(
            "data_summary"
        ) or {}

        if data_summary.get("row_count") is not None:

            lines.append(
                f"数据点数量：{data_summary.get('row_count')}"
            )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_failure_notice(
    execution_results,
    review_result,
    chart_results,
    dataset_info,
):
    """
    汇总本次分析里的失败信息，生成一段固定的 Markdown 提示。

    V3.8：

    这段内容由程序直接生成并追加到报告末尾，
    不依赖模型「记得写」。

    旧行为：任务执行失败时，报告照样输出一份看起来很正常的
    结论，用户完全不知道有分析根本没跑成。
    """

    problems = []

    # ========================================
    # 数据集本身就没读进来
    # ========================================

    if not dataset_info:

        problems.append(
            "- 数据集读取失败，本次分析没有可用的数据基础。"
        )

    # ========================================
    # 执行失败的任务
    # ========================================

    for item in execution_results or []:

        if not isinstance(item, dict):

            continue

        if item.get("status") != "failed":

            continue

        error = item.get("error") or {}

        problems.append(
            "- 第 {step} 步「{task}」（工具 {tool}）执行失败："
            "{etype}: {emsg}".format(
                step=item.get("step"),
                task=item.get("task"),
                tool=item.get("tool"),
                etype=error.get(
                    "type", "UnknownError"
                ),
                emsg=error.get("message", ""),
            )
        )

    # ========================================
    # Reviewer 判定分析不完整
    # ========================================

    review_result = review_result or {}

    if review_result.get("passed") is False:

        problems.append(
            "- Reviewer 判定分析不完整：{reason}".format(
                reason=(
                    review_result.get("reason")
                    or "未给出原因"
                )
            )
        )

        for task in (
            review_result.get("missing_tasks") or []
        ):

            problems.append(
                f"  - 缺失分析：{task}"
            )

    # ========================================
    # 图表生成失败
    # ========================================

    for index, chart_result in enumerate(
        chart_results or [], start=1
    ):

        if not isinstance(chart_result, dict):

            continue

        if chart_result.get("success") is not False:

            continue

        error = chart_result.get("error") or {}

        problems.append(
            "- 第 {index} 张图表生成失败：{msg}".format(
                index=index,
                msg=(
                    error.get("message")
                    or "未知原因"
                )
            )
        )

    if not problems:

        return ""

    return (
        "\n\n---\n\n"
        "## ⚠️ 本次分析未能完全完成\n\n"
        "以下内容由系统自动记录，未经模型润色：\n\n"
        + "\n".join(problems)
        + "\n\n"
        "上面的结论只覆盖成功执行的部分。"
        "失败任务涉及的问题没有得到回答，请不要据此推断。\n"
    )


def build_health_disclosure(state):
    """
    把数据体检的限制写成报告里必须出现的一段。

    V4.11：这段由**程序**生成（不经过模型），
    所以它一定会出现在报告里，不会被模型漏掉。
    """

    findings = state.get("health_findings") or []

    blocking = [
        item for item in findings
        if item.get("severity") == "block"
    ]

    if not blocking:

        return ""

    lines = [
        "",
        "---",
        "",
        "## 数据限制说明",
        "",
        "本次分析的数据存在以下限制，结论已相应处理：",
        "",
    ]

    for item in blocking:

        lines.append("- **{}**：{}".format(
            item.get("title"), item.get("detail")
        ))

    lines.append("")

    return "\n".join(lines)


def generate_report(
    state: AgentState
):
    """
    Report Generator。

    汇总：

    用户问题
    +
    所有执行结果
    +
    Reviewer结果

    生成最终报告。
    """

    question = state[
        "question"
    ]

    execution_results = state[
        "execution_results"
    ]

    review_result = state[
        "review_result"
    ]

    chart_results = [
        item
        for item in (state.get("chart_results") or [])
        if isinstance(item, dict)
    ]

    chart_summary = build_charts_summary(
        chart_results
    )

    failure_notice = build_failure_notice(
        execution_results=execution_results,
        review_result=review_result,
        chart_results=chart_results,
        dataset_info=state.get("dataset_info"),
    )

    # ====================================================
    # V4.1
    #
    # 非表格输入（Word 正文、图片解读结果）没有数值可算，
    # 直接把内容交给模型来回答用户问题。
    # ====================================================

    input_context = state.get(
        "input_context"
    ) or ""

    # V4.2：多轮对话上下文
    history_text = format_history(
        state.get("history")
    )

    if input_context:

        text_context_block = f"""
====================
原始内容（来自上传的文档或图片，不是表格数据）
====================

{input_context}
"""

    else:

        text_context_block = ""

    prompt = f"""
你是专业的数据分析师。

请根据数据分析 Agent 的执行结果，
生成最终中文分析报告。

====================

用户问题：

{question}

====================

执行结果：

{execution_results}

====================

审查结果：

{review_result}

====================

====================
图表结果（仅供你了解，不要原样输出）
====================

{chart_summary}
{text_context_block}
====================
之前的对话
====================

{history_text}

要求：

1. 必须回答用户问题。
2. 不要编造数据。
3. 所有结论必须来自执行结果。
4. 使用专业中文。
5. 结构清晰。
6. 本次生成的全部图表都已经在页面下方的「可视化结果」区域
   自动展示。因此报告中不要输出图片链接、不要输出任何本地文件路径，
   也不要出现「图表链接」这样的字段。
   如果需要提到图表，只用文字说明。
7. 如果执行结果里有失败的任务：
   - 必须单独列出「未能完成的分析」，写清楚是哪一步、为什么失败；
   - 绝对不要对失败的任务给出结论，也不要用别的数据替代；
   - 不要假装本次分析是完整的。
8. 如果上面提供了「原始内容」：
   - 直接根据这段内容回答用户的问题；
   - 明确说明本次没有做数值计算，
     结论来自对文档或图片内容的理解；
   - 不要编造任何数值，也不要假装做过计算。
9. 如果「之前的对话」里已经有分析结果，
   而用户这次是在追问，
   只回答这次追问的问题，
   不要重复上一轮的完整报告。
10. 包含：

- 核心结论
- 数据分析
- 详细结果
- 建议（如果适合）

请直接输出最终报告。
"""


    messages = [
        {
            "role": "system",
            "content": (
                "你是专业的数据分析报告生成器。"
            )
        },
        {
            "role": "user",
            "content": prompt
        }
    ]


    report = call_llm(
        messages
    )

    # ====================================================
    # V3.8
    #
    # 失败信息由程序直接追加，不依赖模型「记得写」。
    #
    # 之前只要模型没提，用户看到的就会是一份看起来很正常的
    # 结论，完全不知道有分析根本没跑成。
    # ====================================================

    if failure_notice:

        report = f"{report}{failure_notice}"


    disclosure = build_health_disclosure(state)

    if disclosure and disclosure not in report:

        report = report + disclosure

    return {
        "final_report": report
    }