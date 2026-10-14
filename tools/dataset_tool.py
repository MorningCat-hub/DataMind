import io
import pandas as pd


def get_dataset_info(file_path: str) -> str:
    """
    读取 CSV 数据集。

    返回：
    - 字段
    - 数据类型
    - 前5行
    - 基础统计信息
    """

    df = pd.read_csv(file_path)

    buf = io.StringIO()

    buf.write("=== 数据集字段 ===\n")
    buf.write(str(list(df.columns)))
    buf.write("\n\n")

    buf.write("=== 数据类型 ===\n")
    buf.write(str(df.dtypes))
    buf.write("\n\n")

    buf.write("=== 前5行样例数据 ===\n")
    buf.write(df.head().to_string())
    buf.write("\n\n")

    buf.write("=== 基础统计描述 ===\n")

    # include="all"
    # 让字符串类型字段也能够展示统计信息
    buf.write(
        df.describe(
            include="all"
        ).to_string()
    )

    return buf.getvalue()