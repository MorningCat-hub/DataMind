"""
DataMind V4.15
对话记录持久化（文件版）

解决的问题：

    之前会话只存在内存里，重启服务对话就没了 ——
    那是个演示页，不是一个应用。

存法：

    **一个会话一个 JSON 文件**，放在 data/conversations/ 下。

        data/conversations/
            3f2a9c1b....json

    文件内容就是会话本身（数据绑定、每一轮的问答、报告正文），
    没有派生字段 —— 打开文件就能看懂这次分析发生了什么。

为什么用文件而不是数据库：

    · 能直接读、diff、grep、备份，不需要任何工具
    · 没有 schema，没有迁移，不用解释"那个 .db 文件是什么"
    · 单机单用户，没有并发写入问题

代价（说清楚）：

    列表要读目录下所有文件，因为摘要字段（标题、轮数）就存在各自文件里。
    本地几百个会话、每个几十 KB，是毫秒级。
    真要涨到那个量级以上，再加一个索引文件即可。

原子写：

    先写 .tmp 再 os.replace。
    中途崩了也不会留下半个 JSON 文件。
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional


DEFAULT_DIR = os.path.join(
    os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    ),
    "data",
    "conversations",
)

# 会话 id 会变成文件名，所以必须挡住路径分隔符之类的东西
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_lock = threading.RLock()

_state = {
    "dir": DEFAULT_DIR,
}


# ========================================
# 目录
# ========================================

def set_store_dir(path: Optional[str]) -> None:
    """切换存储目录（测试用）。"""

    with _lock:

        _state["dir"] = path or DEFAULT_DIR


def store_dir() -> str:

    return _state["dir"]


def _ensure_dir():
    """
    确保存储目录存在，返回目录路径。

    建不出来就返回 None —— 历史记录是便利功能，
    **不该让分析因为这些原因失败**：

        只读安装、磁盘满、工作目录不对、权限不足……
    """

    target = _state["dir"]

    try:

        os.makedirs(target, exist_ok=True)

    except OSError:

        return None

    return target


def init_store() -> None:

    with _lock:

        _ensure_dir()


def reset_store(path: Optional[str] = None) -> None:
    """清空存储目录（测试用）。"""

    if path is not None:

        set_store_dir(path)

    with _lock:

        target = _ensure_dir()

        if target is None:

            return

        for name in os.listdir(target):

            if name.endswith(".json"):

                try:

                    os.remove(os.path.join(target, name))

                except OSError:

                    pass


def _path_for(session_id: str) -> Optional[str]:

    if not session_id or not SAFE_ID.match(str(session_id)):

        return None

    return os.path.join(_state["dir"], "{}.json".format(session_id))


# ========================================
# 写入
# ========================================

def _title_from(session: Dict[str, Any]) -> str:

    for turn in session.get("history") or []:

        question = " ".join(
            str((turn or {}).get("question") or "").split()
        )

        if question:

            return question[:40]

    return "新的分析"


def _summary(session: Dict[str, Any]) -> Dict[str, Any]:
    """
    列表页要用的摘要。

    这些字段直接写进文件，列表时就不用解析完整会话 ——
    不过为了简单，读的时候还是整份读。
    """

    return {
        "title": _title_from(session),
        "created_at": session.get("created_at") or time.time(),
        "updated_at": time.time(),
        "turn": int(session.get("turn") or 0),
        "source_name": session.get("source_name"),
        "has_dataset": bool(session.get("file_path")),
    }


def save_conversation(session: Dict[str, Any]) -> None:
    """
    写入 / 更新一条会话。

    文件内容 = 摘要 + 会话本体。
    """

    if not session or not session.get("session_id"):

        return

    path = _path_for(session["session_id"])

    if path is None:

        return

    with _lock:

        if _ensure_dir() is None:

            return

        created_at = session.get("created_at")

        if not created_at:

            # 已存在就沿用原来的创建时间
            existing = _read_file(path)

            if existing:

                created_at = existing.get("created_at")

        session = dict(session)

        session["created_at"] = created_at or time.time()

        payload = {
            "session_id": session["session_id"],
            "title": _title_from(session),
            "created_at": session["created_at"],
            "updated_at": time.time(),
            "turn": int(session.get("turn") or 0),
            "source_name": session.get("source_name"),
            "has_dataset": bool(session.get("file_path")),
            "session": session,
        }

        try:

            temporary = path + ".tmp"

            with open(temporary, "w", encoding="utf-8") as handle:

                json.dump(payload, handle, ensure_ascii=False, indent=2)

            os.replace(temporary, path)

        except OSError:

            # 存不下就算了，分析结果已经返回给用户了
            return


def rename_conversation(session_id: str, title: str) -> bool:

    title = " ".join(str(title or "").split())[:60]

    if not title:

        return False

    path = _path_for(session_id)

    if path is None or not os.path.isfile(path):

        return False

    with _lock:

        payload = _read_file(path)

        if payload is None:

            return False

        payload["title"] = title

        payload["updated_at"] = time.time()

        try:

            temporary = path + ".tmp"

            with open(temporary, "w", encoding="utf-8") as handle:

                json.dump(payload, handle, ensure_ascii=False, indent=2)

            os.replace(temporary, path)

        except OSError:

            return False

    return True


def delete_conversation(session_id: str) -> bool:

    path = _path_for(session_id)

    if path is None or not os.path.isfile(path):

        return False

    with _lock:

        try:

            os.remove(path)

        except OSError:

            return False

    return True


# ========================================
# 读取
# ========================================

def _read_file(path: str) -> Optional[Dict[str, Any]]:

    try:

        with open(path, "r", encoding="utf-8") as handle:

            payload = json.load(handle)

    except (OSError, ValueError):

        return None

    if isinstance(payload, dict):

        return payload

    return None


def load_conversation(session_id: str) -> Optional[Dict[str, Any]]:

    path = _path_for(session_id)

    if path is None or not os.path.isfile(path):

        return None

    with _lock:

        payload = _read_file(path)

    if payload is None:

        return None

    session = payload.get("session")

    if isinstance(session, dict):

        return session

    # 兼容：万一文件里直接就是会话本体
    if payload.get("history") is not None:

        return payload

    return None


def list_conversations(limit: int = 50) -> List[Dict[str, Any]]:
    """
    列表页摘要，按最近更新排序。

    读目录下所有文件 —— 摘要字段就在各自文件里。
    """

    with _lock:

        target = _ensure_dir()

        if target is None:

            return []

        items: List[Dict[str, Any]] = []

        try:

            names = os.listdir(target)

        except OSError:

            return []

        for name in names:

            if not name.endswith(".json"):

                continue

            payload = _read_file(os.path.join(target, name))

            if payload is None:

                continue

            items.append({
                "session_id": payload.get("session_id")
                or name[:-5],
                "title": payload.get("title"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "turn": payload.get("turn") or 0,
                "source_name": payload.get("source_name"),
                "has_dataset": bool(payload.get("has_dataset")),
            })

    items.sort(
        key=lambda item: item.get("updated_at") or 0,
        reverse=True,
    )

    return items[: int(limit)]


def count_conversations() -> int:

    with _lock:

        target = _ensure_dir()

        if target is None:

            return 0

        try:

            return len([
                name for name in os.listdir(target)
                if name.endswith(".json")
            ])

        except OSError:

            return 0


# ========================================
# 导出
# ========================================

def export_markdown(session_id: str) -> Optional[str]:
    """
    把一条对话导出成 Markdown。
    """

    session = load_conversation(session_id)

    if session is None:

        return None

    lines = [
        "# {}".format(_title_from(session)),
        "",
    ]

    if session.get("source_name"):

        lines.append("- 数据文件：{}".format(session["source_name"]))

    lines.append("- 轮数：{}".format(session.get("turn") or 0))
    lines.append("")

    for index, turn in enumerate(session.get("history") or [], 1):

        turn = turn or {}

        lines.append("## 第 {} 轮".format(index))
        lines.append("")
        lines.append("**问题**：{}".format(
            " ".join(str(turn.get("question") or "").split())
        ))
        lines.append("")
        lines.append(str(turn.get("answer") or "").strip())
        lines.append("")

    return "\n".join(lines)
