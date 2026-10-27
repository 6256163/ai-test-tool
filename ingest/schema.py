"""Pydantic schema —— 生成层的输出契约。

这一层是「能不能稳定落成代码」的关键：LLM 必须按这个 schema 产出，
codegen 才能无歧义地翻译成 Playwright 测试代码。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class LocatorStrategy(str, Enum):
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"
    ROLE = "role"
    TESTID = "testid"


class Locator(BaseModel):
    by: LocatorStrategy
    value: str
    # 同一页面上可能有多个同名元素（如商品列表里每张卡片都有 product-name）。
    # Playwright 默认 strict mode：多元素定位会直接抛错，必须显式指定序号。
    # 语义：0 = 第 1 个；正整数 = 第 n+1 个；-1 = 最后一个；None = 期望唯一（不给号）。
    index: int | None = Field(
        default=None,
        description="同名元素有多个时选取第几个，从 0 开始；-1 表示最后一个。仅当元素在列表/表格中重复出现时才填。",
    )


class Action(str, Enum):
    # 通用（web + app 都能用）
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    WAIT = "wait"
    ASSERT_TEXT = "assert_text"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_URL = "assert_url"
    ASSERT_COUNT = "assert_count"
    SCROLL = "scroll"                 # 两端都有（web 用滚轮/滚动，app 用滚动到元素）
    # 移动端新增（原生 App）
    SWIPE = "swipe"                   # 手势滑动
    LONG_PRESS = "long_press"         # 长按
    LAUNCH = "launch"                 # 启动/切回 app（替代 web 的 navigate）
    # web-only（触屏场景不可用）
    HOVER = "hover"                   # 触屏无悬停
    SELECT = "select"                 # 原生下拉用 picker，不映射 SELECT


# ====== 平台标识与动作适用性 ======
PLATFORM_WEB = "web"
PLATFORM_APP = "app"

# 每个动作适用的平台集合。codegen 据此在生成代码里给出降级/跳过提示，
# judge 据此在 app 用例里拦截 web-only 动作（hover/select）。
ACTION_PLATFORMS: dict[str, set[str]] = {
    "navigate": {PLATFORM_WEB},
    "hover": {PLATFORM_WEB},
    "select": {PLATFORM_WEB},
    "launch": {PLATFORM_APP},
    "swipe": {PLATFORM_APP},
    "long_press": {PLATFORM_APP},
    "scroll": {PLATFORM_WEB, PLATFORM_APP},
    "click": {PLATFORM_WEB, PLATFORM_APP},
    "fill": {PLATFORM_WEB, PLATFORM_APP},
    "wait": {PLATFORM_WEB, PLATFORM_APP},
    "assert_text": {PLATFORM_WEB, PLATFORM_APP},
    "assert_visible": {PLATFORM_WEB, PLATFORM_APP},
    "assert_url": {PLATFORM_WEB},           # 原生 app 无 URL 概念
    "assert_count": {PLATFORM_WEB, PLATFORM_APP},
}


def framework_to_platform(target_framework: str | None) -> str:
    """把 target_framework 字符串映射到平台（web / app）。

    playwright → web；appium / espresso / xcuitest / app → app。
    """
    tf = (target_framework or "playwright").lower()
    if tf in {"appium", "espresso", "xcuitest", "app"}:
        return PLATFORM_APP
    return PLATFORM_WEB


class Step(BaseModel):
    action: Action
    locator: Locator | None = None
    value: str | None = None
    expect: str | None = None
    note: str | None = None


class Case(BaseModel):
    id: str = Field(..., description="用例唯一标识，如 TC_LOGIN_001")
    title: str
    preconditions: list[str] = Field(default_factory=list)
    steps: list[Step]
    coverage_tags: list[str] = Field(default_factory=list, description="从[冒烟,主流程,边界,异常,回归]中选")
    target_framework: str = "playwright"
    base_path: str | None = Field(default=None, description="首屏 URL path，如 /login")
