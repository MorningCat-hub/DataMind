"""
临时文件清理

项目原本没有任何清理逻辑，temp/ 和 temp_charts/ 会一直堆积
（实测已经堆到 160 个文件 / 3.2 MB）。

清理规则：只删自己生成的 data_*.csv / chart_*.png，
只删超过 TTL 的，不做递归删除。
"""

import os
import time

import pytest


def test_cleanup_exists_with_a_sane_ttl():

    import main

    assert callable(main.cleanup_temp_files)
    assert isinstance(main.TEMP_TTL_HOURS, int)
    assert 1 <= main.TEMP_TTL_HOURS <= 24 * 30


def test_importing_main_does_not_clean_the_repo(project_root):
    """
    回归：清理曾经写在模块顶层，于是「import main」就会去删项目里的文件
    （测试因此产生副作用）。现在挂在应用启动（lifespan）上。
    """

    source = open(
        os.path.join(project_root, "main.py"), encoding="utf-8"
    ).read()

    assert "asynccontextmanager" in source
    assert "lifespan" in source

    # 顶层不该再有裸调用
    top_level = [
        line for line in source.splitlines()
        if line.strip() == "cleanup_temp_files()"
        and not line.startswith(" ")
    ]

    assert top_level == [], top_level


def test_cleanup_evicts_only_old_matching_files(isolated_temp):

    import main

    temp_dir = isolated_temp["temp"]
    chart_dir = isolated_temp["charts"]

    old = time.time() - (main.TEMP_TTL_HOURS + 2) * 3600

    stale_csv = os.path.join(temp_dir, "data_old.csv")
    stale_png = os.path.join(chart_dir, "chart_old.png")
    fresh_csv = os.path.join(temp_dir, "data_new.csv")
    foreign = os.path.join(temp_dir, "keep_me.txt")

    for path in (stale_csv, stale_png, fresh_csv, foreign):

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")

    for path in (stale_csv, stale_png):

        os.utime(path, (old, old))

    removed = main.cleanup_temp_files()

    assert not os.path.exists(stale_csv)
    assert not os.path.exists(stale_png)
    assert os.path.exists(fresh_csv), "新文件必须保留"
    assert os.path.exists(foreign), "只删自己生成的文件"
    assert removed >= 2


def test_cleanup_does_not_recurse(project_root):

    source = open(
        os.path.join(project_root, "main.py"), encoding="utf-8"
    ).read()

    body = source.split("def cleanup_temp_files")[1].split("cleanup_temp_files()")[0]

    assert "walk(" not in body
    assert "rmtree" not in body
