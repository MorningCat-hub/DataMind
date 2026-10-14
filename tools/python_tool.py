import io
import sys
import time
from typing import Any, Dict

import pandas as pd


def run_stat_calc(
    file_path: str,
    expression: str
) -> Dict[str, Any]:
    """
    执行 Pandas 数据分析代码。

    V3.2 改动：
    =========================

    工具统一返回结构化结果：

    成功：

    {
        "success": True,
        "data": {...},
        "error": None,
        "metadata": {...}
    }

    失败：

    {
        "success": False,
        "data": None,
        "error": {...},
        "metadata": {...}
    }

    ⚠️ 注意：

    当前版本仍然使用 exec。

    这是为了方便 DataMind V3 的学习和开发。

    正式生产环境不能直接执行
    LLM 生成的 Python 代码。

    V4 可以升级：

    - Docker Sandbox
    - 独立 Python Execution Service
    - Restricted Environment
    """

    # =========================
    # V3.2
    # 记录工具执行开始时间
    # =========================

    start_time = time.time()

    try:

        # =========================
        # 读取数据
        # =========================

        df = pd.read_csv(
            file_path
        )

        # =========================
        # 构建代码执行环境
        # =========================

        local_vars = {
            "df": df,
            "pd": pd,
            "result": None
        }

        # =========================
        # 捕获 print 输出
        # =========================

        old_stdout = sys.stdout

        new_stdout = io.StringIO()

        sys.stdout = new_stdout

        try:

            exec(
                expression,
                {},
                local_vars
            )

        finally:

            # 无论成功还是失败
            # 都必须恢复 stdout

            sys.stdout = old_stdout

        # =========================
        # 获取执行结果
        # =========================

        output = new_stdout.getvalue()

        result = local_vars.get(
            "result"
        )

        execution_time = (
            time.time() - start_time
        )

        # =========================
        # V3.2
        # 成功：统一结构化返回
        # =========================

        return {
            "success": True,

            "data": {

                # print 输出
                "output": output,

                # result 变量
                "result": str(result)
            },

            "error": None,

            "metadata": {

                "tool": "run_stat_calc",

                "execution_time": round(
                    execution_time,
                    4
                )
            }
        }

    except Exception as e:

        execution_time = (
            time.time() - start_time
        )

        # =========================
        # V3.2
        # 失败：统一结构化返回
        # =========================

        return {
            "success": False,

            "data": None,

            "error": {

                "type": type(e).__name__,

                "message": str(e)
            },

            "metadata": {

                "tool": "run_stat_calc",

                "execution_time": round(
                    execution_time,
                    4
                )
            }
        }