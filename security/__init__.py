"""security —— 敏感数据处理（出域前占位 / 生成后回填 / 凭据注册表）。"""
from security.secrets import (
    extract_secrets,
    rehydrate_case,
    restore_text,
    load_profiles,
    build_cred_mapping,
    cred_slots_text,
    reconcile_with_registry,
)

__all__ = [
    "extract_secrets",
    "rehydrate_case",
    "restore_text",
    "load_profiles",
    "build_cred_mapping",
    "cred_slots_text",
    "reconcile_with_registry",
]
