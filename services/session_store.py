"""
DataMind V4.2
会话存储（多轮对话）

多轮对话需要记住：

1. 用户上传的数据集（归一化之后的 CSV 路径）
2. 数据集说明 / 来源 / 提示
3. 非表格内容（Word 正文、图片解读结果）
4. 历史问答

当前实现是进程内内存存储，适合本地单用户使用：
进程重启后会话会丢失。

要升级成持久化，只需要把这里的字典换成
SQLite / Redis，接口不用改。
"""

import threading
import time
import uuid
from typing import Any, Dict, Optional

from services import store


# 最多保留多少个会话
MAX_SESSIONS = 50

# 会话闲置多久后回收（秒）
SESSION_TTL_SECONDS = 6 * 60 * 60


def empty_session(session_id: str) -> Dict[str, Any]:
    """新建一个空会话。"""

    now = time.time()

    return {
        "session_id": session_id,
        "created_at": now,
        "last_active": now,

        # 数据集
        "file_path": None,
        "dataset_info": None,
        "input_kind": "none",
        "input_context": None,
        "source_type": None,
        "dataset_notes": [],

        # 历史问答
        # V4.3：原始文件名
        "source_name": None,

        "history": [],
        "turn": 0,
    }


def has_dataset(session: Optional[Dict[str, Any]]) -> bool:
    """
    这个会话是否已经加载过数据。

    input_kind 为 error 时不算加载成功。
    """

    if not session:

        return False

    if session.get("input_kind") not in ("table", "text"):

        return False

    return bool(
        session.get("file_path")
        or session.get("input_context")
    )


class SessionStore:
    """进程内会话存储。"""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ):

        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds

    # ------------------------------------
    # 基础操作
    # ------------------------------------

    def create(
        self,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新建会话。传入已有 id 时会覆盖同 id 的旧会话。"""

        with self._lock:

            self._purge_locked()

            new_id = session_id or uuid.uuid4().hex

            session = empty_session(new_id)

            self._sessions[new_id] = session

            # V4.14：落库，重启之后还在
            store.save_conversation(session)

            self._trim_locked()

            return dict(session)

    def get(
        self,
        session_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """取会话；不存在返回 None。"""

        if not session_id:

            return None

        with self._lock:

            session = self._sessions.get(session_id)

            if session is None:

                # V4.14：内存里没有就去数据库找 ——
                # 这就是「重启后还能接着聊」的地方
                restored = store.load_conversation(session_id)

                if restored is None:

                    return None

                session = restored

                session["last_active"] = time.time()

                self._sessions[session_id] = session

                # 恢复出来的会话也要受容量约束：
                # 否则每次 get() 一个老会话，内存就会慢慢超过上限。
                # 它现在是「最近活跃」，所以被淘汰的是别人。
                self._trim_locked()

            session["last_active"] = time.time()

            return dict(session)

    def drop(self, session_id: str) -> None:
        """删除会话。"""

        with self._lock:

            self._sessions.pop(session_id, None)

        store.delete_conversation(session_id)

    # ------------------------------------
    # 数据集
    # ------------------------------------

    def reset_dataset(self, session_id: str) -> None:
        """
        清掉会话里的数据集与历史。

        上传新文件时调用 —— 这是一个全新的分析上下文。
        """

        with self._lock:

            session = self._sessions.get(session_id)

            if session is None:

                return

            fresh = empty_session(session_id)

            fresh["created_at"] = session.get(
                "created_at", fresh["created_at"]
            )

            self._sessions[session_id] = fresh

    def remember(
        self,
        session_id: str,
        final_state: Dict[str, Any],
        question: str,
        answer: str,
    ) -> None:
        """
        把这一轮的结果写回会话。

        包括：数据集信息、历史问答、轮次。
        """

        with self._lock:

            session = self._sessions.get(session_id)

            if session is None:

                return

            for key in (
                "dataset_info",
                "file_path",
                "input_context",
                "source_type",
                "source_name",
            ):

                value = final_state.get(key)

                if value:

                    session[key] = value

            kind = final_state.get("input_kind")

            if kind:

                session["input_kind"] = kind

            notes = final_state.get("dataset_notes")

            if notes:

                session["dataset_notes"] = list(notes)

            session["history"].append(
                {
                    "question": question,
                    "answer": answer or "",
                }
            )

            session["turn"] += 1
            session["last_active"] = time.time()

        # V4.14：每一轮都落库
        store.save_conversation(session)

    # ------------------------------------
    # 回收
    # ------------------------------------

    def _purge_locked(self) -> None:
        """清理超时会话。"""

        now = time.time()

        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.get("last_active", now) > self._ttl
        ]

        for key in expired:

            self._sessions.pop(key, None)

    def _trim_locked(self) -> None:
        """超过上限时丢掉最久未活动的会话。"""

        if len(self._sessions) <= self._max_sessions:

            return

        ordered = sorted(
            self._sessions.items(),
            key=lambda item: item[1].get("last_active", 0),
        )

        for key, _session in ordered[
            : len(self._sessions) - self._max_sessions
        ]:

            self._sessions.pop(key, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


# 全局单例
session_store = SessionStore()
