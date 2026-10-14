"""
口径库的目录化与可用性筛选

1. 知识库目录下可以有多个 *.yaml（一个领域一个包）
2. 检索时只保留「所需字段在当前数据集里都存在」的口径
3. 但问题里**点名**的口径要留下，并标注缺哪些字段 ——
   否则「毛利率」这种存在的意义（告诉模型你缺成本、别硬算）就失效了
"""

import os

import pytest

from agent.utils.knowledge import (
    is_applicable,
    load_entries,
    retrieve,
)


def test_the_knowledge_directory_loads_every_pack(project_root):

    directory = os.path.join(project_root, "knowledge")

    packs = [
        name for name in os.listdir(directory)
        if name.endswith((".yaml", ".yml"))
    ]

    assert len(packs) >= 2, packs
    assert len(load_entries()) >= 30


def test_the_ecommerce_pack_is_loaded():

    names = {entry["name"] for entry in load_entries()}

    assert "复购率" in names
    assert "退货率" in names


def test_an_explicit_path_still_reads_one_file(project_root):

    single = os.path.join(project_root, "knowledge", "metrics.yaml")

    entries = load_entries(single)

    assert entries and all(entry.get("name") for entry in entries)


def test_every_entry_uses_real_dataset_column_names(project_root):
    """列名从仓库里的数据集实际读出来，不写死。"""

    import pandas as pd

    sources = [
        os.path.join(project_root, "test_chart_data.csv"),
        os.path.join(project_root, "eval", "datasets", "demo_orders.csv"),
        os.path.join(project_root, "eval", "datasets", "sales_2026.csv"),
        os.path.join(project_root, "data", "online_retail_small.csv"),
    ]

    # 有意为之的「缺席字段」：
    # 「毛利率」要求「成本」，而数据里没有成本列 ——
    # 这条口径存在的意义就是告诉模型「你缺成本，不要硬算利润率」。
    absent_by_design = {"成本"}

    real = set()

    for path in sources:

        if os.path.isfile(path):

            real |= set(pd.read_csv(path, nrows=0).columns)

    assert real, "至少要能读到一个数据集"

    for entry in load_entries():

        for field in entry.get("requires") or []:

            if field in absent_by_design:

                continue

            assert field in real, (entry["name"], field)


def test_applicability_needs_all_required_fields():

    entry = {"name": "x", "requires": ["a", "b"]}

    assert is_applicable(entry, ["a", "b", "c"]) is True
    assert is_applicable(entry, ["a"]) is False


def test_an_entry_without_requirements_is_always_applicable():

    assert is_applicable({"name": "x"}, ["a"]) is True


def test_without_columns_nothing_is_filtered():

    assert is_applicable({"name": "x", "requires": ["nope"]}, None) is True


def test_a_pack_for_another_dataset_is_annotated(sales_csv):
    """
    为「有退款/折扣列」写的口径，在别的数据上要么不注入，
    要么**必须被标注缺哪些字段** —— 绝不能悄悄注入一个算不出来的公式。

    注意：问题里点名提到的口径会被保留（见下一个用例），
    所以这里断言的是「保留了就必须标注」，而不是「一定不保留」。
    """

    result = retrieve(
        "净销售额是多少？", file_path=sales_csv, use_llm_fallback=False
    )

    for hit in result["hits"]:

        requires = hit["entry"].get("requires") or []

        missing = [item for item in requires if item == "退款金额"]

        if missing:

            assert hit["entry"].get("_missing_fields"), (
                "缺字段的口径必须标注 _missing_fields：%s"
                % hit["entry"]["name"]
            )


def test_the_skipped_count_is_reported(demo_csv):

    result = retrieve(
        "净销售额是多少？", file_path=demo_csv, use_llm_fallback=False
    )

    assert "skipped_inapplicable" in result
    assert result["skipped_inapplicable"] >= 1


def test_a_named_entry_survives_missing_fields(demo_csv):
    """
    「毛利率」需要成本列，而 demo 数据里没有。
    它必须仍然被注入，并明确标出缺什么 ——
    否则模型会去拿相近字段凑利润率。
    """

    result = retrieve(
        "毛利率是多少？", file_path=demo_csv, use_llm_fallback=False
    )

    names = [hit["entry"]["name"] for hit in result["hits"]]

    assert "毛利率" in names, names

    hit = [
        item for item in result["hits"] if item["entry"]["name"] == "毛利率"
    ][0]

    assert hit["entry"].get("_missing_fields"), hit["entry"]
    assert "成本" in hit["entry"]["_missing_fields"]


def test_the_block_warns_about_missing_fields(demo_csv):
    """注入给模型的文本里必须带上「缺什么、不要凑」。"""

    from agent.utils.knowledge import format_knowledge_block

    result = retrieve(
        "毛利率是多少？", file_path=demo_csv, use_llm_fallback=False
    )

    block = format_knowledge_block(result["hits"])

    assert "缺少这些字段" in block
    assert "不要用相近字段凑" in block
