import os
from dotenv import load_dotenv
from openai import OpenAI


# 加载 .env
load_dotenv()


# =========================
# OpenAI Client
# =========================

# =========================
# V4.1
#
# 显式设置超时与重试次数。
#
# 之前用的是 SDK 默认值（600 秒超时 / 2 次重试）：
# 一次卡住要等十分钟才失败。
# 图片识别单次耗时更长，所以给到 120 秒。
# =========================

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    timeout=120.0,
    max_retries=1
)


# =========================
# Model Name
# =========================

MODEL_NAME = os.getenv(
    "MODEL_NAME",
    "deepseek-flash"
)


def call_llm(messages, temperature=0.2):
    """
    统一调用 LLM。

    所有 Agent Node 都通过这个函数调用模型。

    ⚠️ 如果你后面更换模型，
    只需要修改 .env。
    """

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=temperature
    )

    return response.choices[0].message.content


# =========================
# V4.5 原生 Tool Calling
# =========================

def call_llm_with_tools(
    messages,
    tools,
    temperature=0.2,
    tool_choice="auto"
):
    """
    带工具的模型调用。

    返回的是完整的 message 对象（不只是 content），
    因为调用方需要读取 tool_calls 和 reasoning_content。

    注意：

    当前模型跑在思考模式下，
    强制指定 tool_choice（指定某个函数）会返回 400：
    "Thinking mode does not support this tool_choice"。

    所以这里固定用 auto，让模型自己决定要不要调用工具。
    """

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=temperature,
        tools=tools,
        tool_choice=tool_choice
    )

    return response.choices[0].message


def assistant_turn(message):
    """
    把模型的一次回复转成可以回填进 messages 的 dict。

    注意（实测踩过）：

    1. 思考模式下必须把 reasoning_content 一起带回去，
       否则下一轮会返回 400；
    2. 模型可能一次返回多个 tool_calls，
       每一个 tool_call_id 都必须有对应的 tool 消息，
       少一个也会 400。
    """

    turn = {
        "role": "assistant",
        "content": message.content or ""
    }

    tool_calls = getattr(message, "tool_calls", None) or []

    if tool_calls:

        turn["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments
                }
            }
            for call in tool_calls
        ]

    reasoning = getattr(message, "reasoning_content", None)

    if reasoning is None:

        extra = getattr(message, "model_extra", None) or {}

        reasoning = extra.get("reasoning_content")

    if reasoning:

        turn["reasoning_content"] = reasoning

    return turn