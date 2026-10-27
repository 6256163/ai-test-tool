"""server/db.py —— 阶段0 平台存储层（SQLite，零额外依赖）。

落盘位置：项目根 data/platform.db。
只存「用例元数据 + 运行结果摘要 + 代码 + 输出」，不主动二次脱敏——
引擎侧 security 已保证上云出网只有占位符；落库为本地文件，属阶段0 已知取舍
（详见 README「阶段0 平台服务化」安全说明）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT, "data")
_DB_PATH = os.path.join(_DATA_DIR, "platform.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id          TEXT PRIMARY KEY,
                source      TEXT NOT NULL,         -- 'generate' | 'run-case'
                requirement TEXT,                  -- generate 时的原始需求
                title       TEXT,
                case_json   TEXT NOT NULL,         -- 完整 case 序列化（JSON）
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id          TEXT PRIMARY KEY,
                case_id     TEXT NOT NULL,
                source      TEXT NOT NULL,         -- 'generate' | 'run-case'
                code        TEXT,
                output      TEXT,
                exit_code   INTEGER,
                passed      INTEGER,               -- pytest 通过数
                failed      INTEGER,               -- pytest 失败数
                gate_passed INTEGER,               -- 质量门 0/1
                reasons     TEXT,                  -- JSON list
                locator_warnings TEXT,             -- JSON list
                heal_summary TEXT,                 -- JSON or NULL
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_case ON runs(case_id);
            """
        )
        _migrate_runs(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_runs(conn: sqlite3.Connection) -> None:
    """阶段1：给 runs 表补状态/参数列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    adds = []
    if "status" not in cols:
        adds.append(("status", "TEXT NOT NULL DEFAULT 'success'"))  # queued/running/success/failed
    if "params" not in cols:
        adds.append(("params", "TEXT"))          # JSON: {base_url, heal, repeat}
    if "started_at" not in cols:
        adds.append(("started_at", "TEXT"))
    if "finished_at" not in cols:
        adds.append(("finished_at", "TEXT"))
    if "meta" not in cols:
        adds.append(("meta", "TEXT"))            # JSON: 额外信息（如 repeat 稳定性）
    for name, definition in adds:
        conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")


def save_case(*, source: str, requirement: str | None, title: str | None, case_json: str) -> str:
    cid = uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO cases (id, source, requirement, title, case_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, source, requirement, title, case_json, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return cid


def save_run(*, case_id: str, source: str, code: str | None, output: str,
             exit_code: int, passed: int, failed: int, gate_passed: bool,
             reasons: list, locator_warnings: list, heal_summary: dict | None,
             status: str = "success") -> str:
    rid = uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO runs (id, case_id, source, code, output, exit_code, passed, failed, "
            "gate_passed, reasons, locator_warnings, heal_summary, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, case_id, source, code, output, exit_code, passed, failed,
             int(bool(gate_passed)), json.dumps(reasons, ensure_ascii=False),
             json.dumps(locator_warnings, ensure_ascii=False),
             json.dumps(heal_summary, ensure_ascii=False) if heal_summary else None,
             status, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return rid


def create_run_pending(case_id: str, *, source: str, params: str) -> str:
    """阶段1：插入一条「排队中」的运行记录，等待 worker 取走执行。"""
    rid = uuid.uuid4().hex
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO runs (id, case_id, source, params, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (rid, case_id, source, params, "queued", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return rid


# 允许 update_run 写入的列白名单（防止调用方误写任意字段）
_ALLOWED_RUN_FIELDS = {
    "status", "code", "output", "exit_code", "passed", "failed", "gate_passed",
    "reasons", "locator_warnings", "heal_summary", "params", "meta",
    "started_at", "finished_at",
}


def update_run(run_id: str, **fields) -> None:
    """通用更新 runs 一行；JSON 字段（reasons/locator_warnings/heal_summary/meta/params）
    若传入非字符串则自动序列化。"""
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _ALLOWED_RUN_FIELDS:
            continue
        if k in ("reasons", "locator_warnings", "heal_summary", "meta", "params"):
            v = json.dumps(v, ensure_ascii=False) if v is not None and not isinstance(v, str) else v
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(run_id)
    conn = _conn()
    try:
        conn.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    finally:
        conn.close()


def list_cases(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT c.id, c.source, c.requirement, c.title, c.created_at, "
            "  (SELECT status FROM runs WHERE case_id=c.id ORDER BY created_at DESC LIMIT 1) AS last_status, "
            "  (SELECT gate_passed FROM runs WHERE case_id=c.id ORDER BY created_at DESC LIMIT 1) AS last_gate "
            "FROM cases c ORDER BY c.created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_case(case_id: str) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["case_json"] = json.loads(d["case_json"])
        d["runs"] = list_runs(case_id, limit=100)
        return d
    finally:
        conn.close()


def list_runs(case_id: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _conn()
    try:
        if case_id:
            rows = conn.execute(
                "SELECT id, case_id, source, status, exit_code, passed, failed, gate_passed, created_at "
                "FROM runs WHERE case_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (case_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, case_id, source, status, exit_code, passed, failed, gate_passed, created_at "
                "FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_runs_by_status(statuses: tuple[str, ...]) -> list[dict]:
    """阶段1：取出处于指定状态的运行（用于 worker 启动恢复卡住任务）。"""
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT id FROM runs WHERE status IN ({placeholders})", statuses
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_case(case_id: str, *, title: str | None = None, case_json: str | None = None) -> bool:
    """阶段1：更新用例的标题和/或 JSON。"""
    sets, vals = [], []
    if title is not None:
        sets.append("title=?")
        vals.append(title)
    if case_json is not None:
        sets.append("case_json=?")
        vals.append(case_json)
    if not sets:
        return False
    vals.append(case_id)
    conn = _conn()
    try:
        cur = conn.execute(f"UPDATE cases SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_case(case_id: str) -> bool:
    """阶段1：删除用例及其全部运行记录。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM runs WHERE case_id=?", (case_id,))
        cur = conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else []
        d["locator_warnings"] = json.loads(d["locator_warnings"]) if d["locator_warnings"] else []
        d["heal_summary"] = json.loads(d["heal_summary"]) if d["heal_summary"] else None
        return d
    finally:
        conn.close()


init_db()  # 模块导入即确保表存在
