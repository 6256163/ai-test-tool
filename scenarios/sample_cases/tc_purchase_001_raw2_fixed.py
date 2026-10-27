import pytest
from playwright.async_api import Page, expect

BASE_URL = 'http://localhost:8081'  # 由 config.framework.web.base_url 注入

# case_id: TC_PURCHASE_001
# title: 登录后搜索产品旗舰版并加入购物车，校验购物车数量与单价一致
# coverage_tags: 主流程, 回归
# framework: playwright (web)
# mobile_h5: False

@pytest.mark.asyncio
async def test_tc_purchase_001(page: Page):
    # 前置条件: 被测系统已部署，登录页可访问
    # 前置条件: 存在可搜索的产品「旗舰版」
    # 前置条件: 购物车已清空（隔离历史数据，否则数量断言依赖上一次运行）
    await page.goto(f"{BASE_URL}/login")  # 打开登录页
    await page.get_by_test_id('input-username').fill('admin')  # 输入用户名 admin
    await page.get_by_test_id('input-password').fill('admin123')  # 输入密码 admin123
    await page.get_by_test_id('btn-login').click()  # 点击登录按钮
    await expect(page.get_by_test_id('current-user')).to_contain_text('admin')  # 校验登录成功，右上角显示 admin
    await page.goto(f"{BASE_URL}/cart")  # 进入购物车页，准备清空残留数据
    await page.get_by_test_id('btn-cart-clear').click()  # 清空购物车，隔离历史状态
    await page.goto(f"{BASE_URL}/products")  # 进入商品列表页
    await page.get_by_test_id('input-keyword').fill('旗舰版')  # 在搜索框输入产品关键字「旗舰版」
    await page.get_by_test_id('btn-search').click()  # 点击搜索按钮
    await expect(page.get_by_test_id('product-name').first).to_contain_text('旗舰版')  # 校验搜索结果第一条包含「旗舰版」（列表内多元素，必须指定 index）
    await page.get_by_test_id('product-name').first.click()  # 点击搜索结果第一个产品进入详情
    await expect(page.get_by_test_id('detail-name')).to_contain_text('旗舰版')  # 校验进入产品详情页且名称一致
    await expect(page.get_by_test_id('detail-price')).to_contain_text('22.4')  # 记录详情页单价，供购物车侧比对
    await page.get_by_test_id('btn-add-cart').click()  # 数量默认 1，点击加入购物车（成功后前端自动跳转 /cart）
    await expect(page.get_by_test_id('cart-table')).to_contain_text('旗舰版')  # 校验购物车已包含该产品
    await expect(page.get_by_test_id('cart-total')).to_contain_text('22.4')  # 校验购物车金额与详情页单价一致
    await expect(page.get_by_test_id('cart-count')).to_contain_text('1')  # 校验购物车中该产品数量为 1
