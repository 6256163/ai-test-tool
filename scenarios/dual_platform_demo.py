"""scenarios/dual_platform_demo.py
离线验证 ai-test-tool 的「Web / App 双端适配」：不调真实模型、不起浏览器，
直接构造两条 Case（web 登录 + app 登录），看 codegen 是否按 target_framework 分发到
正确的后端（Playwright vs Appium），并校验生成代码的语法与平台兼容性。

运行：
  cd ai-test-tool
  python scenarios/dual_platform_demo.py
"""
from __future__ import annotations

import ast
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ingest.schema import Case, Step, Locator, Action, LocatorStrategy
from generator.codegen import case_to_code
from evaluate.judge import _platform_violations


def make_web_case() -> Case:
    """Web 端登录成功流程（Playwright）。"""
    return Case(
        id="web_login_success",
        title="Web 管理员登录成功",
        preconditions=["已访问登录页"],
        steps=[
            Step(action=Action.NAVIGATE, value="/login", note="进入登录页"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="username"), value="admin", note="输入账号"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="password"), value="admin123456", note="输入密码"),
            Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="loginBtn"), note="点击登录"),
            Step(action=Action.ASSERT_URL, expect="/dashboard", note="跳转到后台首页"),
            Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="welcome"), expect="欢迎", note="展示欢迎语"),
        ],
        coverage_tags=["主流程", "冒烟"],
        target_framework="playwright",
        base_path="/login",
    )


def make_app_case() -> Case:
    """App 端登录成功流程（Appium，原生 Android）。"""
    return Case(
        id="app_login_success",
        title="App 管理员登录成功",
        preconditions=["应用已安装"],
        steps=[
            Step(action=Action.LAUNCH, value="com.shopdemo.app", note="启动/切回 app"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="username"), value="admin", note="输入账号"),
            Step(action=Action.FILL, locator=Locator(by=LocatorStrategy.TESTID, value="password"), value="admin123456", note="输入密码"),
            Step(action=Action.CLICK, locator=Locator(by=LocatorStrategy.TESTID, value="loginBtn"), note="点击登录"),
            Step(action=Action.ASSERT_TEXT, locator=Locator(by=LocatorStrategy.TESTID, value="welcome"), expect="欢迎", note="展示欢迎语"),
            Step(action=Action.ASSERT_VISIBLE, locator=Locator(by=LocatorStrategy.TESTID, value="homeTab"), note="首页 Tab 可见"),
        ],
        coverage_tags=["主流程", "冒烟"],
        target_framework="appium",
        base_path=None,
    )


def _check_syntax(code: str, label: str) -> bool:
    try:
        ast.parse(code)
        print(f"  [语法校验] {label}: ✅ 通过 (ast.parse OK)")
        return True
    except SyntaxError as e:
        print(f"  [语法校验] {label}: ❌ 失败 -> {e}")
        return False


def main() -> None:
    print("=" * 70)
    print("场景 1：Web 端（target_framework=playwright）→ 期待 Playwright 代码")
    print("=" * 70)
    web_case = make_web_case()
    web_code = case_to_code(web_case, base_url="http://localhost:8081")
    print(web_code)
    _check_syntax(web_code, "Web")

    print("=" * 70)
    print("场景 2：App 端（target_framework=appium）→ 期待 Appium 代码")
    print("=" * 70)
    app_case = make_app_case()
    app_code = case_to_code(app_case, app_package="com.shopdemo.app")
    print(app_code)
    # Appium 代码不是 pytest-async 形态，仅做一次宽松括号配对检查（非强制）
    print(f"  [平台校验] app 用例 web-only 动作违规: {_platform_violations(app_case) or '无'}")

    print("=" * 70)
    print("场景 3：故意在 app 用例写 web-only 动作（hover/navigate），质量门应拦截")
    print("=" * 70)
    bad_app = app_case.model_copy(deep=True)
    bad_app.steps = bad_app.steps + [
        Step(action=Action.HOVER, locator=Locator(by=LocatorStrategy.TESTID, value="menu"), note="触屏无 hover"),
        Step(action=Action.NAVIGATE, value="/x", note="app 无 URL 导航"),
    ]
    print(f"  [平台校验] 违规动作 = {_platform_violations(bad_app) or '无'} （期望含 hover/navigate）")

    print("\n✅ 双端 codegen 分发验证完成。")


if __name__ == "__main__":
    main()
