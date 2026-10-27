"""server/jobs.py —— 阶段1 异步任务队列（后台 pytest worker + 状态轮询）。

设计要点：
  - 单 daemon 线程消费内存队列，逐个取 run_id → 调 engine.run_case → 写回 DB。
    这样「后台跑 pytest」不会阻塞 FastAPI 事件循环；前端用 GET /api/runs/{id} 轮询。
  - 运行是串行的（一个 worker 一次跑一个），避免并发 Playwright 抢 profile/端口。
    这对 MVP 足够；阶段3 再扩成多 worker 网格。
  - 进程重启后，DB 里 stuck 在 queued/running 的任务会被 _recover 重新入队，
    保证「断点续跑」，不会丢任务。
  - 全程不碰断言：self-heal 走 engine 的 heal 开关（护栏在 healer 内部）。
"""
from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone

import server.db as db
import server.engine as engine

_q: "queue.Queue[dict]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker() -> None:
    while True:
        item = _q.get()
        try:
            _process(item)
        except Exception as e:  # 兜底：任何未捕获异常都标记失败，别让 worker 死掉
            try:
                db.update_run(item["run_id"], status="failed",
                              output=f"worker 未捕获异常：{e}", finished_at=_now())
            except Exception:
                pass
        finally:
            _q.task_done()


def _process(item: dict) -> None:
    rid = item["run_id"]
    db.update_run(rid, status="running", started_at=_now())
    try:
        run = db.get_run(rid)
        case_row = db.get_case(run["case_id"])
        params = json.loads(run["params"] or "{}")
        case = engine.build_case(case_row["case_json"])
        res = engine.run_case(
            case,
            base_url=params.get("base_url"),
            heal=bool(params.get("heal", False)),
            repeat=int(params.get("repeat", 1) or 1),
        )
        r = res["run"]
        db.update_run(
            rid,
            status="success" if r.ok else "failed",
            code=res["code"], output=r.output,
            exit_code=r.exit_code, passed=r.passed, failed=r.failed,
            gate_passed=res["gate_passed"], reasons=res["reasons"],
            locator_warnings=res["locator_warnings"], heal_summary=res["heal"],
            meta=res.get("meta"), finished_at=_now(),
        )
    except Exception as e:
        db.update_run(rid, status="failed",
                      output=f"执行异常：{e}", finished_at=_now())


def ensure_worker() -> None:
    """确保 worker 线程存活；首次启动时恢复上一进程卡住的任务。"""
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, daemon=True)
            _thread.start()
            for r in db.list_runs_by_status(("queued", "running")):
                _q.put({"run_id": r["id"]})


def enqueue_run(case_id: str, *, source: str = "run",
                base_url: str | None = None, heal: bool = False,
                repeat: int = 1) -> str:
    """入队一条运行任务，返回 run_id（状态初始为 queued）。"""
    params = json.dumps(
        {"base_url": base_url, "heal": bool(heal), "repeat": int(repeat or 1)},
        ensure_ascii=False,
    )
    rid = db.create_run_pending(case_id, source=source, params=params)
    ensure_worker()
    _q.put({"run_id": rid})
    return rid
