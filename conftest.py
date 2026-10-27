"""conftest.py —— 提供生成用例所依赖的 `page` fixture。

背景：`generator/codegen.py` 生成的用例签名是 `async def test_x(page: Page)`，
但项目原先既没装 `pytest-playwright`、也没有 conftest，导致任何 web 用例一启动就
`fixture 'page' not found`——这跟用例本身的质量无关，是基建缺口。

约定：
- 若将来装了 `pytest-playwright`（它自己会注册 page fixture），本文件自动让位，避免重名冲突。
- `runner/executor.py` 会把本文件复制到 pytest 的临时工作目录，保证在 /tmp 下跑的生成用例
  也能拿到 fixture。
"""
from __future__ import annotations

import pytest

try:  # 装了 pytest-playwright 就用它的 page fixture，避免重复定义
    import pytest_playwright  # noqa: F401

    _HAS_PW_PLUGIN = True
except ModuleNotFoundError:
    _HAS_PW_PLUGIN = False


if not _HAS_PW_PLUGIN:
    import pytest_asyncio
    from playwright.async_api import async_playwright

    @pytest_asyncio.fixture
    async def page():
        """每条用例一个独立浏览器上下文；用例结束自动关闭。"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            pg = await context.new_page()
            try:
                yield pg
            finally:
                await browser.close()
