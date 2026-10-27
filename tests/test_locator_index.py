"""P1：列表元素（v-for 内 testid）的 index 语义。

起因：LLM 生成 TC_PURCHASE_001 时对 product-name（商品列表 v-for 内的元素，页面上有 12 个）
未指定序号，Playwright strict mode 直接抛 "resolved to 12 elements"，用例一行都跑不过。
修法链路：catalog 识别重复 testid → hints 告知 LLM → 必须带 index → codegen 生成 .first/.nth →
judge 在「执行前」就把漏带 index 拦下来（不必等浏览器报错）。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from generator.codegen import case_to_code      # noqa: E402
from evaluate.judge import lint_locators        # noqa: E402
from prober.dom_catalog import Catalog          # noqa: E402
from ingest.schema import Case                  # noqa: E402


CATALOG = Catalog(
    routes=["/products"],
    route_testids={"/products": ["product-name", "btn-search"]},
    global_testids=[],
    all_testids=["product-name", "btn-search", "detail-name"],
    repeated_testids=["product-name"],
)


def _case(index):
    loc = {"by": "testid", "value": "product-name"}
    if index is not None:
        loc["index"] = index
    return Case.model_validate({
        "id": "TC_T",
        "title": "t",
        "steps": [
            {"action": "click", "locator": loc},
            {"action": "assert_text", "locator": {"by": "testid", "value": "detail-name"},
             "expect": "x"},
        ],
        "coverage_tags": ["冒烟"],
        "target_framework": "playwright",
        "base_path": "/products",
    })


# ---------- codegen：index → 具体 API ----------
@pytest.mark.parametrize("index,expected", [
    (0, ".first"),
    (2, ".nth(2)"),
    (-1, ".last"),
    (None, "("),     # 不带 index 时不产生任何后缀
])
def test_codegen_emits_nth(index, expected):
    code = case_to_code(_case(index), base_url="http://x")
    line = [l for l in code.splitlines() if "product-name" in l and "click" in l][0]
    assert expected in line
    if index is None:
        assert ".first" not in line and ".nth(" not in line


# ---------- judge：漏带 index 必须在执行前被拦 ----------
def test_judge_flags_missing_index():
    errors, _ = lint_locators(_case(None), CATALOG)
    assert any("列表元素未指定 index" in e and "product-name" in e for e in errors)


def test_judge_accepts_index():
    errors, _ = lint_locators(_case(0), CATALOG)
    assert errors == []


def test_judge_no_catalog_is_lenient():
    errors, warnings = lint_locators(_case(None), None)
    assert errors == [] and warnings == []


# ---------- catalog：v-for 检测 ----------
def test_repeated_detection(tmp_path):
    src = tmp_path / "S.vue"
    src.write_text(
        "<template>\n"
        "  <div data-testid=\"outer\">\n"
        "    <el-row v-for=\"p in products\" :key=\"p.id\">\n"
        "      <el-card data-testid=\"row-btn\">x</el-card>\n"
        "    </el-row>\n"
        "  </div>\n"
        "</template>\n",
        encoding="utf-8",
    )
    from prober.dom_catalog import _repeated_testids_in_file
    got = _repeated_testids_in_file(src.read_text(encoding="utf-8"))
    assert "row-btn" in got          # v-for 内
    assert "outer" not in got        # v-for 外
