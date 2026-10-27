"""tests/test_secrets.py —— 脱敏/回填/凭据注册表的回归测试。

跑：python -m pytest tests/test_secrets.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ingest.schema import Case  # noqa: E402
from security.secrets import (  # noqa: E402
    build_cred_mapping, cred_slots_text, extract_secrets, load_profiles,
    rehydrate_case, reconcile_with_registry, restore_text,
)


@pytest.mark.parametrize("text,ph", [
    ("使用admin/admin123456测试登陆成功流程", "${ACCOUNT}/${PASSWORD}"),
    ("账号：admin，密码：admin123456 登录", "${ACCOUNT}，密码：${PASSWORD}"),
    ("拼参数 ?token=abc123def&user=tom", "token=${TOKEN}"),
    ("手机号 13800000000 接收验证码", "${PHONE}"),
])
def test_extract(text, ph):
    masked, mapping = extract_secrets(text)
    if ph is None:
        assert not mapping
    else:
        assert ph in masked
        assert mapping


def test_extract_leaks_nothing():
    """出网文本里不能残留真值。"""
    _, mapping = extract_secrets("使用admin/admin123456测试登陆成功流程")
    masked, mapping2 = extract_secrets("使用admin/admin123456测试登陆成功流程")
    for secret in mapping2.values():
        assert secret not in masked


def test_no_false_positive_on_paths():
    """斜杠对只在登录上下文启用，普通路径/route 不能被误伤。"""
    for text in ["打开 /products 页面搜索商品", "跳转到 /products/:id 看详情", "分页 1/17"]:
        masked, mapping = extract_secrets(text)
        assert not mapping, f"误伤了：{text} -> {masked}"


def test_restore_and_rehydrate():
    mapping = {"ACCOUNT": "admin", "PASSWORD": "admin123"}
    assert restore_text("${ACCOUNT}/${PASSWORD}", mapping) == "admin/admin123"

    case = Case.model_validate({
        "id": "TC1", "title": "登录 ${ACCOUNT}",
        "steps": [
            {"action": "fill", "locator": {"by": "testid", "value": "input-username"},
             "value": "${ACCOUNT}"},
            {"action": "assert_text", "locator": {"by": "testid", "value": "current-user"},
             "expect": "${ACCOUNT}"},
        ],
        "coverage_tags": ["冒烟"], "base_path": "/login",
    })
    restored = rehydrate_case(case, mapping)
    assert restored.steps[0].value == "admin"
    assert restored.steps[1].expect == "admin"   # expect 里的占位符同样要回填
    assert restored.title == "登录 admin"


def test_registry_overrides_wrong_password(monkeypatch):
    monkeypatch.setenv("TARGET_DEMO_ADMIN_PWD", "admin123")
    cfg = {"credentials": {"enabled": True, "profiles": {
        "admin": {"username": "admin", "password": "${TARGET_DEMO_ADMIN_PWD}"},
    }}}
    profiles, warnings = load_profiles(cfg)
    assert profiles["admin"]["password"] == "admin123"
    assert not warnings

    _masked, mapping = extract_secrets("使用admin/admin123456测试登陆成功流程")
    final, overrides = reconcile_with_registry(mapping, profiles)
    assert final["PASSWORD"] == "admin123"       # 错打的 admin123456 被注册表纠正
    assert overrides

    slots = build_cred_mapping(profiles)
    assert slots["CRED.admin.password"] == "admin123"
    hint = cred_slots_text(profiles) or ""
    assert "${CRED.admin.password}" in hint and "admin123" not in hint   # 提示词里只有槽位名


def test_missing_env_is_skipped():
    cfg = {"credentials": {"enabled": True, "profiles": {
        "admin": {"username": "admin", "password": "${DEFINITELY_UNSET_VAR_XYZ}"},
    }}}
    profiles, warnings = load_profiles(cfg)
    assert profiles["admin"].get("password") is None
    assert warnings
