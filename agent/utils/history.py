"""
DataMind V4.2
多轮对话上下文

作用：

    把之前的问答压缩成一段简短上下文，
    塞进 Planner / Reporter 的 prompt。

为什么需要压缩：

    直接把历史原文全部塞进 prompt 会越来越长，
    几轮之后就会挤掉真正重要的数据集信息。
"""

from typing import Any, Dict, List


# 最多带几轮历史
MAX_TURNS = 3

# 每条回答最多保留多少字
MAX_ANSWER_CHARS = 400


def format_history(
    history: List[Dict[str, Any]],
    max_turns: int = MAX_TURNS,
    answer_chars: int = MAX_ANSWER_CHARS,
) -> str:
    """
    把历史问答整理成 prompt 里的一段文字。

    没有历史时返回一句说明，
    这样 prompt 里不会出现空段落。
    """

    if not history:

        return "（这是本次对话的第一轮）"

    lines = []

    for turn in history[-max_turns:]:

        question = str(
            turn.get("question", "")
        ).strip()

        answer = str(
            turn.get("answer", "")
        ).strip().replace("\n", " ")

        if len(answer) > answer_chars:

            answer = answer[:answer_chars] + "…"

        lines.append(f"用户：{question}")

        if answer:

            lines.append(f"助手：{answer}")

    return "\n".join(lines)
