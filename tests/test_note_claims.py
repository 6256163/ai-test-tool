"""P1 补充规则：note 里承诺的校验必须真的存在。

起因：LLM 在 TC_PURCHASE_001 的步骤 13 写了
「校验购物车已包含该产品，且购物车单价与详情页 detail-price 一致（运行时比对）」，
但 `detail-price` 在整条用例里从未被任何步骤定位过，代码里也自然没有对应的 assert。

这类「承诺未兑现」比断言写错更危险：代码照样跑绿，却让人误以为价格一致性被覆盖到了。
judge 必须把它当硬伤拦下来。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evaluate.judge import lint_locators  # noqa: E402
from ingest.schema import Case  # noqa: E402
from prober.dom_catalog import Catalog  # noqa: E402

CATALOG = Catalog(
    routes=["/cart"],
    route_testids={"/cart": ["cart-table", "cart-count", "detail-price"]},
    global_testids=[],
    all_testids=["cart-table", "cart-count", "detail-price", "detail-name"],
    repeated_testids=[],
)


def _purchase_case(note: str, extra_step: dict | None = None) -> Case:
    steps = [
        {"action": "assert_text", "locator": {"by": "testid", "value": "detail-name"},
         "expect": "旗舰版"},
        {"action": "assert_text", "locator": {"by": "testid", "value": "cart-table"},
         "expect": "旗舰版", "note": note},
    ]
    if extra_step:
        steps.insert(1, extra_step)
    return Case.model_validate({
        "id": "TC_PURCHASE_001",
        "title": "加购后校验购物车",
        "steps": steps,
        "coverage_tags": ["主流程"],
        "target_framework": "playwright",
        "base_path": "/cart",
    })


def test_note_claim_without_assertion_is_error():
    """note 点名 detail-price，但没有任何步骤定位它 → 硬伤。"""
    errors, _ = lint_locators(
        _purchase_case("校验购物车单价与详情页 detail-price 一致（运行时比对）"), CATALOG)
    assert any("承诺的校验未实现" in e or "该校验并未真正实现" in e for e in errors)
    assert any("detail-price" in e for e in errors)


def test_note_claim_satisfied_by_real_step():
    """补上真正的 assert 步骤后，同一句 note 就不再是问题。"""
    errors, _ = lint_locators(
        _purchase_case(
            "校验购物车单价与详情页 detail-price 一致（运行时比对）",
            {"action": "assert_text", "locator": {"by": "testid", "value": "detail-price"},
             "expect": "22.4"},
        ), CATALOG)
    assert errors == []


def test_note_without_testid_token_is_clean():
    """普通中文 note 不应被误伤（正则只认「像 testid」的 token）。"""
    errors, _ = lint_locators(_purchase_case("校验购物车已包含该产品"), CATALOG)
    assert errors == []


def test_note_claiming_unknown_testid_is_ignored():
    """note 里提到的东西不在前端目录里（可能是外部系统），不应按本规则报错。"""
    errors, _ = lint_locators(_purchase_case("与支付系统 pay-gateway 的金额比对"), CATALOG)
    assert not any("pay-gateway" in e for e in errors)


def test_no_catalog_is_lenient():
    errors, warnings = lint_locators(_purchase_case("参考 detail-price"), None)
    assert errors == [] and warnings == []
