from agent.state import AgentState
from agent.utils.trace import create_trace_event
from tools.dataset_tool import get_dataset_info
from tools.loader import ingest


SOURCE_LABELS = {
    "csv": "CSV 文件",
    "excel": "Excel 文件",
    "word_table": "Word 文档表格",
    "word_text": "Word 文档正文",
    "image_table": "图片（视觉识别）",
    "image_text": "图片内容",
}


def build_dataset_info(result):
    """
    把「数据来源」拼在数据集说明前面。

    下游 Planner / Reviewer / Reporter 看到的仍然是原来的
    数据集说明格式，只是多了一段来源信息。
    """

    normalized_path = result.get("normalized_path")

    description = get_dataset_info(normalized_path)

    label = SOURCE_LABELS.get(
        result.get("source_type"),
        "数据文件"
    )

    notes = result.get("notes") or []

    header = f"=== 数据来源 ===\n{label}"

    if notes:

        header += "\n提示：" + "；".join(notes)

    return header + "\n\n" + description


def analyze_dataset(
    state: AgentState
):
    """
    数据集读取节点（V4.1 起同时负责多格式识别）。

    职责：

    1. 按扩展名识别上传的文件
    2. 表格类（CSV / Excel / Word 表格 / 图片提取）
       → 统一归一化成标准 CSV，并把 file_path 指向它，
         这样 Planner / Executor / 绘图链路完全不用改
    3. 非表格内容（Word 正文、纯文字图片）
       → 记录内容，由报告节点直接回答用户问题
    4. 读取失败 → 记录原因，由报告节点如实说明

    V3.5.8：

    - 补全 Agent Trace，让前端「执行过程」能够展示数据读取阶段
    """

    agent_trace = list(
        state.get("agent_trace") or []
    )

    file_path = state["file_path"]

    # ==========================================
    # V4.2
    #
    # 追问时数据集沿用上一轮，
    # 不需要用户每次都重新上传文件。
    # ==========================================

    is_followup = bool(
        state.get("is_followup")
    )

    if is_followup and (
        state.get("dataset_info")
        or state.get("input_context")
    ):

        agent_trace.append(
            create_trace_event(
                event="dataset_reused",

                node="dataset",

                status="success",

                message="沿用上一轮已加载的数据集",

                metadata={
                    "source_type":
                        state.get("source_type")
                },

                display={
                    "title": "沿用上一轮数据集",
                    "icon": "info",
                    "level": "info",
                    "visible": True
                }
            )
        )

        return {
            "agent_trace": agent_trace
        }

    try:

        result = ingest(file_path)

    except Exception as exc:  # noqa: BLE001

        result = {
            "kind": "error",
            "source_type": None,
            "normalized_path": None,
            "shape": None,
            "context": None,
            "notes": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    kind = result.get("kind")
    notes = result.get("notes") or []
    source_type = result.get("source_type")

    # ==========================================
    # 读取失败
    # ==========================================

    if kind == "error":

        error_message = (
            result.get("error") or "文件读取失败"
        )

        agent_trace.append(
            create_trace_event(
                event="dataset_error",

                node="dataset",

                status="error",

                message=error_message,

                error={
                    "type": "FileReadError",
                    "message": error_message
                },

                display={
                    "title": "文件读取失败",
                    "icon": "alert",
                    "level": "error",
                    "visible": True
                }
            )
        )

        return {
            "dataset_info": None,
            "input_kind": "error",
            "input_context": None,
            "source_type": source_type,
            "dataset_notes": notes,
            "agent_trace": agent_trace,
            "error": error_message
        }

    # ==========================================
    # 非表格内容：转为内容解读
    # ==========================================

    if kind == "text":

        context = result.get("context") or ""

        agent_trace.append(
            create_trace_event(
                event="content_extracted",

                node="dataset",

                status="success",

                message=(
                    "未发现表格数据，已转为内容解读"
                ),

                metadata={
                    "source_type": source_type,
                    "chars": len(context)
                },

                display={
                    "title": "转为内容解读",
                    "icon": "info",
                    "level": "info",
                    "visible": True
                }
            )
        )

        return {
            "dataset_info": None,
            "input_kind": "text",
            "input_context": context,
            "source_type": source_type,
            "dataset_notes": notes,
            "agent_trace": agent_trace,
            "error": None
        }

    # ==========================================
    # 表格数据：归一化 + 描述
    # ==========================================

    dataset_info = build_dataset_info(result)

    shape = result.get("shape") or [0, 0]

    agent_trace.append(
        create_trace_event(
            event="dataset_analyzed",

            node="dataset",

            status="success",

            message=(
                f"已读取数据：{shape[0]} 行 × {shape[1]} 列"
            ),

            metadata={
                "source_type": source_type,
                "shape": shape
            },

            display={
                "title": (
                    "读取并理解数据集"
                    f"（{SOURCE_LABELS.get(source_type, '数据文件')}）"
                ),
                "icon": "info",
                "level": "info",
                "visible": True
            }
        )
    )

    return {
        "dataset_info": dataset_info,
        "file_path": (
            result.get("normalized_path") or file_path
        ),
        "input_kind": "table",
        "input_context": None,
        "source_type": source_type,
        "dataset_notes": notes,
        "agent_trace": agent_trace,
        "error": None
    }
