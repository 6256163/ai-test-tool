"""generator/codegen.py —— 把 Case schema 转成可运行的测试代码。

双端适配核心：Case schema 是「平台无关」的契约（动作原子 + 定位器 + 覆盖标签），
本模块把"翻译"职责下放到「后端（Backend）」多态对象：
  - PlaywrightBackend  → 生成 pytest + Playwright 代码（web 端，可叠加移动端 H5 视口模拟）
  - AppiumBackend      → 生成 Appium（原生 App）代码骨架

新增动作映射：
  NAVIGATE      → page.goto(...)              [web]
  CLICK         → locator.click()            [web/app]
  FILL          → locator.fill(value)        [web] / send_keys [app]
  HOVER         → locator.hover()            [web-only]
  SELECT        → locator.select_option(v)   [web-only]
  WAIT          → page.wait_for_timeout(ms)  [web] / time.sleep [app]
  SWIPE         → 手势滑动                   [app]
  SCROLL        → 滚动到元素                 [web/app]
  LONG_PRESS    → 长按                       [app]
  LAUNCH        → activate_app(pkg)          [app，替代 navigate]
  ASSERT_TEXT   → expect(loc).to_contain_text / loc.text ==        [web/app]
  ASSERT_VISIBLE→ expect(loc).to_be_visible / loc.is_displayed()   [web/app]
  ASSERT_URL    → expect(page).to_have_url                          [web]
  ASSERT_COUNT  → expect(loc).to_have_count(int) / len(...)==       [web/app]
"""
from __future__ import annotations

import re
import textwrap

from ingest.schema import (
    Case, Locator, Action,
    PLATFORM_WEB, PLATFORM_APP, framework_to_platform,
)

DEFAULT_BASE_URL = "http://localhost:3000"


def _appium_index(i: int | None) -> str:
    """把 index 语义翻译成 Python 下标表达式（-1 天然表示最后一个）。"""
    return str(int(i))


def _slug(s: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", (s or "").strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "case"


# ====================== 后端抽象 ======================
class CodegenBackend:
    """把单个 Step 翻译成目标框架代码行的抽象基类。"""

    platform: str = PLATFORM_WEB        # 子类覆盖
    arg: str = "page"                   # 页面对象的变量名（page / driver）

    def loc_expr(self, loc: Locator) -> str:
        raise NotImplementedError

    def step_to_code(self, step, idx: int) -> str:
        raise NotImplementedError


class PlaywrightBackend(CodegenBackend):
    platform = PLATFORM_WEB
    arg = "page"

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        # 需要 base_url 才能把 LLM 给的相对路径（如 "/login"）补成可断言的完整 URL。
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def _url_expr(self, raw: str | None) -> str:
        """把 Step 里的 URL/path 转成 Python 表达式（字符串字面量或 f-string）。

        必须处理三种脏数据（LLM 常见输出）：
          1. 前导/尾随空格   " /login"  → 直接拼接会得到 "...8081 /login"，浏览器报 Invalid URL
          2. 相对路径        "/login"   → to_have_url 按完整 URL 匹配，相对路径永远不成立
          3. 已是完整 URL / glob  → 原样保留，不再拼 BASE_URL
        """
        v = (raw or "").strip()
        if not v:
            return 'f"{BASE_URL}/"'
        if v.startswith(("http://", "https://", "*")):
            return repr(v)
        if not v.startswith("/"):
            v = "/" + v
        return 'f"{BASE_URL}' + v + '"'

    @staticmethod
    def _nth_suffix(loc: Locator) -> str:
        """多元素选择后缀：Playwright strict mode 下同名多元素必须显式取序号。"""
        i = getattr(loc, "index", None)
        if i is None:
            return ""
        if i == 0:
            return ".first"
        if i == -1:
            return ".last"
        return f".nth({i})"

    def loc_expr(self, loc: Locator) -> str:
        v = repr(loc.value)
        by = loc.by.value
        suffix = self._nth_suffix(loc)
        if by == "css":
            return f"page.locator({v}){suffix}"
        if by == "xpath":
            return f"page.locator('xpath=' + {v}){suffix}"
        if by == "text":
            return f"page.get_by_text({v}){suffix}"
        if by == "role":
            return f"page.get_by_role({v}){suffix}"
        if by == "testid":
            return f"page.get_by_test_id({v}){suffix}"
        return f"page.locator({v}){suffix}"

    def step_to_code(self, step, idx: int) -> str:
        a = step.action.value
        note = f"  # {step.note}" if step.note else ""
        # --- 无需定位器的动作（必须先于「缺定位器跳过」判断）---
        if a == "navigate":
            return f'await {self.arg}.goto({self._url_expr(step.value)}){note}'
        if a == "wait":
            ms = (str(step.value or "500")).strip()
            return f"await {self.arg}.wait_for_timeout({ms}){note}"
        if a == "assert_url":
            return f'await expect({self.arg}).to_have_url({self._url_expr(step.expect)}){note}'
        # app-only 动作在 web 端降级提示
        if a in ("launch", "swipe", "long_press"):
            return f"# 步骤{idx}: 动作 {a} 在 web 端不适用，已跳过{note}"
        # --- 需要定位器的动作 ---
        if step.locator is None:
            return f"# 步骤{idx}: 动作 {a} 缺少定位器，已跳过{note}"
        loc = self.loc_expr(step.locator)
        if a == "click":
            return f"await {loc}.click(){note}"
        if a == "fill":
            return f"await {loc}.fill({(step.value or '')!r}){note}"
        if a == "hover":
            return f"await {loc}.hover(){note}"
        if a == "select":
            return f"await {loc}.select_option({(step.value or '')!r}){note}"
        if a == "scroll":
            return f"await {loc}.scroll_into_view_if_needed(){note}"
        if a == "assert_visible":
            return f"await expect({loc}).to_be_visible(){note}"
        if a == "assert_text":
            return f"await expect({loc}).to_contain_text({(step.expect or '')!r}){note}"
        if a == "assert_count":
            try:
                count = int(step.value or 0)
            except (TypeError, ValueError):
                count = 0
            return f"await expect({loc}).to_have_count({count}){note}"
        return f"# 步骤{idx}: 暂未实现动作 {a}{note}"


class AppiumBackend(CodegenBackend):
    platform = PLATFORM_APP
    arg = "driver"

    def __init__(self, app_package: str = "com.shopdemo.app"):
        self.app_package = app_package

    def loc_expr(self, loc: Locator) -> str:
        v = repr(loc.value)
        by = loc.by.value
        i = getattr(loc, "index", None)
        # 同名多元素：find_element 遇到多个匹配会报错，故统一走 find_elements + 下标
        many = i is not None
        # -1 天然表示最后一个：find_elements(...)[-1]
        idx_expr = _appium_index(i) if many else ""
        if by == "testid":
            # testid → accessibility id / content-desc（原生 app 首选）
            if not many:
                return f"driver.find_element(AppiumBy.ACCESSIBILITY_ID, {v})"
            return f"driver.find_elements(AppiumBy.ACCESSIBILITY_ID, {v})[{idx_expr}]"
        if by == "text":
            sel = f'new UiSelector().text("{loc.value}")'
            if not many:
                return f"driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, \'{sel}\')"
            return f"driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, '{sel}')[{idx_expr}]"
        if by == "css":
            appium_by = "AppiumBy.CSS_SELECTOR"
        elif by == "xpath":
            appium_by = "AppiumBy.XPATH"
        else:
            # role 在原生 app 无直接映射，降级为 xpath
            appium_by = "AppiumBy.XPATH"
        if not many:
            return f"driver.find_element({appium_by}, {v})"
        return f"driver.find_elements({appium_by}, {v})[{idx_expr}]"

    def step_to_code(self, step, idx: int) -> str:
        a = step.action.value
        note = f"  # {step.note}" if step.note else ""
        if a == "launch":
            return f"driver.activate_app({self.app_package!r}){note}"
        # web-only 动作在 app 端降级提示
        if a == "navigate":
            return f"# 步骤{idx}: 原生 app 无 URL 导航，请用 launch；已跳过{note}"
        if a == "hover":
            return f"# 步骤{idx}: 触屏无 hover，已跳过{note}"
        if a == "select":
            return f"# 步骤{idx}: 原生下拉用 picker，未实现 select；已跳过{note}"
        if a == "assert_url":
            return f"# 步骤{idx}: 原生 app 无 URL 断言，已跳过{note}"
        if a == "wait":
            ms = step.value or "500"
            try:
                secs = int(ms) / 1000.0
            except (TypeError, ValueError):
                secs = 0.5
            return f"time.sleep({secs:.3f}){note}"
        if step.locator is None:
            return f"# 步骤{idx}: 动作 {a} 缺少定位器，已跳过{note}"
        loc = self.loc_expr(step.locator)
        if a == "click":
            return f"{loc}.click(){note}  # 触屏 tap"
        if a == "fill":
            return f"{loc}.send_keys({(step.value or '')!r}){note}"
        if a == "long_press":
            return f"TouchAction({self.arg}).long_press({loc}).release().perform(){note}"
        if a == "swipe":
            return f"self._swipe({self.arg}){note}  # 占位：按方向/距离实现手势"
        if a == "scroll":
            return f"{loc}.scroll_into_view(){note}"
        if a == "assert_visible":
            return f"assert {loc}.is_displayed(){note}"
        if a == "assert_text":
            return f"assert {loc}.text == {(step.expect or '')!r}{note}"
        if a == "assert_count":
            try:
                count = int(step.value or 0)
            except (TypeError, ValueError):
                count = 0
            return f"assert len({loc}) == {count}{note}"
        return f"# 步骤{idx}: 暂未实现动作 {a}{note}"


# ====================== 后端分发 ======================
def get_backend(case: Case, *, base_url: str = DEFAULT_BASE_URL,
                app_package: str = "com.shopdemo.app") -> CodegenBackend:
    """按 Case.target_framework 选择后端（Appium 可注入 app_package）。"""
    if framework_to_platform(case.target_framework) == PLATFORM_APP:
        return AppiumBackend(app_package=app_package)
    # 每次新建实例：base_url 是实例状态，复用单例会被上一条用例污染
    return PlaywrightBackend(base_url=base_url)


# ====================== 代码装配 ======================
def _header_web(case: Case, func: str, base_url: str, mobile: bool) -> list[str]:
    h = [
        "import pytest",
        "from playwright.async_api import Page, expect",
        "",
        f'BASE_URL = {base_url!r}  # 由 config.framework.web.base_url 注入',
        "",
        f"# case_id: {case.id}",
        f"# title: {case.title}",
        f"# coverage_tags: {', '.join(case.coverage_tags) or '-'}",
        f"# framework: {case.target_framework} (web)",
        f"# mobile_h5: {mobile}",
        "",
        "@pytest.mark.asyncio",
        f"async def {func}(page: Page):",
    ]
    if mobile:
        h.append('    # 移动端 H5：用移动视口加载页面（如需完整设备模拟，改用 devices["iPhone 13"] 建 context）')
        h.append('    await page.set_viewport_size({"width": 390, "height": 844})')
    return h


def _header_app(case: Case, func: str, app_package: str) -> list[str]:
    return [
        "import time",
        "from appium.webdriver.common.appiumby import AppiumBy",
        "from appium.webdriver.common.touch_action import TouchAction",
        "",
        f'APP_PACKAGE = {app_package!r}  # 由 config.framework.app.app_package 注入',
        "",
        f"# case_id: {case.id}",
        f"# title: {case.title}",
        f"# coverage_tags: {', '.join(case.coverage_tags) or '-'}",
        f"# framework: {case.target_framework} (app)",
        "",
        "# driver 由 conftest 的 Appium fixture 注入（带 caps：platformName/deviceName/appPackage...）",
        f"def {func}(driver):",
    ]


def case_to_code(case: Case, *, base_url: str = DEFAULT_BASE_URL,
                 mobile: bool = False, app_package: str = "com.shopdemo.app") -> str:
    backend = get_backend(case, base_url=base_url, app_package=app_package)
    func = "test_" + _slug(case.id or case.title)

    if backend.platform == PLATFORM_APP:
        header = _header_app(case, func, app_package)
    else:
        header = _header_web(case, func, base_url, mobile)

    body: list[str] = []
    for pre in case.preconditions:
        body.append(f"# 前置条件: {pre}")
    for i, step in enumerate(case.steps, 1):
        body.append(backend.step_to_code(step, i))
    if not body:
        body.append("pass")
    indented = textwrap.indent("\n".join(body), "    ")
    return "\n".join(header) + "\n" + indented + "\n"


def batch_to_file(cases: list[Case], path: str, *, base_url: str = DEFAULT_BASE_URL,
                  mobile: bool = False, app_package: str = "com.shopdemo.app") -> None:
    blocks = [case_to_code(c, base_url=base_url, mobile=mobile, app_package=app_package)
              for c in cases]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(blocks))
