import json
import os

from contextlib import asynccontextmanager

from datetime import datetime

from dotenv import load_dotenv

from typing import Optional

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    HTTPException
)

from fastapi.staticfiles import (
    StaticFiles
)

from fastapi.responses import (
    FileResponse,
    StreamingResponse,
    Response
)

from agent.graph import (
    build_graph
)

from agent.utils.trace_formatter import (
    format_agent_trace
)

from agent.utils.trace_formatter import (
    format_trace_event
)

from services.session_store import (
    has_dataset,
    session_store
)

from tools.loader import (
    SUPPORTED_EXTENSIONS,
    accepted_extensions,
    supported_hint
)


# ============================================================
# 加载环境变量
# ============================================================

load_dotenv()


# ============================================================
# 临时目录
# ============================================================

TEMP_FOLDER = "temp"

os.makedirs(
    TEMP_FOLDER,
    exist_ok=True
)


# ============================================================
# V3.5 Chart
#
# Chart Executor 生成的图片保存目录：
#
# temp_charts/
#
# 例如：
# temp_charts/chart_xxx.png
# ============================================================

CHART_FOLDER = "temp_charts"

os.makedirs(
    CHART_FOLDER,
    exist_ok=True
)


# ============================================================
# V4.8 临时文件清理
#
# 之前没有任何清理逻辑，
# temp/ 和 temp_charts/ 会一直堆积（实测已堆到 160 个文件 / 3.2 MB）。
# ============================================================

# 临时文件保留多久（小时）
TEMP_TTL_HOURS = 24

# 只清理自己生成的文件，避免误删用户放进来的东西
TEMP_FILE_PATTERNS = (
    "data_*.csv",
    "chart_*.png"
)


def cleanup_temp_files():
    """
    清理过期的临时文件。

    只删自己生成的文件（data_*.csv / chart_*.png），
    只删超过 TEMP_TTL_HOURS 的，不做递归删除。
    """

    import glob
    import time

    cutoff = time.time() - TEMP_TTL_HOURS * 3600

    removed = 0

    for folder in (TEMP_FOLDER, CHART_FOLDER):

        for pattern in TEMP_FILE_PATTERNS:

            for path in glob.glob(
                os.path.join(folder, pattern)
            ):

                try:

                    if (
                        os.path.isfile(path)
                        and os.path.getmtime(path) < cutoff
                    ):

                        os.remove(path)

                        removed += 1

                except OSError:

                    continue

    if removed:

        print(
            "[temp] 已清理 %d 个超过 %d 小时的临时文件"
            % (removed, TEMP_TTL_HOURS)
        )

    return removed


@asynccontextmanager
async def lifespan(app):
    """
    应用启动时清理过期临时文件。

    注意：这里**不能**在模块导入时直接调用 ——
    否则测试里 import main 就会去删项目目录里的文件，
    测试会因此产生副作用。
    放进 lifespan 之后，只有真正启动服务才会清理。
    """

    cleanup_temp_files()

    yield


# ============================================================
# FastAPI
# ============================================================

# ============================================================
# V3.6.2
# 前端静态文件禁用浏览器缓存
#
# 浏览器对没有 Cache-Control 的响应会按 Last-Modified 做
# 启发式缓存。旧版 app.js 的 Last-Modified 是 2026-09-18，
# 浏览器会认为它还能新鲜缓存两天以上，于是连服务器都不问，
# 直接用旧副本 —— 表现就是「前端代码改了，刷新也没生效」。
#
# 这里显式声明 no-store，彻底避免这个问题。
# ============================================================


class NoCacheStaticFiles(StaticFiles):
    """
    前端静态文件禁用浏览器缓存。
    """

    async def get_response(self, *args, **kwargs):

        response = await super().get_response(
            *args,
            **kwargs
        )

        response.headers["Cache-Control"] = (
            "no-store, must-revalidate"
        )

        return response


app = FastAPI(
    lifespan=lifespan,
    title="DataMind V3.5 | Self-Healing Data Analysis Agent"
)


# ============================================================
# 首页
# ============================================================

# ============================================================
# V4.14 对话历史
#
# 之前会话只在内存里，重启即丢。现在有了 SQLite：
# 列表、读取、改名、删除、导出。
# ============================================================


@app.get("/conversations")
def list_conversations_api(
    limit: int = 50
):
    """
    历史对话列表（按最近更新排序）。
    """

    items = store.list_conversations(limit=limit)

    return {
        "conversations": items,
        "total": len(items)
    }


@app.get("/conversations/{session_id}")
def get_conversation_api(
    session_id: str
):
    """
    取一条对话的完整内容，供界面渲染历史。
    """

    session = store.load_conversation(session_id)

    if session is None:

        raise HTTPException(
            status_code=404,
            detail="没有这条对话"
        )

    return {
        "session_id": session_id,
        "turn": session.get("turn") or 0,
        "source_name": session.get("source_name"),
        "source_type": session.get("source_type"),
        "input_kind": session.get("input_kind"),
        "file_path": session.get("file_path"),
        "history": session.get("history") or []
    }


@app.delete("/conversations/{session_id}")
def delete_conversation_api(
    session_id: str
):
    """删除一条对话（内存缓存与数据库一起清）。"""

    session_store.drop(session_id)

    removed = store.delete_conversation(session_id)

    return {
        "deleted": bool(removed) or True,
        "session_id": session_id
    }


@app.post("/conversations/{session_id}/rename")
def rename_conversation_api(
    session_id: str,
    title: str = Form("")
):
    """给对话改个名字。"""

    if not title.strip():

        raise HTTPException(
            status_code=400,
            detail="标题不能为空"
        )

    renamed = store.rename_conversation(session_id, title)

    if not renamed:

        raise HTTPException(
            status_code=404,
            detail="没有这条对话"
        )

    return {
        "session_id": session_id,
        "title": title.strip()[:60]
    }


@app.get("/conversations/{session_id}/export")
def export_conversation_api(
    session_id: str,
    format: str = "md"
):
    """
    导出对话。

    目前支持 markdown；返回的是**文件下载**，
    这样浏览器会直接存成 .md。
    """

    text = store.export_markdown(session_id)

    if text is None:

        raise HTTPException(
            status_code=404,
            detail="没有这条对话"
        )

    name = "datamind_{}.md".format(session_id[:8])

    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="{}"'.format(name)
        }
    )


from services import store

@app.get("/")
async def root():

    # V3.6.2：
    # 首页也禁用缓存，保证 index.html 里的
    # app.js / style.css 版本参数能及时生效。

    return FileResponse(
        "frontend/index.html",
        headers={
            "Cache-Control": "no-store, must-revalidate"
        }
    )


# ============================================================
# 挂载数据临时目录
#
# 浏览器访问：
#
# /temp/xxx.csv
#
# ============================================================

app.mount(
    "/temp",
    StaticFiles(
        directory=TEMP_FOLDER
    ),
    name="temp"
)


# ============================================================
# V3.4.3
# 前端静态文件
# ============================================================

FRONTEND_FOLDER = "frontend"

os.makedirs(
    FRONTEND_FOLDER,
    exist_ok=True
)


app.mount(
    "/frontend",
    NoCacheStaticFiles(
        directory=FRONTEND_FOLDER
    ),
    name="frontend"
)


# ============================================================
# V3.5
# Chart 静态图片目录
#
# Chart Executor：
#
# temp_charts/chart_xxx.png
#
# 浏览器：
#
# /charts/chart_xxx.png
# ============================================================

app.mount(
    "/charts",
    StaticFiles(
        directory=CHART_FOLDER
    ),
    name="charts"
)


# ============================================================
# 创建 Agent Graph
# ============================================================

graph = build_graph()


# ============================================================
# API
# ============================================================

# ============================================================
# V4.1
# 保存上传文件
# ============================================================

async def save_upload(file):

    """
    保存上传文件，返回 (路径, 错误信息)。

    V4.1：

    保留原始扩展名 —— 之前不管上传什么文件，
    都会被强行存成 .csv，导致 Excel / Word / 图片
    根本无法被识别。
    """

    original_name = file.filename or ""

    extension = os.path.splitext(
        original_name
    )[1].lower()

    if extension not in SUPPORTED_EXTENSIONS:

        return None, None, (
            "暂不支持的文件格式："
            f"{extension or '（无扩展名）'}。"
            f"支持的格式：{supported_hint()}"
        )

    uid = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )

    upload_path = os.path.join(
        TEMP_FOLDER,
        f"data_{uid}{extension}"
    )

    content = await file.read()

    with open(upload_path, "wb") as f:

        f.write(content)

    # V4.3：把原始文件名一起返回，
    # 前端要在输入框的附件位置显示它
    return upload_path, original_name, None


# ============================================================
# V4.2
# 流式输出
#
# 服务端每产生一个 Agent 事件就立刻推一帧 SSE，
# 前端「做到哪里显示哪里」，
# 而不是等全部跑完再一次性回放。
# ============================================================

def sse_frame(payload) -> str:
    """
    打包一帧 Server-Sent Events。
    """

    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n"
    )


def error_stream(message: str):
    """
    用一个只包含错误的流回应请求，
    这样前端只需要处理一种响应格式。
    """

    return StreamingResponse(
        iter([sse_frame({"type": "error", "message": message})]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no"
        }
    )


def build_initial_state(
    question,
    session,
    upload_path,
    upload_name=None
):
    """
    组装这一轮的初始 State。

    追问时沿用会话里已经加载好的数据集。
    """

    followup = (
        upload_path is None
        and has_dataset(session)
    )

    state = {
        "question": question,

        "file_path":
            upload_path
            or session.get("file_path")
            or "",

        "dataset_info": None,
        "plan": [],
        "current_step": 0,
        "execution_results": [],
        "execution_status": "idle",
        "pending_code": None,
        "last_execution_error": None,
        "current_task_retry_count": 0,
        "max_task_retries": 2,
        "code_history": [],
        "agent_trace": [],
        "error": None,
        "review_result": None,
        "retry_count": 0,
        "chart_plan": None,
        "chart_results": [],
        "final_report": None,

        "input_kind": "table",
        "input_context": None,
        "source_type": None,
        "dataset_notes": [],

        "history": session.get("history") or [],
        "is_followup": followup,
        "session_id": session.get("session_id"),

        # V4.3：原始文件名
        "source_name":
            upload_name
            or session.get("source_name"),

        # V4.4：数值核对结果
        "verification": None,

        # V4.6：业务口径
        "knowledge_context": None,
        "knowledge_hits": [],

        # V4.11：数据体检
        "health_findings": [],
        "health_context": None,
        "health_notice": None,

        # V4.12：结论核对
        "conclusion_check": None
    }

    if followup:

        state.update({
            "dataset_info":
                session.get("dataset_info"),

            "input_kind":
                session.get("input_kind") or "table",

            "input_context":
                session.get("input_context"),

            "source_type":
                session.get("source_type"),

            "dataset_notes":
                session.get("dataset_notes") or []
        })

    return state


def run_agent_stream(
    question,
    session,
    upload_path,
    upload_name=None
):
    """
    真正跑图并逐帧推送。

    这是同步生成器：Starlette 会把它放进线程池执行，
    因此不会阻塞事件循环
    （旧版 async def + 同步 graph.invoke 会把整个服务卡住）。
    """

    session_id = session["session_id"]

    try:

        if upload_path is not None:

            # 上传了新文件 -> 全新的分析上下文
            session_store.reset_dataset(session_id)

        initial_state = build_initial_state(
            question,
            session,
            upload_path,
            upload_name
        )

        yield sse_frame({
            "type": "start",
            "session_id": session_id,
            "turn": session["turn"] + 1,
            "reset": upload_path is not None
        })

        # ====================================================
        # 从初始 State 开始累积。
        #
        # 注意：
        # stream_mode="updates" 只会给出「节点返回过的字段」。
        # 初始 State 里那些没有任何节点改动过的字段
        # （例如 source_name）不会出现在 updates 里，
        # 所以必须先把它们放进来，
        # 否则 meta 帧里的文件名会永远是空的。
        # ====================================================

        final_state = dict(initial_state)

        seen = 0

        # ====================================================
        # 逐节点流式执行
        # ====================================================

        for chunk in graph.stream(
            initial_state,
            stream_mode="updates"
        ):

            if not isinstance(chunk, dict):

                continue

            for _node_name, update in chunk.items():

                if not isinstance(update, dict):

                    continue

                final_state.update(update)

                trace = update.get("agent_trace")

                if not isinstance(trace, list):

                    continue

                # 每个节点返回的是累积后的完整 trace，
                # 用下标把新增的事件挑出来推给前端。
                for event in trace[seen:]:

                    yield sse_frame({
                        "type": "trace",
                        "event": format_trace_event(event)
                    })

                seen = len(trace)

        # ====================================================
        # 收尾
        # ====================================================

        chart_results = [
            bind_chart_url(item)
            for item in (
                final_state.get("chart_results") or []
            )
            if isinstance(item, dict)
        ]

        report = final_state.get("final_report") or ""

        yield sse_frame({
            "type": "report",
            "content": report
        })

        yield sse_frame({
            "type": "charts",
            "charts": chart_results
        })

        yield sse_frame({
            "type": "meta",
            "input_kind":
                final_state.get("input_kind"),
            "source_type":
                final_state.get("source_type"),
            "source_name":
                final_state.get("source_name"),
            "dataset_notes":
                final_state.get("dataset_notes") or [],
            "verification":
                final_state.get("verification"),
            "knowledge_hits":
                final_state.get("knowledge_hits") or [],
            "health_findings":
                final_state.get("health_findings") or [],
            "conclusion_check":
                final_state.get("conclusion_check")
        })

        # 把这一轮写回会话
        session_store.remember(
            session_id,
            final_state,
            question,
            report
        )

        updated = session_store.get(session_id) or session

        yield sse_frame({
            "type": "done",
            "session_id": session_id,
            "turn": updated.get("turn", 0)
        })

    except Exception as exc:  # noqa: BLE001

        import traceback

        yield sse_frame({
            "type": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()
        })


@app.post("/chat/stream")
async def chat_stream(
    question: str = Form(""),
    session_id: str = Form(""),
    file: Optional[UploadFile] = File(None)
):

    """
    多轮对话 + 流式输出。

    第一轮必须带上文件；
    之后可以不传文件，直接追问，
    数据集会沿用同一个会话。
    """

    question = (question or "").strip()

    if not question:

        return error_stream("请输入分析问题")

    upload_path = None
    upload_name = None

    if file is not None and (file.filename or ""):

        upload_path, upload_name, error = await save_upload(file)

        if error:

            return error_stream(error)

    session = session_store.get(session_id)

    if session is None:

        if upload_path is None:

            return error_stream(
                "请先上传数据文件（CSV / Excel / Word / 图片）"
            )

        session = session_store.create(session_id or None)

    elif upload_path is None and not has_dataset(session):

        return error_stream(
            "上一轮没有成功加载数据，请重新上传文件"
        )

    return StreamingResponse(
        run_agent_stream(
            question,
            session,
            upload_path,
            upload_name
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no"
        }
    )


# ============================================================
# V3.9
# 把图表结果里的本地路径转换成浏览器可访问的 URL
#
# Chart Executor 原始结果：
#
#     "chart_path": "temp_charts/chart_xxx.png"
#
# 前端需要：
#
#     "chart_url": "/charts/chart_xxx.png"
# ============================================================

def bind_chart_url(
    chart_result
):
    """
    给单张图表结果补上 chart_url。

    失败或没有生成图片的结果原样返回，
    交给前端显示失败原因。
    """

    if (
        not isinstance(chart_result, dict)
        or chart_result.get("success") is not True
    ):

        return chart_result

    chart_path = chart_result.get(
        "chart_path"
    )

    if not chart_path:

        return chart_result

    chart_filename = os.path.basename(
        chart_path
    )

    return {
        **chart_result,
        "chart_url": f"/charts/{chart_filename}"
    }


@app.post(
    "/analyze"
)
async def analyze(
    file: UploadFile = File(...),
    question: str = Form(...)
):

    try:

        # ====================================================
        # 保存 CSV
        # ====================================================

        # ====================================================
        # V4.1 / V4.2
        #
        # 保存上传文件（保留原始扩展名），
        # 与流式对话接口共用同一段逻辑。
        # ====================================================

        upload_path, upload_name, upload_error = await save_upload(file)

        if upload_error:

            return {"error": upload_error}


        # ====================================================
        # 初始化 State
        # ====================================================

        initial_state = {

            # ------------------------------------------------
            # 用户输入
            # ------------------------------------------------

            "question":
                question,

            "file_path":
                upload_path,


            # ------------------------------------------------
            # Dataset
            # ------------------------------------------------

            "dataset_info":
                None,


            # ------------------------------------------------
            # Planner
            # ------------------------------------------------

            "plan":
                [],

            "current_step":
                0,


            # ------------------------------------------------
            # Executor Result
            # ------------------------------------------------

            "execution_results":
                [],

            "execution_status":
                "idle",

            "pending_code":
                None,

            "last_execution_error":
                None,

            "current_task_retry_count":
                0,

            "max_task_retries":
                2,

            "code_history":
                [],


            # ------------------------------------------------
            # Agent Trace
            # ------------------------------------------------

            "agent_trace":
                [],


            # ------------------------------------------------
            # Error
            # ------------------------------------------------

            "error":
                None,


            # ------------------------------------------------
            # Reviewer
            # ------------------------------------------------

            "review_result":
                None,


            # ------------------------------------------------
            # 全局重新规划次数
            # ------------------------------------------------

            "retry_count":
                0,


            # ------------------------------------------------
            # V3.5 Chart
            #
            # 给 Chart Planner / Chart Executor
            # 预留 State。
            # ------------------------------------------------

            "chart_plan":
                None,

            "chart_results":
                [],


            # ------------------------------------------------
            # V4.1 多格式输入
            # ------------------------------------------------

            "input_kind":
                "table",

            "input_context":
                None,

            "source_type":
                None,

            "dataset_notes":
                [],


            # ------------------------------------------------
            # V4.2 多轮对话
            # ------------------------------------------------

            "history":
                [],

            "is_followup":
                False,

            "session_id":
                None,

            "source_name":
                upload_name,

            "verification":
                None,

            "knowledge_context":
                None,

            "knowledge_hits":
                [],

            "health_findings":
                [],

            "health_context":
                None,

            "health_notice":
                None,

            "conclusion_check":
                None,


            # ------------------------------------------------
            # Final Report
            # ------------------------------------------------

            "final_report":
                None
        }


        # ====================================================
        # 运行 LangGraph
        # ====================================================

        final_state = graph.invoke(
            initial_state
        )


        # ====================================================
        # Agent Trace
        # ====================================================

        agent_trace = final_state.get(
            "agent_trace",
            []
        )

        agent_trace_view = format_agent_trace(
            agent_trace
        )


        # ====================================================
        # V3.5
        # 获取 Chart Result
        # ====================================================

        chart_results = list(
            final_state.get("chart_results") or []
        )


        # ====================================================
        # V3.5
        # 将本地图片路径转换成浏览器可访问 URL
        #
        # Chart Executor 原始结果：
        #
        # {
        #     "success": True,
        #     "chart_path":
        #         "temp_charts/chart_xxx.png"
        # }
        #
        # 增加：
        #
        # "chart_url":
        #     "/charts/chart_xxx.png"
        # ====================================================

        # V3.9：
        # 每张图表都要补上浏览器可访问的 URL，
        # 生成几张就返回几张。
        chart_results = [
            bind_chart_url(item)
            for item in chart_results
            if isinstance(item, dict)
        ]


        # ====================================================
        # 最终返回结果
        # ====================================================

        return {

            # ------------------------------------------------
            # 最终分析报告
            # ------------------------------------------------

            "final_analysis":
                final_state.get(
                    "final_report"
                ),


            # ------------------------------------------------
            # Reviewer
            # ------------------------------------------------

            "review_result":
                final_state.get(
                    "review_result"
                ),


            # ------------------------------------------------
            # 全局 Retry
            # ------------------------------------------------

            "retry_count":
                final_state.get(
                    "retry_count"
                ),


            # ------------------------------------------------
            # Executor Result
            # ------------------------------------------------

            "execution_results":
                final_state.get(
                    "execution_results"
                ),


            # ------------------------------------------------
            # Code History
            # ------------------------------------------------

            "code_history":
                final_state.get(
                    "code_history"
                ),


            # ------------------------------------------------
            # Chart Planner
            # ------------------------------------------------

            "chart_plan":
                final_state.get(
                    "chart_plan"
                ),


            # ------------------------------------------------
            # Chart Executor
            # ------------------------------------------------

            "chart_results":
                chart_results,


            # ------------------------------------------------
            # V4.4 数值核对
            # ------------------------------------------------

            "verification":
                final_state.get("verification"),

            # ------------------------------------------------
            # V4.6 业务口径
            # ------------------------------------------------

            "knowledge_hits":
                final_state.get("knowledge_hits") or [],

            # ------------------------------------------------
            # V4.11 数据体检
            # ------------------------------------------------

            "health_findings":
                final_state.get("health_findings") or [],

            # ------------------------------------------------
            # V4.12 结论核对
            # ------------------------------------------------

            "conclusion_check":
                final_state.get("conclusion_check"),


            # ------------------------------------------------
            # V4.1 数据来源
            # ------------------------------------------------

            "input_kind":
                final_state.get("input_kind"),

            "source_type":
                final_state.get("source_type"),

            "dataset_notes":
                final_state.get("dataset_notes"),


            # ------------------------------------------------
            # 完整 Agent Trace
            # ------------------------------------------------

            "agent_trace":
                agent_trace,


            # ------------------------------------------------
            # 前端专用 Trace
            # ------------------------------------------------

            "agent_trace_view":
                agent_trace_view
        }


    except Exception as e:

        import traceback

        return {

            "error":
                str(e),

            "trace":
                traceback.format_exc()
        }


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )
