"""security/secrets.py —— 敏感数据「出域前占位 + 生成后回填」+ 凭据注册表。

为什么必须有「回填」这半步：
  原 `mask_sensitive` 只做 mask，LLM 看到 `${PHONE}` 会把它当输入值写进 case.value，
  生成的脚本变成 fill('${PHONE}') —— 脱了敏就等于用例必然失败。
  所以 mask 与 rehydrate 必须成对：真值留本机，出网的只有占位符。

三层能力：
  A. extract_secrets(text)        —— 从需求句抽出凭据 → 占位符 + 本地映射
  B. reconcile_with_registry(...) —— 若抽到的账号命中凭据注册表，真值以注册表为准
                                     （同时根治「手打密码写错」，如 admin123456 vs admin123）
  C. rehydrate_case(case, map)    —— codegen 之前把占位符换回真值

占位符（占位符本身不敏感，出网无害）：
  ${ACCOUNT} ${PASSWORD} ${TOKEN} ${JWT} ${API_KEY} ${BASIC_AUTH} ${PRIVATE_KEY}
  ${PHONE} ${EMAIL} ${ID}
  ${CRED.<profile>.<field>}       —— 注册表槽位，LLM 可直接引用
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ingest.schema import Case

# ---------- 占位符 ----------
P_ACCOUNT = "${ACCOUNT}"
P_PASSWORD = "${PASSWORD}"
P_TOKEN = "${TOKEN}"
P_JWT = "${JWT}"
P_API_KEY = "${API_KEY}"
P_BASIC = "${BASIC_AUTH}"
P_PEM = "${PRIVATE_KEY}"


# ---------- 脱敏规则 ----------
@dataclass
class SecretRule:
    name: str
    pattern: str
    value_group: int = 0          # 只有该分组是真值；0 表示整段命中
    placeholder: str | Callable[[re.Match], str] = P_PASSWORD
    group_placeholders: dict[int, str] = field(default_factory=dict)  # 多分组分别占位

    def compile(self) -> re.Pattern:
        return re.compile(self.pattern)


def _ph_by_label(m: re.Match) -> str:
    """按标签判断：密码/口令 → ${PASSWORD}，其余 → ${ACCOUNT}。"""
    label = m.group(1)
    return P_PASSWORD if any(k in label for k in ("密码", "口令", "pwd", "password")) else P_ACCOUNT


# 顺序敏感：先凭据复合形态，再通用单项（否则含手机号的密码会被拦腰截断半掩）
RULES: list[SecretRule] = [
    SecretRule("private_key",
               r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
               placeholder=P_PEM),
    SecretRule("basic_auth_url",
               r"(?i)(?:https?://)([^\s/:@]{1,64}):([^\s/@]{1,128})@",
               group_placeholders={1: P_ACCOUNT, 2: P_PASSWORD}),
    SecretRule("authorization_header",
               r"(?i)\bauthorization\b\s*[:=]\s*(?:bearer|basic)?\s*(\S+)",
               value_group=1, placeholder=P_TOKEN),
    SecretRule("jwt",
               r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b",
               placeholder=P_JWT),
    SecretRule("api_key",
               r"\b(?:sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{12,})\b",
               placeholder=P_API_KEY),
    SecretRule("kv_token",
               r"(?i)\b(token|access_token|sessionid|api_key)\s*=\s*([^\s&\"']+)",
               value_group=2, placeholder=P_TOKEN),
    SecretRule("kv_password",
               r"(?i)\b(password|pwd|secret|passwd)\s*=\s*([^\s&\"']+)",
               value_group=2, placeholder=P_PASSWORD),
    # 中文标签：账号：admin / 密码：admin123
    SecretRule("labeled_cred",
               r"(账号|帐号|用户名|用户|登录名|密码|口令|pwd|password|user|username)"
               r"\s*(?:是|为)?\s*[：:=]\s*([^\s,，。;；、]+)",
               value_group=2, placeholder=_ph_by_label),
    # 斜杠对：admin/admin123 —— 仅在存在登录上下文时启用，避免误伤普通 "a/b"
    SecretRule("slash_pair",
               r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]{2,30})/([A-Za-z0-9_.\-!@#$%^&*]{3,30})"
               r"(?![A-Za-z0-9_.\-])",
               group_placeholders={1: P_ACCOUNT, 2: P_PASSWORD}),
    # 通用单项放最后
    SecretRule("phone", r"(?<!\d)1[3-9]\d{9}(?!\d)", placeholder="${PHONE}"),
    SecretRule("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", placeholder="${EMAIL}"),
    SecretRule("idcard", r"\b\d{17}[\dXx]\b", placeholder="${ID}"),
]

# slash_pair / kv 这类规则需要上下文佐证，否则 "跳转到 /products" 也会被当成凭据
_CRED_CONTEXT = ("登录", "登陆", "log in", "login", "sign in", "signin", "账号", "帐户",
                 "用户名", "密码", "口令", "凭证", "凭据", "auth")
_CTX_GATED = {"slash_pair"}


def _key_of(placeholder: str) -> str:
    """`${PASSWORD}` -> `PASSWORD`"""
    return placeholder[2:-1]


def extract_secrets(text: str) -> tuple[str, dict[str, str]]:
    """抽出文本里的敏感数据，返回 (占位符文本, {占位符名: 真值})。

    真值只留在本机 mapping 里，随同使用的是无任何敏感信息的占位符。
    """
    mapping: dict[str, str] = {}
    out = text or ""
    lowered = out.lower()
    for rule in RULES:
        if rule.name in _CTX_GATED and not any(c in lowered for c in _CRED_CONTEXT):
            continue
        rx = rule.compile()

        def _sub(m: re.Match, _rule=rule) -> str:
            """把命中里的敏感分组换成占位符，非敏感部分（如 `账号：`、key=）原样保留。"""
            # 整段命中
            if not _rule.group_placeholders and not _rule.value_group:
                ph = _rule.placeholder(m) if callable(_rule.placeholder) else _rule.placeholder
                mapping.setdefault(_key_of(ph), m.group(0))
                return ph

            ph_map: dict[int, str]
            if _rule.group_placeholders:
                ph_map = dict(_rule.group_placeholders)
            else:
                ph = _rule.placeholder(m) if callable(_rule.placeholder) else _rule.placeholder
                ph_map = {_rule.value_group: ph}

            pieces: list[str] = []
            cursor = 0
            for gi in sorted(ph_map):
                start, end = m.span(gi)
                ph = ph_map[gi]
                mapping.setdefault(_key_of(ph), m.group(gi))
                pieces.append(m.group(0)[cursor:start - m.start()])
                pieces.append(ph)
                cursor = end - m.start()
            pieces.append(m.group(0)[cursor:])
            return "".join(pieces)

        out = rx.sub(_sub, out)
    return out, mapping


# ---------- B 层：凭据注册表 ----------
def expand_env(value: str) -> tuple[str, bool]:
    """`${VAR}` → 环境变量值；返回 (值, 是否成功解析)。"""
    v = (value or "").strip()
    if v.startswith("${") and v.endswith("}"):
        env_name = v[2:-1]
        got = os.environ.get(env_name, "")
        return got, bool(got)
    return v, True


def load_profiles(cfg: dict) -> tuple[dict[str, dict[str, str]], list[str]]:
    """读 config.credentials.profiles，展开环境变量。

    返回 (profiles, warnings)；环境变量缺失的字段会被跳过（不给空值占位符）。
    """
    sec = cfg.get("credentials") or {}
    if not sec.get("enabled"):
        return {}, []
    profiles: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    for name, fields in (sec.get("profiles") or {}).items():
        resolved: dict[str, str] = {}
        for field_name, raw in (fields or {}).items():
            val, ok = expand_env(str(raw))
            if not ok:
                warnings.append(f"凭据 {name}.{field_name} 的环境变量未设置，已跳过该字段")
                continue
            resolved[str(field_name)] = val
        if resolved:
            profiles[str(name)] = resolved
    return profiles, warnings


def build_cred_mapping(profiles: dict[str, dict[str, str]]) -> dict[str, str]:
    """注册表 → `${CRED.admin.password}` 这类槽位映射。"""
    mapping: dict[str, str] = {}
    for name, fields in profiles.items():
        for field_name, val in fields.items():
            mapping[f"CRED.{name}.{field_name}"] = val
    return mapping


def cred_slots_text(profiles: dict[str, dict[str, str]]) -> str | None:
    """生成注入 LLM 的「可用凭据槽位」说明（不含任何真值）。"""
    if not profiles:
        return None
    slots = [f"${{CRED.{n}.{f}}}" for n, fields in profiles.items() for f in fields]
    return (
        "【可用测试凭据槽位】以下槽位可直接用作 value 或 expect，运行时会自动替换为真实凭据：\n"
        + "、".join(slots)
        + "\n规则：不要在用例里手写真实账号密码；需要凭据时直接引用上面的槽位。"
    )


def reconcile_with_registry(mapping: dict[str, str],
                            profiles: dict[str, dict[str, str]]) -> tuple[dict[str, str], list[str]]:
    """句中抽到的账号若命中注册表条目，真值以注册表为准。

    这是根治「密码手打错」的地方：句子写 admin123456、注册表写 admin123，
    则以注册表为准，并给出明确告警，避免用例因陈旧密码而必然失败。
    """
    warnings: list[str] = []
    out = dict(mapping)
    account = out.get(_key_of(P_ACCOUNT))
    if account and profiles and account in profiles:
        prof = profiles[account]
        for variant_key in ("username", "user", "account"):
            if variant_key in prof and prof[variant_key] != account:
                warnings.append(f"账号以注册表为准：{account} → {prof[variant_key]}")
                out[_key_of(P_ACCOUNT)] = prof[variant_key]
                break
        given = out.get(_key_of(P_PASSWORD))
        truth = prof.get("password") or prof.get("pwd")
        if truth and given and truth != given:
            warnings.append(
                f"句中密码与注册表 `{account}` 不一致，已按注册表回填（{given} → {truth}）"
            )
        if truth:
            out[_key_of(P_PASSWORD)] = truth
    return out, warnings


# ---------- C 层：回填 ----------
def restore_text(text: str, mapping: dict[str, str]) -> str:
    """把字符串里的 ${KEY} 换回真值；未知占位符原样保留（不误删）。"""
    if not text or not mapping or "${" not in text:
        return text
    out = text
    for key, secret in mapping.items():
        if not secret:
            continue
        out = out.replace("${" + key + "}", secret)
    return out


def _walk(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return restore_text(obj, mapping)
    if isinstance(obj, list):
        return [_walk(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, mapping) for k, v in obj.items()}
    return obj


def rehydrate_case(case: Case, mapping: dict[str, str]) -> Case:
    """把占位符换回真值后重新校验成 Case。必须在 codegen 之前调用。

    覆盖字段：value / expect / title / preconditions / base_path / locator.value，
    即所有可能出现占位符的文本位（含断言期望，如 expect: ${ACCOUNT}）。
    """
    if not mapping:
        return case
    data = _walk(case.model_dump(), mapping)
    try:
        return Case.model_validate(data)
    except Exception:  # 回填结果万一不合 schema，宁可用原 case 也不要整条流水线崩
        return case


# ---------- 向后兼容 ----------
def mask_sensitive(text: str) -> str:
    """兼容旧接口的单向脱敏（返回不含真值的文本）。"""
    return extract_secrets(text)[0]
