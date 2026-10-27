"""scenarios/demo.py —— 离线演示：不调真实模型，用预置用例验证 P0 全链路。

演示你画的 P0 验收标准：
  「拿 3 个真实业务场景，AI 生成的用例能跑通、误判率 < 阈值」
这里用 MockLLM 产出 3 个场景（登录 / 搜索 / 加购）的 Case，
codegen 翻译成 Playwright 代码，并做语法校验 + 质量门自检。
（真正 `pytest` 跑起来需要本地装了 playwright 浏览器，本 demo 只校验到代码层。）
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

from ingest.schema import Case, Step, Locator, Action, LocatorStrategy  # noqa: E402
from generator.codegen import case_to_code  # noqa: E402
from generator.llm_client import LLMClient  # noqa: E402
from evaluate.judge import gate  # noqa: E402


# 预置 3 个真实业务场景（模拟 LLM 的结构化输出）
SCENARIOS: list[Case] = [
    Case(
        id="TC_LOGIN_001", title="账号密码登录成功",
        preconditions=["已注册用户存在", "处于登录页"],
        base_path="/login",
        coverage_tags=["冒烟", "主流程"],
        steps=[
            Step(action=Action.NAVIGATE, value="/login"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="phone"), value="13800000000"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="password"), value="Test@1234"),
            Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="login-btn")),
            Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TEXT, value="欢迎回来"), expect="欢迎回来"),
        ],
    ),
    Case(
        id="TC_SEARCH_001", title="商品搜索并返回结果",
        preconditions=["首页已加载"],
        base_path="/",
        coverage_tags=["主流程", "回归"],
        steps=[
            Step(action=Action.NAVIGATE, value="/"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="search-input"), value="运动鞋"),
            Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="search-btn")),
            Step(action=Action.WAIT, value="800"),
            Step(action=Action.ASSERT_COUNT, locator=Locator(by=LocatorStrategy.TESTID, value="product-card"), value="1"),
        ],
    ),
    Case(
        id="TC_CART_001", title="加入购物车",
        preconditions=["已登录", "商品详情页已打开"],
        base_path="/product/1001",
        coverage_tags=["主流程"],
        steps=[
            Step(action=Action.NAVIGATE, value="/product/1001"),
            Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="add-to-cart")),
            Step(action=Action.WAIT, value="500"),
            Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="cart-count"), expect="1"),
        ],
    ),
]


class MockLLMClient(LLMClient):
    """不联网：按关键词命中预置用例，模拟 generate_case。"""

    def __init__(self):
        self._scenarios = SCENARIOS

    def generate_case(self, requirement: str, *, system_prompt=None) -> Case:
        for c in self._scenarios:
            if any(k in requirement for k in ("登录", "login")) and "LOGIN" in c.id:
                return c
            if any(k in requirement for k in ("搜索", "search")) and "SEARCH" in c.id:
                return c
            if any(k in requirement for k in ("购物车", "cart", "加购")) and "CART" in c.id:
                return c
        return self._scenarios[0]


def main() -> None:
    llm = MockLLMClient()
    print("P0 演示：3 个真实业务场景 → 代码生成 → 语法校验 → 质量门\n")
    for req in ["用户登录", "搜索商品", "加入购物车"]:
        case = llm.generate_case(req)
        code = case_to_code(case)
        ast.parse(code)  # 语法校验：生成的代码必须是合法 Python
        # 质量门自检（run 用占位对象，仅验证「有断言 / 标签」逻辑）
        class _FakeRun:  # noqa
            exit_code = 0
            failed = 0
            output = ""

            @property
            def ok(self):
                return self.exit_code == 0 and self.failed == 0
        passed, reasons = gate(case, _FakeRun(), code)
        print(f"--- 场景：{req} | 质量门：{'PASS' if passed else 'FAIL ' + str(reasons)} ---")
        print(code)
        print()


if __name__ == "__main__":
    main()
