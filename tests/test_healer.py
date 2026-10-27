"""tests/test_healer.py —— 定位器自愈（Healer）单元测试。

覆盖：
  - parse_failures：识别 strict mode / not found / visible timeout
  - heal_case：strict mode 补 index=0；catalog 外 testid 做 difflib 重命名；
    catalog 内但当前页没有 → 需评审；text 找不到 → 需评审；断言 expect 不被修改（护栏）
  - heal_and_rerun：修补后重跑直到通过（mock execute）
"""
from __future__ import annotations

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ingest.schema import Case, Step, Locator, Action, LocatorStrategy  # noqa: E402
from prober.dom_catalog import Catalog  # noqa: E402
from runner.executor import RunResult  # noqa: E402
from runner.healer import (  # noqa: E402
    parse_failures, heal_case, heal_and_rerun, FailType, Failure,
)


# 一个小而真实的 catalog（取自 target_demo 的若干 testid）
CATALOG = Catalog.from_dict({
    "routes": ["/login", "/products", "/cart"],
    "route_testids": {
        "/login": ["input-username", "input-password", "btn-login", "current-user"],
        "/products": ["input-keyword", "btn-search", "product-name", "detail-name"],
        "/cart": ["cart-table", "cart-count", "btn-cart-clear"],
    },
    "global_testids": ["current-user"],
    "all_testids": [
        "input-username", "input-password", "btn-login", "current-user",
        "input-keyword", "btn-search", "product-name", "detail-name",
        "cart-table", "cart-count", "btn-cart-clear",
    ],
    "repeated_testids": ["product-name"],
})


def _case_with(steps: list[Step]) -> Case:
    return Case(
        id="TC_HEAL_001", title="healer 测试",
        preconditions=[], steps=steps,
        coverage_tags=["回归"], target_framework="playwright", base_path="/login",
    )


def test_parse_strict_mode():
    out = (
        "tests/gen.py:27: in test_x\n"
        "    await expect(page.get_by_test_id('product-name').first).to_contain_text('x')\n"
        "E   playwright._impl._errors.Error: strict mode violation: "
        "locator.getByTestId('product-name') resolved to 12 elements.\n"
    )
    fails = parse_failures(out)
    assert len(fails) == 1
    assert fails[0].fail_type == FailType.STRICT_MODE
    assert fails[0].by == "testid"
    assert fails[0].value == "product-name"


def test_parse_not_found():
    out = (
        "E   playwright._impl._errors.Error: "
        "page.get_by_test_id(\"input-user\").fill: Timeout 30000ms exceeded.\n"
    )
    fails = parse_failures(out)
    assert len(fails) == 1
    assert fails[0].fail_type == FailType.NOT_FOUND
    assert fails[0].value == "input-user"


def test_parse_visible_timeout():
    out = (
        "E   playwright._impl._errors.Error: "
        "locator.getByTestId('menu-cart') is not visible\n"
    )
    fails = parse_failures(out)
    assert fails[0].fail_type == FailType.TIMEOUT_VISIBLE


def test_heal_strict_mode_adds_index():
    steps = [
        Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="product-name"),
             expect="旗舰版"),
        Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="product-name")),
    ]
    case = _case_with(steps)
    fails = [Failure(FailType.STRICT_MODE, "testid", "product-name")]
    out = heal_case(case, fails, CATALOG)
    assert len(out.applied) == 2
    # 两步的 product-name 都补了 index=0
    assert out.healed_case.steps[0].locator.index == 0
    assert out.healed_case.steps[1].locator.index == 0
    # 护栏：expect 不能被动
    assert out.healed_case.steps[0].expect == "旗舰版"


def test_heal_rename_via_catalog():
    steps = [
        Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="input-user"),
             value="admin"),
    ]
    case = _case_with(steps)
    fails = [Failure(FailType.NOT_FOUND, "testid", "input-user")]
    out = heal_case(case, fails, CATALOG)
    assert len(out.applied) == 1
    # input-user → input-username（catalog 里最接近的）
    assert out.healed_case.steps[0].locator.value == "input-username"


def test_heal_not_found_in_catalog_needs_review():
    # testid 在当前页没有但 catalog 里有（可能是路由/上下文错）→ 不猜，交人工
    steps = [
        Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="current-user"),
             expect="admin"),
    ]
    case = _case_with(steps)
    fails = [Failure(FailType.NOT_FOUND, "testid", "current-user")]
    out = heal_case(case, fails, CATALOG)
    assert len(out.needs_review) == 1
    assert out.healed_case.steps[0].locator.value == "current-user"  # 没被改


def test_heal_unhealable_no_matching_step():
    steps = [Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="btn-login"))]
    case = _case_with(steps)
    fails = [Failure(FailType.NOT_FOUND, "testid", "ghost-element")]
    out = heal_case(case, fails, CATALOG)
    assert len(out.unhealable) == 1
    assert out.healed_case is not None


def test_heal_and_rerun_loops_until_pass():
    # mock execute：第一次未自愈（无 .first）失败，自愈补 .first 后通过
    strict_out = (
        "E   Error: strict mode violation: locator.getByTestId('product-name') resolved to 12 elements.\n"
    )

    def mock_execute(code: str) -> RunResult:
        if ".first" in code:  # 已自愈
            return RunResult(0, 1, 0, "passed")
        return RunResult(1, 0, 1, strict_out)

    steps = [
        Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="product-name"),
             expect="旗舰版"),
    ]
    case = _case_with(steps)
    out = heal_and_rerun(case, catalog=CATALOG, execute_fn=mock_execute, base_url="http://x", max_attempts=3)
    assert out.final_ok is True
    assert len(out.applied) == 1
    assert len(out.runs) == 2  # 失败 1 次 + 自愈后通过 1 次
    assert out.healed_case.steps[0].locator.index == 0


def test_heal_and_rerun_stops_when_unfixable():
    # 逻辑失败（断言 expect 不匹配），不是 locator 问题 → 自愈无能为力，不空转
    logic_out = "E   AssertionError: assert '1' == '2'\n"

    def mock_execute(code: str) -> RunResult:
        return RunResult(1, 0, 1, logic_out)

    steps = [
        Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="cart-count"),
             expect="1"),
    ]
    case = _case_with(steps)
    out = heal_and_rerun(case, catalog=CATALOG, execute_fn=mock_execute, base_url="http://x", max_attempts=3)
    assert out.final_ok is False
    assert len(out.applied) == 0  # 没有任何可应用的修补
    assert len(out.runs) == 1     # 只跑了一轮就停，没有对逻辑问题空转
