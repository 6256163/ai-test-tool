"""server/app.py —— 阶段1 平台壳：FastAPI 服务 + SQLite 结果库 + 平台壳 UI。

启动（在 aitest venv 内）：
  python -m uvicorn server.app:app \
      --host 0.0.0.0 --port 8000

相对阶段0 的变化：
  - 执行核心抽到 server.engine（同步接口 / 异步 worker 共用）。
  - 新增 server.jobs：后台 worker 线程异步跑 pytest，前端轮询状态。
  - 新增用例 CRUD：POST /api/cases、PUT /api/cases/{id}、DELETE /api/cases/{id}。
  - 新增异步运行入口：POST /api/runs（入队）→ GET /api/runs/{id}（轮询）。
  - UI 升级为「列表 + 新建/编辑/删除 + 一键异步运行并实时看结果」的平台壳。
"""
from __future__ import annotations

import json
import os
import sys

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main  # noqa: E402  (run_pipeline 仍需它做「需求→用例」)
import server.db as db  # noqa: E402
import server.engine as engine  # noqa: E402
import server.jobs as jobs  # noqa: E402

app = FastAPI(title="AI-Test-Tool Platform", version="0.2.0")


def _public(res: dict, run_id: str) -> dict:
    run = res["run"]
    return {
        "run_id": run_id,
        "code": res["code"],
        "run": {"exit_code": run.exit_code, "passed": run.passed,
                "failed": run.failed, "ok": run.ok, "output": run.output[-4000:]},
        "gate_passed": res["gate_passed"],
        "reasons": res["reasons"],
        "locator_warnings": res["locator_warnings"],
        "heal": res["heal"],
        "meta": res.get("meta"),
        "secret_slots": res["secret_slots"],
    }


# ====================== 同步入口（阶段0 保留，便于 CLI/测试） ======================
@app.post("/api/generate")
async def api_generate(body: dict):
    """从一句话需求生成用例并跑质量门（需要服务端配置 LLM_API_KEY）。"""
    requirement = (body.get("requirement") or "").strip()
    if not requirement:
        raise HTTPException(400, "requirement 不能为空")
    cfg = engine.get_cfg()
    if body.get("platform"):
        cfg.setdefault("framework", {})["type"] = body["platform"]
    if not cfg["llm"]["api_key"]:
        # 缺 Key 不止报错：把「本可发给 LLM 的完整提示词」一起返回，便于本地调试/排查。
        preview = engine.build_prompt_preview(
            requirement, platform=body.get("platform"), use_hints=True,
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": "服务端未配置 LLM_API_KEY，无法从需求生成。可改用 POST /api/cases "
                         "直接提交用例 JSON，或直接 POST /api/run-case、POST /api/runs。",
                "prompt_preview": preview,
                "note": "下面是本可发给 LLM 的完整提示词（含脱敏占位符与 DOM 目录 hints），"
                        "可据此本地调试或排查。配置 LLM_API_KEY 后即可真正生成。",
            },
        )
    heal = bool(body.get("heal", False))
    res = await run_in_threadpool(main.run_pipeline, requirement, cfg, heal=heal)
    case = res["case"]
    cid = db.save_case(source="generate", requirement=requirement, title=case.title,
                       case_json=case.model_dump_json())
    rid = db.save_run(
        case_id=cid, source="generate", code=res["code"], output=res["run"].output,
        exit_code=res["run"].exit_code, passed=res["run"].passed, failed=res["run"].failed,
        gate_passed=res["gate_passed"], reasons=res["reasons"],
        locator_warnings=res["locator_warnings"], heal_summary=res["heal"], status="success",
    )
    out = _public(res, rid)
    out["case_id"] = cid
    return out


@app.post("/api/run-case")
async def api_run_case(body: dict):
    """绕过 LLM，直接提交已有用例 JSON，同步跑 codegen + 执行 + 质量门（阶段0 主入口）。"""
    raw = body.get("case_json")
    if not raw:
        raise HTTPException(400, "case_json 不能为空")
    try:
        case = engine.build_case(raw)
    except Exception as e:
        raise HTTPException(400, f"case_json 校验失败：{e}")
    base_url = (body.get("base_url") or "").strip() or None
    heal = bool(body.get("heal", False))
    repeat = int(body.get("repeat", 1) or 1)
    res = await run_in_threadpool(engine.run_case, case, base_url=base_url,
                                  heal=heal, repeat=repeat)
    cid = db.save_case(source="run-case", requirement=None, title=res["case"].title,
                       case_json=res["case"].model_dump_json())
    rid = db.save_run(
        case_id=cid, source="run-case", code=res["code"], output=res["run"].output,
        exit_code=res["run"].exit_code, passed=res["run"].passed, failed=res["run"].failed,
        gate_passed=res["gate_passed"], reasons=res["reasons"],
        locator_warnings=res["locator_warnings"], heal_summary=res["heal"], status="success",
    )
    out = _public(res, rid)
    out["case_id"] = cid
    return out


# ====================== 用例 CRUD（阶段1） ======================
@app.post("/api/cases")
async def api_create_case(body: dict):
    """新建用例（手动录入或直接导入 LLM 产物）。body: {case_json, title?}"""
    raw = body.get("case_json")
    if not raw:
        raise HTTPException(400, "case_json 不能为空")
    try:
        case = engine.build_case(raw)
    except Exception as e:
        raise HTTPException(400, f"case_json 校验失败：{e}")
    title = (body.get("title") or case.title or None)
    cid = db.save_case(source="manual", requirement=None, title=title,
                       case_json=case.model_dump_json())
    return {"case_id": cid, "title": title, "source": "manual"}


@app.put("/api/cases/{case_id}")
async def api_update_case(case_id: str, body: dict):
    title = body.get("title")
    raw = body.get("case_json")
    if raw is not None:
        try:
            engine.build_case(raw)
        except Exception as e:
            raise HTTPException(400, f"case_json 校验失败：{e}")
        case_json = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else raw
    else:
        case_json = None
    if title is None and case_json is None:
        raise HTTPException(400, "至少需要 title 或 case_json 其一")
    ok = db.update_case(case_id, title=title, case_json=case_json)
    if not ok:
        raise HTTPException(404, "case 不存在")
    return {"ok": True, "case_id": case_id}


@app.delete("/api/cases/{case_id}")
async def api_delete_case(case_id: str):
    ok = db.delete_case(case_id)
    if not ok:
        raise HTTPException(404, "case 不存在")
    return {"ok": True, "case_id": case_id}


# ====================== 异步运行（阶段1 核心） ======================
@app.post("/api/runs")
async def api_enqueue_run(body: dict):
    """异步提交一次运行：入队后由后台 worker 执行，前端轮询 GET /api/runs/{id}。

    body: {case_id? , case_json? , base_url? , heal? , repeat?}
      - 给 case_json 会先落库成一条 manual 用例再用它跑；否则必须给已存在的 case_id。
    """
    case_id = body.get("case_id")
    raw = body.get("case_json")
    if raw is not None:
        try:
            case = engine.build_case(raw)
        except Exception as e:
            raise HTTPException(400, f"case_json 校验失败：{e}")
        case_id = db.save_case(source="manual", requirement=None,
                               title=case.title, case_json=case.model_dump_json())
    if not case_id:
        raise HTTPException(400, "必须提供 case_id 或 case_json 之一")
    if db.get_case(case_id) is None:
        raise HTTPException(404, "case_id 不存在")
    base_url = (body.get("base_url") or "").strip() or None
    heal = bool(body.get("heal", False))
    repeat = int(body.get("repeat", 1) or 1)
    rid = jobs.enqueue_run(case_id, source="run", base_url=base_url, heal=heal, repeat=repeat)
    return {"run_id": rid, "case_id": case_id, "status": "queued"}


@app.post("/api/reload-catalog")
async def api_reload_catalog():
    """前端源码改了之后，重新扫描 data-testid 目录。"""
    # engine 用的是模块级缓存，直接重置让它下次重新扫描
    engine._catalog = None
    engine._catalog_loaded = False
    cat = engine.get_catalog()
    n = len(cat.all_testids) if cat else 0
    return {"ok": True, "testids": n}


@app.get("/api/cases")
async def api_list_cases(limit: int = 200, offset: int = 0):
    return db.list_cases(limit=limit, offset=offset)


@app.get("/api/cases/{case_id}")
async def api_get_case(case_id: str):
    c = db.get_case(case_id)
    if not c:
        raise HTTPException(404, "case 不存在")
    return c


@app.get("/api/runs")
async def api_list_runs(case_id: str | None = None, limit: int = 50, offset: int = 0):
    return db.list_runs(case_id=case_id, limit=limit, offset=offset)


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "run 不存在")
    return r


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_PAGE)


# 进程启动即拉起 worker（并恢复上一进程卡住的任务）
jobs.ensure_worker()


_PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-Test-Tool Platform</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2329;--muted:#6b7280;--border:#e5e7eb;--ok:#16a34a;--bad:#dc2626;--warn:#d97706;--accent:#2563eb;--code-bg:#0d1117;--code-fg:#e6edf3;--pill:#eef2ff;}
@media (prefers-color-scheme: dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#8b949e;--border:#30363d;--ok:#3fb950;--bad:#f85149;--warn:#e3b341;--accent:#58a6ff;--code-bg:#010409;--code-fg:#e6edf3;--pill:#1f2a44;}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 24px;border-bottom:1px solid var(--border);background:var(--card);position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
header h1{font-size:16px;margin:0}
header .sub{color:var(--muted);font-size:12px}
main{padding:20px;max-width:1100px;margin:0 auto}
.toolbar{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
button{background:var(--accent);color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:13px}
button.sec{background:transparent;color:var(--accent);border:1px solid var(--accent)}
button.danger{background:transparent;color:var(--bad);border:1px solid var(--bad)}
button:disabled{opacity:.5;cursor:not-allowed}
input,textarea{background:var(--card);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:8px;font-size:13px}
textarea{width:100%;min-height:120px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.case-title{font-weight:600}
.muted{color:var(--muted);font-size:12px}
.row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap;align-items:center}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.badge.ok{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.badge.bad{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
.badge.queued{background:var(--pill);color:var(--accent)}
.badge.running{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.badge.src{background:var(--pill);color:var(--accent)}
pre{background:var(--code-bg);color:var(--code-fg);padding:12px;border-radius:8px;overflow:auto;font-size:12px;max-height:440px;white-space:pre-wrap;word-break:break-word}
summary{cursor:pointer;color:var(--accent);font-size:13px}
.link{color:var(--accent);cursor:pointer;font-size:13px}
.err{color:var(--bad)}
.options{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0}
.options input[type=number]{width:64px}
.collapse{display:none}
</style>
</head>
<body>
<header><h1>AI-Test-Tool Platform</h1><span class="sub">阶段1 · 平台壳（异步运行 + 用例 CRUD）</span></header>
<main>
  <div class="toolbar">
    <button id="btn-cases">刷新列表</button>
    <button class="sec" id="btn-new">新建用例</button>
    <button class="sec" id="btn-gen">从需求生成</button>
    <button class="sec" id="btn-reload">重载 DOM 目录</button>
  </div>
  <div class="options">
    <label class="muted">base_url 覆盖 <input id="opt-url" placeholder="默认读 config"></label>
    <label class="muted"><input type="checkbox" id="opt-heal"> 失败自愈</label>
    <label class="muted">重复 <input type="number" id="opt-repeat" value="1" min="1" max="5"></label>
  </div>

  <div id="new" class="card collapse">
    <div class="muted">手动录入或直接粘贴 LLM 产出的用例 JSON（case schema）。</div>
    <input id="new-title" placeholder="用例标题（可选）" style="width:100%;margin:8px 0">
    <textarea id="new-json" placeholder='{"id":"TC_001","title":"...","steps":[...],"target_framework":"playwright","base_path":"/login"}'></textarea>
    <div class="row"><button id="new-save">保存用例</button><span id="new-msg" class="muted"></span></div>
  </div>

  <div id="gen" class="card collapse">
    <div class="muted">需要服务端配置 LLM_API_KEY。从一句话需求生成用例并跑质量门。若未配置 Key，会直接打印将发给 LLM 的完整提示词，便于本地调试。</div>
    <textarea id="gen-req" placeholder="例如：登录后搜索产品旗舰版并加入购物车"></textarea>
    <div class="row" style="margin-top:8px"><label class="muted"><input type="checkbox" id="gen-heal"> 失败自愈</label><button id="gen-go">生成</button><span id="gen-msg" class="muted"></span></div>
    <pre id="gen-out" class="collapse"></pre>
  </div>

  <div id="list"></div>
</main>
<script>
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
function esc(t){const e=document.createElement('div');e.textContent=t==null?'':t;return e.innerHTML;}
function badge(ok){return ok?'<span class="badge ok">PASS</span>':'<span class="badge bad">FAIL</span>';}
function statusBadge(s){const m={queued:'queued',running:'running',success:'ok',failed:'bad'};
  const cls=m[s]||'queued';const label={queued:'排队中',running:'运行中',success:'通过',failed:'失败'}[s]||s;
  return `<span class="badge ${cls}">${label}</span>`;}
const OPT={url:()=>$('#opt-url').value.trim(),heal:()=>$('#opt-heal').checked,repeat:()=>Math.max(1,parseInt($('#opt-repeat').value||'1',10))};

async function api(url,opts){const r=await fetch(url,opts);const j=await r.json().catch(()=>({}));if(!r.ok){const err=Object.assign(new Error(j.detail||('HTTP '+r.status)),{json:j});throw err;}return j;}

async function loadCases(){
  const el=$('#list');el.innerHTML='加载中…';
  try{
    const cs=await api('/api/cases?limit=200');
    if(!cs.length){el.innerHTML='<div class="muted">暂无用例。点「新建用例」或「从需求生成」试试。</div>';return;}
    el.innerHTML='';
    for(const c of cs){
      const last=c.last_status;
      const card=document.createElement('div');card.className='card';
      card.innerHTML=
        `<div class="row" style="justify-content:space-between"><div><span class="case-title">${esc(c.title||'(无标题)')}</span> `+
        `<span class="badge src">${esc(c.source)}</span> ${statusBadge(last)}</div>`+
        `<div class="muted">${esc(c.id.slice(0,8))} · ${esc(c.created_at)}</div></div>`+
        `<div class="row">`+
        `<button data-run="${c.id}">运行</button>`+
        `<button class="sec" data-edit="${c.id}">编辑</button>`+
        `<button class="danger" data-del="${c.id}">删除</button>`+
        `<span class="link" data-detail="${c.id}">运行记录</span></div>`+
        `<div id="live-${c.id}"></div>`+
        `<div id="c-${c.id}" class="collapse"></div>`;
      el.appendChild(card);
    }
    el.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>runCase(b.dataset.run));
    el.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editCase(b.dataset.edit));
    el.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>delCase(b.dataset.del));
    el.querySelectorAll('[data-detail]').forEach(a=>a.onclick=()=>toggleDetail(a.dataset.detail,a));
  }catch(e){el.innerHTML='<div class="err">加载失败：'+esc(e.message)+'</div>';}
}

function toggleDetail(cid,a){
  const box=$('#c-'+cid);
  if(box.style.display==='block'){box.style.display='none';return;}
  box.style.display='block';
  if(!box.dataset.loaded){showCase(cid);box.dataset.loaded='1';}
}

async function runCase(cid){
  const box=$('#live-'+cid);box.innerHTML='<div class="muted">入队中…</div>';
  try{
    const j=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:cid,base_url:OPT.url(),heal:OPT.heal(),repeat:OPT.repeat()})});
    box.innerHTML='<div class="muted">任务已入队（'+j.run_id.slice(0,8)+'），轮询状态…</div>';
    pollRun(j.run_id,box);
  }catch(e){box.innerHTML='<div class="err">入队失败：'+esc(e.message)+'</div>';}
}

function pollRun(rid,box){
  const timer=setInterval(async()=>{
    try{
      const r=await api('/api/runs/'+rid);
      box.innerHTML='<div class="row"><div><div class="muted">状态</div>'+statusBadge(r.status)+'</div>'+
        (r.status==='success'||r.status==='failed'?'':`<div class="muted">运行中（pytest 可能需数十秒）…</div>`)+'</div>';
      if(r.status==='success'||r.status==='failed'){
        clearInterval(timer);
        box.innerHTML+=renderRun(r);
        loadCases();
      }
    }catch(e){clearInterval(timer);box.innerHTML='<div class="err">轮询失败：'+esc(e.message)+'</div>';}
  },1000);
}

function renderPromptPreview(j){
  const p=j.prompt_preview||{};const msgs=p.messages||[];
  let h='<div class="err">'+esc(j.error||'')+'</div>';
  h+='<div class="muted">'+(j.note||'')+'</div>';
  if(p.secrets&&p.secrets.length)h+='<div class="muted">脱敏占位符（仅出网这些，真值在本地）：'+esc(p.secrets.map(s=>'${'+s+'}').join(' '))+'</div>';
  msgs.forEach(m=>{h+='<div class="muted">── role='+esc(m.role)+' ──</div><pre>'+esc(m.content)+'</pre>';});
  if(p.schema)h+='<details><summary>response_format: json_schema(test_case) · 点击展开完整输出约束</summary><pre>'+esc(JSON.stringify(p.schema,null,2))+'</pre></details>';
  if(p.provider)h+='<div class="muted">provider：'+esc(JSON.stringify(p.provider))+'</div>';
  return h;
}

function renderRun(r){
  let h='<div class="row"><div><div class="muted">质量门</div>'+badge(r.gate_passed)+'</div>'+
    `<div><div class="muted">pytest</div>exit=${r.exit_code} passed=${r.passed} failed=${r.failed}</div>`+
    `<div><div class="muted">状态</div>${statusBadge(r.status)}</div></div>`;
  if(r.reasons&&r.reasons.length)h+='<div class="muted">质量门原因：</div><pre>'+esc(r.reasons.join('\n'))+'</pre>';
  if(r.locator_warnings&&r.locator_warnings.length)h+='<div class="muted">定位器建议：</div><pre>'+esc(r.locator_warnings.join('\n'))+'</pre>';
  if(r.heal_summary)h+='<div class="muted">Healer：</div><pre>'+esc(JSON.stringify(r.heal_summary,null,2))+'</pre>';
  if(r.meta)h+='<div class="muted">运行元信息：</div><pre>'+esc(JSON.stringify(r.meta,null,2))+'</pre>';
  h+='<details><summary>生成代码</summary><pre>'+esc(r.code||'')+'</pre></details>';
  h+='<details><summary>执行输出</summary><pre>'+esc(r.output||'')+'</pre></details>';
  return h;
}

async function showCase(cid){
  const box=$('#c-'+cid);box.innerHTML='加载中…';
  try{
    const c=await api('/api/cases/'+cid);
    let html='<div class="muted">运行记录（'+c.runs.length+'）：</div>';
    if(!c.runs.length)html+='<div class="muted">无</div>';
    for(const r of c.runs){
      html+='<div class="card" style="margin:6px 0"><div class="row" style="justify-content:space-between"><div>'+statusBadge(r.status)+
        ` <span class="muted">${esc(r.source)} · ${esc(r.id.slice(0,8))} · exit=${r.exit_code} passed=${r.passed} failed=${r.failed}</span></div></div>`+
        '<div class="link" data-rid="'+r.id+'">查看运行详情</div><div id="r-'+r.id+'"></div></div>';
    }
    box.innerHTML=html;
    box.querySelectorAll('[data-rid]').forEach(a=>a.onclick=async()=>{const rb=$('#r-'+a.dataset.rid);rb.innerHTML='加载中…';const rr=await api('/api/runs/'+a.dataset.rid);rb.innerHTML=renderRun(rr);});
  }catch(e){box.innerHTML='<div class="err">加载失败：'+esc(e.message)+'</div>';}
}

async function editCase(cid){
  try{
    const c=await api('/api/cases/'+cid);
    const card=document.querySelector(`[data-edit="${cid}"]`).closest('.card');
    let box=card.querySelector('.edit-box');
    if(box){box.remove();return;}
    box=document.createElement('div');box.className='edit-box card';box.style.marginTop='8px';
    box.innerHTML=`<input id="ed-title-${cid}" value="${esc(c.title||'')}" style="width:100%;margin-bottom:8px" placeholder="标题（可选）">`+
      `<textarea id="ed-json-${cid}">${esc(JSON.stringify(c.case_json,null,2))}</textarea>`+
      `<div class="row"><button id="ed-save-${cid}">保存</button><span id="ed-msg-${cid}" class="muted"></span></div>`;
    card.appendChild(box);
    $('#ed-save-'+cid).onclick=async()=>{
      const msg=$('#ed-msg-'+cid);msg.textContent='保存中…';
      let parsed;try{parsed=JSON.parse($('#ed-json-'+cid).value);}catch(e){msg.textContent='JSON 解析失败：'+e.message;return;}
      try{
        await api('/api/cases/'+cid,{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({title:$('#ed-title-'+cid).value.trim(),case_json:parsed})});
        msg.textContent='已保存';box.remove();loadCases();
      }catch(e){msg.textContent='保存失败：'+e.message;}
    };
  }catch(e){alert('加载用例失败：'+e.message);}
}

async function delCase(cid){
  if(!confirm('确认删除该用例及其全部运行记录？'))return;
  try{await api('/api/cases/'+cid,{method:'DELETE'});loadCases();}
  catch(e){alert('删除失败：'+e.message);}
}

$('#btn-cases').onclick=loadCases;
$('#btn-new').onclick=()=>{const g=$('#new');g.classList.toggle('collapse');};
$('#btn-gen').onclick=()=>{const g=$('#gen');g.classList.toggle('collapse');};
$('#btn-reload').onclick=async()=>{const j=await api('/api/reload-catalog',{method:'POST'});alert('DOM 目录已重载，testid 数：'+(j.testids||0));};
$('#new-save').onclick=async()=>{
  const msg=$('#new-msg');msg.textContent='保存中…';
  let parsed;try{parsed=JSON.parse($('#new-json').value);}catch(e){msg.textContent='JSON 解析失败：'+e.message;return;}
  try{
    const j=await api('/api/cases',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_json:parsed,title:$('#new-title').value.trim()})});
    msg.textContent='已创建：'+j.case_id.slice(0,8);$('#new-json').value='';loadCases();
  }catch(e){msg.textContent='保存失败：'+e.message;}
};
$('#gen-go').onclick=async()=>{
  const out=$('#gen-out');out.classList.remove('collapse');out.textContent='生成中…';$('#gen-msg').textContent='';
  try{
    const r=await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({requirement:$('#gen-req').value,heal:$('#gen-heal').checked})});
    out.textContent=JSON.stringify(r,null,2);$('#gen-msg').textContent='已生成并落库';loadCases();
  }catch(e){
    if(e.json&&e.json.prompt_preview){
      out.innerHTML=renderPromptPreview(e.json);
      $('#gen-msg').textContent='未配置 LLM_API_KEY：已打印完整提示词';
    }else{
      out.textContent='错误：'+e.message;
    }
  }
};
loadCases();
</script>
</body>
</html>
"""
