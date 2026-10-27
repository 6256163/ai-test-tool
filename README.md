# AI 测试提效工具 —— P0 最小可跑

按运行时流程图的 **P0 阶段**落地：输入一句话 → 生成 1 条 Playwright 用例 → 能跑 + 能判对错。

## 目录（P0 子集）

```
ai-test-tool/
├─ config.yaml            # 模型 / 框架 / 质量门配置
├─ requirements.txt
├─ conftest.py            # 提供生成用例依赖的 async `page` fixture
├─ ingest/
│  ├─ schema.py           # 任务/用例/步骤的 Pydantic 契约  ★
│  └─ parser.py           # 需求 → 结构化任务（P0 极简）
├─ generator/
│  ├─ llm_client.py       # LLM 封装：结构化输出+脱敏+重试+平台提示词  ★
│  └─ codegen.py          # schema → Playwright/Appium 代码（后端多态）★
├─ runner/
│  └─ executor.py         # 子进程跑 pytest 收集结果
├─ evaluate/
│  └─ judge.py            # 质量门（防伪通过 + 平台兼容性 + 定位器校验）★
├─ security/              # P1：敏感数据「占位出网 + 本机回填」+ 凭据注册表 ★
│  └─ secrets.py
├─ tests/
│  └─ test_secrets.py     # 脱敏/回填/注册表回归测试
├─ prober/                # P1：前端定位器目录（DOM → locator hints）★
│  ├─ dom_catalog.py      # 静态扫描 data-testid + 路由 → route→[testid]
│  ├─ build_catalog.py    # 抽取并落盘 catalog.json
│  ├─ verify_catalog.py   # 真实浏览器核对 locator 是否命中实时 DOM
│  └─ catalog.json        # 生成的目录（当前对接 target_demo 前端）
├─ main.py                # 流水线入口 / CLI（支持 --platform）
└─ scenarios/
   ├─ demo.py             # 离线演示（MockLLM，不需 API key）
   └─ dual_platform_demo.py  # Web/App 双端 codegen 分发验证
```

★ = 本次重点实现的 P0 文件。

## 快速开始

```bash
cd ai-test-tool
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 1) 离线演示（不调模型，验证代码生成 + 质量门）
python scenarios/demo.py

# 2) 真实跑（需要 LLM_API_KEY）
export LLM_API_KEY=sk-xxx
export TARGET_DEMO_ADMIN_PWD=admin123   # 凭据注册表引用的环境变量
python main.py "在登录页输入手机号和密码，点击登录，预期进入首页"

# 3) 排查提示词：打印「这句话最终发给 LLM 的完整内容」（不调模型、不花钱）
python prober/dump_prompt.py "使用admin/admin123 在登录页登录，预期跳转到商品页"
python prober/dump_prompt.py "..." --no-hints   # 对比：不注入 DOM 目录的盲生成
python prober/dump_prompt.py "..." --json       # 输出完整 payload（含 response_format schema）
```

> ⚠️ **解释器陷阱**：本项目依赖（PyYAML / pydantic / httpx / playwright）只装在虚拟环境里。
> 直接敲 `python3 main.py` / `python3 prober/dump_prompt.py` 会报 `ModuleNotFoundError: No module named 'yaml'`。
> 必须先 `source .venv/bin/activate`，或直接用 venv 里的解释器绝对路径。
> 两个入口都已内置依赖自检，缺依赖时会直接提示正确命令（退出码 2），不再抛 traceback。

## 设计要点（对应避坑清单）

- **P0 先不做编排/多智能体**：这里只有 解析→生成单条→跑→判 一条线，先验证「AI 用例到底能不能用」。
- **质量门最先写**：`judge.py` 强制「执行通过 + 代码真有断言 + 覆盖标签达标」，专治「看起来绿、实际没断言」。
- **数据安全**：`llm_client.mask_sensitive` 在出域前打码手机号/邮箱/身份证；私有化模型可关脱敏。
- **结构化输出**：用 JSON Schema 让模型直接吐 `Case`，避免脆弱的文本解析；校验失败会回退让模型修一次。

## 下一步（P1）

已落地 **locator 精准化的 hint 部分**（见下方 P1 章节）。剩余：`runner.healer`（执行期定位器自愈）、
`evaluate.coverage`（覆盖率）、`feedback.store`（入库进 CI）、`orchestrator.planner`（批量拆解 + 账号抽取/脱敏）。

---

## Web / App 双端适配（已落地骨架）

`Case` schema 是**平台无关**的契约（动作原子 + 定位器 + 覆盖标签），所以扩展双端只需在「codegen 后端 + config + LLM 提示词 + 质量门」四层按 `target_framework` 分流，**schema 与质量门主体复用不重写**。

### 动作分层（schema.py）
- **通用**：`navigate / click / fill / wait / assert_text / assert_visible / assert_count / scroll`
- **移动端新增**：`swipe`（手势）、`long_press`（长按）、`launch`（启动 app，替代 navigate）
- **web-only**：`hover`（触屏无）、`select`（原生用 picker）、`assert_url`（app 无 URL）
- `ACTION_PLATFORMS` 标记每个动作适用平台；`framework_to_platform()` 把 `playwright→web`、`appium→app` 换算。

### codegen 后端多态（codegen.py）
- `PlaywrightBackend`：生成 pytest + Playwright；`mobile: true` 时注入移动视口（`set_viewport_size`）走 H5 设备模拟。
- `AppiumBackend`：生成 Appium 代码（`ACCESSIBILITY_ID` / `UIAutomator` 定位、`send_keys` / `tap` / `TouchAction` 手势、`activate_app` 启动）。
- `get_backend(case)` 按 `target_framework` 自动选后端。

### 配置（config.yaml）
```yaml
framework:
  type: "web"            # web / app
  web:
    base_url: "http://localhost:8081"
    mobile: false        # mobile H5：用移动视口加载
  app:
    platform_name: "Android"
    app_package: "com.shopdemo.app"
    app_activity: ".MainActivity"
    device_name: "emulator-5554"
    app: "/path/to/app.apk"
```

### 平台提示词（llm_client.py）
`generate_case(..., platform="app")` 改用 `_SYS_APP`：明确「用 launch 不用 navigate、禁用 hover、滚动用 scroll、手势用 swipe」，避免模型在 app 用例里写 web-only 动作。

### 质量门平台校验（judge.py）
`_platform_violations()`：app 用例若含 `hover/select/navigate/assert_url` 等 web-only 动作，质量门直接拦截并提示改用 `launch/swipe` 等。

### 验证
```bash
python scenarios/dual_platform_demo.py   # 对比两条用例生成的 Playwright / Appium 代码
# 真实跑可强制平台：
python main.py "..." --platform app
```

> 注：App 端骨架已能正确分发代码与提示词；真正跑通还需 Appium Server + 模拟器/真机，以及把 `conftest` 的 driver fixture（带 caps）接上——这部分留给 P1 环境对接。

---

## P1 · 与前端 DOM 结合：定位器目录（已落地）

**问题**：P0 里 LLM 凭「需求 + 训练知识」猜选择器，常生成前端根本不存在的 locator，用例跑起来「元素找不到」。
**解法**：把前端当「契约方」，抽出 `data-testid` + 路由 → **定位器目录（catalog）**，生成用例时注入 LLM，让它从清单里选，而不是猜。

### 链路
```
前端源码 --静态扫描--> route→[testid] 目录 --to_hints_text--> 注入 LLM 提示词
                                                        └─> LLM 只选清单内 testid → 精准命中
（升级项）真实浏览器爬取 --> 实时 DOM 目录（覆盖动态/条件渲染元素）
```

### 1) 配置前端源码目录（config.yaml）
```yaml
framework:
  web:
    base_url: "http://localhost:8081"
    frontend_src: "/path/to/frontend/src"   # 为空则退化为盲生成
```

### 2) 抽取目录 / 核对命中
```bash
python prober/build_catalog.py                 # 扫描源码 → prober/catalog.json
python prober/verify_catalog.py --route /login # 真实浏览器核对（9/9 命中）
# 受保护路由先登录再核对（用 catalog 里的登录 testid 真登录）：
python prober/verify_catalog.py --route /products --login --user admin --password admin123
```

### 3) 生成时自动注入
`main.py` 的 `_build_dom_hints()` 读取 `frontend_src` → `scan_source()` → `Catalog.to_hints_text()`，
作为 `dom_hints` 传给 `LLMClient.generate_case()`；提示词规则收紧为「必须从清单选 testid，严禁自创」。

### 设计要点
- **定位器优先级**：`testid > id > role+name > css`。target_demo 用 `data-testid`，契约固定在 testid 上。
- **过滤动态 testid**：`product-card-${p.id}` 这类模板动态值静态匹配不到，扫描时剔除，避免误导 LLM。
- **只看「存在于 DOM(attached)」**：Element UI 的 tab 用 `v-show` 隐藏非激活页，元素存在但不可见，不能因不可见判 locator 无效。
- **同 testid 多元素必须带 `index`**：商品列表每条都有 `product-name`，strict mode 下直接抛
  `resolved to 12 elements`。catalog 会自动识别 `v-for` 块内的 testid（`repeated_testids`），
  hints 里明确告警；`Locator.index`（0=首个，-1=末个）表达选取第几个 → codegen 生成 `.first`/`.nth(n)`/`.last`，
  Appium 端生成 `find_elements(...)[i]`。**漏带 index 会在执行前被质量门拦下**（详见下一节）。
- **长期根本**：前端持续维护 `data-testid` 纪律——这才是「确保 locator 准确」的契约来源。

## P1 · 生成后校验与「用例真能跑」（已落地）

用一条真实 LLM 返回（`navigate.value=" /login"`、`fill` 用 `by=text`、`assert_url.expect=" /products"`）
实测暴露了 4 个与用例质量无关的缺陷，已全部修复：

| 缺陷 | 现象 | 修复 |
|------|------|------|
| 缺 `page` fixture | `fixture 'page' not found`，任何 web 用例都跑不起来 | 新增根 `conftest.py`（装了 `pytest-playwright` 时自动让位） |
| 路径前导空格 | `goto("http://localhost:8081 /login")` → `Cannot navigate to invalid URL` | `PlaywrightBackend._url_expr()` 统一 `strip()` + 规范化为 `/xxx` |
| `assert_url` 不拼 base_url | `to_have_url('/products')` 永远不成立（实测 `**/products` 等 glob 也不匹配，只有完整 URL 能过） | 相对路径自动拼成 `f"{BASE_URL}/products"` |
| `fill` 用 text 定位 | `get_by_text("用户名")` 命中 2 个（登录/注册 tab 都在 DOM），对 label 调 `fill` 必然失败 | `judge.lint_locators()` 直接判硬伤 |

`runner/executor.py` 同时修了两处：改用 `sys.executable -m pytest`（不再依赖 PATH 里的裸 `pytest`），
并把 `conftest.py` 复制进临时目录（pytest 只收集用例文件祖先目录下的 conftest，`/tmp` 里的用例原本拿不到 fixture）。

`main.py` 现在把同一份 `Catalog` 同时用于「生成前 hint」和「生成后校验」，并额外打印定位器建议：

```python
errors, warnings = lint_locators(case, catalog)
# errors（质量门 FAIL）：
#   ① testid 不在目录中 → 幻觉定位器，附 difflib 最接近项
#   ② base_path 不在路由表 → 幻觉路由
#   ③ fill/select 用 text 定位 → 必然失败（命中文案节点而非输入框）
#   ④ 列表元素（v-for 内 testid）漏带 index → strict mode 必挂
#   ⑤ note 点名了某 testid 但整条用例从未定位它 → 承诺的校验没实现
# warnings（仅提示）：
#   ⑥ 该路由有 testid 却退回 text/css → 稳定性差
```

实测输出：
```
❌ 步骤4 定位器幻觉：testid `input-user-name` 不在前端目录中，最接近的是 `input-username`
❌ base_path `/signin` 不在前端路由表中，最接近的是 `/login`
❌ 步骤2 fill 用 text 定位 `用户名`：text 命中的是文案节点而非输入框，必然失败；请改用 testid 或 css
```

## P1 · 凭据脱敏 + 凭据注册表（已落地，`security/secrets.py`）

原 `mask_sensitive` 只有「出」没有「回」：mask 后 LLM 会把 `${PHONE}` 当输入值写进 `value`，
生成的脚本变成 `fill('${PHONE}')` —— **脱了敏就等于用例必然失败**。所以现在是成对实现：

```
需求句 → extract_secrets → 占位符出网（真值留本机 last_secrets）
                                    ↓ LLM 返回带 ${ACCOUNT}/${PASSWORD} 的 Case
本机 reconcile_with_registry + rehydrate_case → case_to_code（真值只出现在本地脚本里）
```

### A. 能脱敏的格式

| 类型 | 示例原文 | 出网形式 |
|------|----------|----------|
| 账号/密码对 | `admin/admin123`、`账号：admin 密码：admin123` | `${ACCOUNT}` / `${PASSWORD}` |
| KV 参数 | `token=abc123`、`password=xxx` | `token=${TOKEN}`、`password=${PASSWORD}` |
| Authorization | `Authorization: Bearer eyJ...` | `Authorization: Bearer ${TOKEN}` |
| URL basic-auth | `https://u:p@host/v1` | `https://${ACCOUNT}:${PASSWORD}@host/v1` |
| 独立 JWT / API Key | `eyJhbGci...`、`sk-xxx`、`AKIA...` | `${JWT}` / `${API_KEY}` |
| PEM 私钥 | `-----BEGIN PRIVATE KEY-----...` | `${PRIVATE_KEY}` |
| 手机 / 邮箱 / 身份证 | `13800000000` 等 | `${PHONE}` / `${EMAIL}` / `${ID}` |

规则顺序：**先复合凭据、后通用单项**，否则「恰好由手机号构成的密码」会被拦腰半掩。
`admin/xxx` 这类斜杠对**仅在存在登录上下文**（登录/账号/密码/login/auth 等）时才启用——
否则 `打开 /products`、`分页 1/17` 都会被误伤（已写进 `tests/test_secrets.py`）。

### B. 凭据注册表（`config.credentials`）

```yaml
credentials:
  enabled: true
  profiles:
    admin:
      username: admin
      password: "${TARGET_DEMO_ADMIN_PWD}"   # 推荐走环境变量
    tester: { username: tester, password: "123456" }
```

- LLM 只会看到槽位名 `${CRED.admin.password}`，**提示词里注入的槽位说明不含任何真值**。
- 句中抽到的账号若命中注册表条目，**真值以注册表为准**并告警：

```
⚠️ 句中密码与注册表 `admin` 不一致，已按注册表回填（admin123456 → admin123）
```

这一步同时根治了「手打密码写错导致用例必然失败」——需求句写错也不影响生成结果。

### 环境前提

```bash
export TARGET_DEMO_ADMIN_PWD=admin123   # 未设置则该字段被跳过，不会用空串去填用例
python -m pytest tests/test_secrets.py -q
```

实测（需求句故意写 `admin/admin123456`，假模型返回带占位符的用例）：
出网消息中真密码出现 **0 次**，生成脚本 `fill('admin123')`，`execute()` → `1 passed`，质量门 PASS。

## P1 · 定位器自愈 Healer（已落地，`runner/healer.py`）

执行失败 ≠ 产品 bug。相当一部分失败是「选择器 drift」：列表/表格里多个同名元素触发
Playwright strict mode、前端重构把 `data-testid` 改名、或 LLM 幻觉出目录里不存在的定位器。
Healer 在**「不改断言」护栏**下，用「运行时报错 + 前端目录（catalog）」自动修补 `Case` 的 `locator`
再重跑：

| 失败类型 | 识别依据 | 自愈动作 |
|----------|----------|----------|
| `strict_mode`（多元素命中） | `strict mode violation ... resolved to N elements` | 给 `locator.index` 补 `0`（`get_by_test_id(x).first`） |
| `not_found` 且 catalog 无此 testid | `Timeout ... exceeded` + 定位器不在目录 | 用 `difflib` 找最接近的重命名并替换 `value` |
| `not_found` 但 catalog 有此 testid | 同上，但 testid 在目录里 | **不猜**（可能是路由/上下文错）→ 标「需人工评审」 |
| `timeout_visible`（元素在但不可见） | `is not visible` 且不含 timeout | **不改**（可能是真 bug，自愈会掩盖）→ 标「需人工评审」 |

### 护栏：禁改断言

只改 `locator` 的 `by / value / index`；**绝不修改 `Step.expect`**，也绝不把 `assert_*` 降级成更弱的
检查。自愈可以修定位器，但不能把失败的断言改成通过——这和「三智能体替换」里讨论的 Healer 护栏一致。

### 解析与循环

- `parse_failures(output)`：只扫 pytest 的 `E` 前缀报错行（源码行 `>`、摘要行自动排除），跳过
  Playwright 多行错误的续行（如 `- Expect ...`），避免把同一条 strict-mode 误拆成「not_found」。
- `heal_case(case, failures, catalog)`：静态修补，返回「已自愈 / 需评审 / 无法自愈」三类清单。
- `heal_and_rerun(case, ...)`：执行 → 解析 → 修补 → 重新生成代码再跑，最多 `max_attempts` 轮；
  一轮内若无任何可应用的修补立即停，避免对「逻辑错误 / 断言失败」空转。

### 用法

```bash
# 复审入口：执行失败后自动自愈并重跑（target_demo 在线时真跑浏览器）
aitest/bin/python scenarios/review_case.py scenarios/sample_cases/tc_heal_demo.json --heal

# 流水线入口：失败可选触发自愈
aitest/bin/python main.py "一句话需求" --heal
```

样例 `tc_heal_demo.json` 故意漏写 `product-name` 的 index，首跑 strict mode 失败，Healer 自动补
`.first` 后第 2 轮跑绿；单元覆盖见 `tests/test_healer.py`（解析分类、strict 补 index、catalog 重命名、
catalog 内有但当前页无→需评审、断言 expect 不被改、heal_and_rerun 循环与停止条件）。

## 复审实录：TC_PURCHASE_001（加购主流程）

一条「看起来很合理」的 LLM 用例，实测暴露了三类非脱敏问题。样例留档在 `scenarios/sample_cases/`：
`tc_purchase_001_llm_raw.json`（原始返回）/ `tc_purchase_001.json`（修正后可跑绿）。

| # | 问题 | 实测证据 | 归属 |
|---|------|----------|------|
| 1 | 列表元素未带 index | `expect(get_by_test_id('product-name'))` → `strict mode violation: resolved to 12 elements`，断言和 click 双双失败 | LLM 缺信息 + schema 无表达手段 → **已支持 `Locator.index`** |
| 2 | 依赖未清理的共享状态 | 去掉「清空购物车」后连跑：`cart-total` 从 `¥44.8` → `¥67.2` 单调累加；只有先清空才稳定为 `¥22.4` | 用例设计问题（缺 precondition）→ 补 `navigate /cart` + `click btn-cart-clear` |
| 3 | note 承诺了步骤没做的事 | 「购物车单价与详情页 detail-price 一致（运行时比对）」实际只有 `assert cart-table contains 旗舰版`，压根没比价 | 提示词需禁止「note 里写步骤未实现的校验」 |

修正后连跑 3 次：`exit=0 passed=1`，质量门 PASS ✅。

> 经验：**断言写「= 1」这类绝对值前，先确认被测状态是否被隔离**。共享账号（admin）的购物车会被历史用例污染，
> 表现为「第一次绿、第二次挂」的假 flaky——不是框架问题，是用例没做数据隔离。

### 第二轮复审：把上面的「人工发现」变成「自动拦截」

上一轮的第 1、3 条靠人眼揪出来，这一轮把它们固化进流水线：

**① note 承诺未兑现 → `judge` 硬伤（新增规则 ⑤）**

```python
_testid_like_tokens(note)          # 从 note 抽出「长得像 testid」的 token
_unrealized_claims(case, known)    # 该 testid 在目录里存在，但没有任何步骤定位过它 → error
```

再次拿到同一份 LLM 返回，无需执行浏览器即可拦截：

```
❌ 步骤8  列表元素未指定 index：testid `product-name` 位于 v-for 内（步骤9 同样）
❌ 步骤13 note 声称校验 `detail-price`，但整条用例没有步骤定位过它 → 该校验并未真正实现
```

这条规则比「断言写错」更难发现：代码照常跑绿，但被测功能从未被验证过。
单元测试见 `tests/test_note_claims.py`（含「补上真实 assert 后应放行」「普通中文 note 不误伤」「目录外的 token 不管」三个反向用例）。

**② 重复执行一致性（flaky 检测）→ `scenarios/review_case.py --repeat N`**

一次性完成「静态校验 → codegen → 真跑 N 次 → 质量门」，结果不一致时明确提示环境依赖：

```bash
aitest/bin/python scenarios/review_case.py scenarios/sample_cases/tc_purchase_001_raw2_fixed.json --repeat 3
#   第1次: ✅ exit=0 passed=1 failed=0
#   第2次: ✅ exit=0 passed=1 failed=0
#   第3次: ✅ exit=0 passed=1 failed=0
#   ✅ 连续 3 次一致通过
```

对照实验（从修正版里删掉「清空购物车」两步，其余不变）：`cart-total` 逐次累加 `¥44.8 → ¥67.2`，
两次都 FAIL —— 证明隔离步骤是必需的，而不是可有可无的保险。

---

## 阶段0 · 平台服务化（已落地，`server/`）

把现有引擎（需求 → 用例 → 代码 → 执行 → 质量门）套一层 FastAPI + SQLite 的壳，**引擎逻辑零重写**。
目标：引擎能被远程调用、运行结果能查，先验证「服务化是否通顺」。

### 启动

```bash
# 在 aitest venv 内（已装 fastapi/uvicorn）
python -m uvicorn server.app:app \
    --host 0.0.0.0 --port 8000
```

打开 http://localhost:8000 即是最简结果页（用例列表 + 运行记录 + 代码/输出查看）。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/generate` | 从一句话需求生成用例并跑质量门（**需要服务端配置 `LLM_API_KEY`**）。缺 key 时返回 400，且 body 内附带 `prompt_preview`（完整提示词），便于本地调试 |
| POST | `/api/run-case` | 提交已有用例 JSON（case schema），跑 codegen + 执行 + 质量门（**绕过 LLM，无 key 也能用**） |
| POST | `/api/reload-catalog` | 前端源码改了之后重扫 DOM 目录（否则缓存一次后不更新） |
| GET  | `/api/cases` `/api/cases/{id}` | 用例列表 / 详情（含运行记录） |
| GET  | `/api/runs` `/api/runs/{id}` | 运行记录列表 / 详情（含代码、输出、质量门） |
| GET  | `/` | 自包含 HTML 结果页 |

`/api/run-case` 的请求体：`{ "case_json": {...}, "base_url": "可选覆盖", "heal": false }`。
同步的引擎调用用 `run_in_threadpool` 包裹，避免阻塞异步事件循环。

### 无 LLM key 时怎么用

本机若没配 `LLM_API_KEY`，`/api/generate` **仍会返回 400，但响应体里附带 `prompt_preview`**——
即「本可发给 LLM 的完整提示词」（含脱敏占位符、DOM 目录 hints、JSON Schema），方便你复制下来本地
调试或用兼容 OpenAI 协议的客户端（DeepSeek / 通义 / 本地 vLLM）自行调用。
Web UI 的「从需求生成」按钮在缺 key 时会自动把这段提示词渲染出来（system/user 完整展示，Schema 折叠）。

- 想真正生成：在 `config.yaml` 的 `llm.api_key` 处填 `${LLM_API_KEY}` 并在环境变量里 export，或直接在命令行用 `python prober/dump_prompt.py "一句话需求"` 看同样的提示词。
- 不打算配 key：用 `/api/run-case`（或页面「提交用例 JSON」）直接跑已有用例，无需模型。

### 存储

本地 SQLite：`data/platform.db`（`server/db.py`，零额外依赖）。只存用例元数据 + 运行结果摘要 + 代码 + 输出。

> ⚠️ 安全说明：落库为本地文件，**未做二次脱敏**——`/api/run-case` 提交的 case_json 若含明文密码（如 `admin123`）
> 会原样入库。因此 `data/` 已在 `.gitignore` 中，**绝不进仓库**；请勿上传含真实生产凭据的用例。

### 文件清单

- `server/app.py`：FastAPI 应用（路由 + 结果页）
- `server/db.py`：SQLite 存储层
- `requirements-server.txt`：服务层依赖（fastapi/uvicorn）

## 阶段1 · 平台壳（已落地）

在阶段0 之上，把「个人 CLI 工具」升级为「团队能自助用的服务」：**异步任务执行 + 用例 CRUD + 完整 UI**。引擎逻辑依旧零重写。

### 新增能力
- **异步运行 + 状态轮询**：`POST /api/runs` 入队一条任务，由后台 daemon worker 线程**串行**跑 pytest，前端用 `GET /api/runs/{id}` 轮询到 `queued → running → success/failed`。浏览器执行不再阻塞 FastAPI 事件循环。
- **用例 CRUD**：`POST /api/cases`（新建）、`PUT /api/cases/{id}`（编辑）、`DELETE /api/cases/{id}`（删除，级联删运行记录）。
- **平台壳 UI**：用例列表（含最近运行状态徽标）、新建/编辑（内联 JSON 编辑器）、删除（带确认）、一键异步运行并实时展开结果（代码/输出/质量门/Healer/元信息）。
- **执行核心收敛**：`server/engine.py` 抽出 `run_case()`，同步接口（`run_in_threadpool`）与异步 worker 共用，消除重复逻辑。
- **断点续跑**：worker 启动时把 DB 里卡在 `queued/running` 的任务重新入队，进程重启不丢任务。
- **flaky 检测**：运行可带 `repeat>1`，`meta` 返回 `{"repeat","oks","stable"}`，连续结果不一致即提示用例有环境依赖。

### 接口（阶段0 之上新增/变更）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/cases` | 新建用例 `{case_json, title?}` → `{case_id,...}` |
| PUT  | `/api/cases/{id}` | 编辑用例 `{title?, case_json?}` |
| DELETE | `/api/cases/{id}` | 删除用例（级联删运行记录） |
| POST | `/api/runs` | 异步入队 `{case_id? 或 case_json?, base_url?, heal?, repeat?}` → `{run_id, case_id, status:"queued"}` |
| GET  | `/api/runs/{id}` | 运行详情（含 `status/started_at/finished_at/params/meta`） |

`/api/runs` 优先用 `case_id`；给了 `case_json` 会先落库成 manual 用例再跑。

### 运行模型与限制
- **单 worker 串行**：当前只有一个后台线程一次跑一个任务，避免并发 Playwright 抢 profile/端口。对 MVP 足够；阶段3 再扩成多 worker 网格。
- **进程模型**：`uvicorn server.app:app` 单进程（未开 `--workers`）。若开多进程，每个进程各有一个 worker 线程，任务可能重复消费——现阶段请勿开多进程。
- 安全取舍同阶段0：`data/` 不进仓库；运行记录里的 case_json 若含明文凭据会原样入库。

### 文件清单（阶段1 新增/变更）
- `server/engine.py`（新增）：配置/catalog 缓存 + `run_case()` 执行核心（支持 repeat/flaky）。
- `server/jobs.py`（新增）：后台 worker 队列 + `enqueue_run` + 启动恢复。
- `server/db.py`（变更）：runs 表迁移 `status/params/started_at/finished_at/meta`；新增 `create_run_pending`/`update_run`/`list_runs_by_status`/`update_case`/`delete_case`；`list_cases` 带 `last_status`。
- `server/app.py`（变更）：接入 engine/jobs，新增 CRUD + 异步运行入口，UI 升级为平台壳。

**③ 本轮仍然只提示、不自动改的项**

- **冗余步骤**：`ProductDetailView` 加购成功后自身会 `router.push('/cart')`，再点一次 `menu-cart` 属多余（无害，但会掩盖导航行为变化）。
- **隐式路由依赖**：登录后由前端自行跳转 `/products`，用例没 `navigate` 也能过；一旦产品改了落地页就会挂。建议补 `assert_url /products`。
- **搜索断言不够强**：只断言「第一条包含关键词」，搜索完全失效时（第一条恰好是该商品）也能通过。应改为断言结果集整体（`to_have_count` 变化 / 遍历全部）。

样例留档：`tc_purchase_001_llm_raw2.json`（本轮原始返回）/ `tc_purchase_001_raw2_fixed.json`（修正后可跑绿）。

---

## Web UI 使用指南

平台壳就绪后，绝大多数操作都可以不碰命令行，直接在浏览器完成。下面把「怎么打开、要配什么、界面怎么用」一次说清。

### 1. 快速开始（打开界面）

```bash
cd ai-test-tool
python -m uvicorn server.app:app \
    --host 0.0.0.0 --port 8000
```

启动后在浏览器打开 **http://localhost:8000** 即可。进程常驻后台，无需每次重开（除非机器重启或你手动停掉）。

> 提示：本机若未配 `LLM_API_KEY`，页面上的「一句话生成用例」会弹 400 提示；但不影响其余功能——用「提交用例 JSON」或点已有用例的「运行」即可真跑浏览器。

### 2. 配置项一览

Web UI 背后依赖以下两处配置，修改后**重启服务**生效（仅前端源码变更可用「重载目录」免重启）。

| 配置位置 | 键 | 作用 | 备注 |
|---------|----|------|------|
| 环境变量 | `LLM_API_KEY` | 开启「一句话生成用例」 | 未设置则 `/api/generate` 返回 400；可配合 `llm.base_url` 接任意兼容 OpenAI 协议的模型（DeepSeek / 通义 / 本地 vLLM） |
| 环境变量 | `TARGET_DEMO_ADMIN_PWD` | 回填 admin 账号密码（脱敏注册表） | 对应 `credentials.profiles.admin.password: ${TARGET_DEMO_ADMIN_PWD}`；缺失则跳过该字段 |
| `config.yaml` | `framework.web.base_url` | 被测系统默认地址 | UI 顶部「全局 base_url」可临时覆盖，优先级高于此处 |
| `config.yaml` | `framework.web.frontend_src` | 前端源码目录，静态抽取 `data-testid` + 路由 | 改为目标项目前端源码路径即可适配新被测系统；留空则跳过目录抽取（LLM 退化为盲生成，定位器准确率下降） |
| `config.yaml` | `framework.type` | `web` / `app` | 决定走 Playwright 还是 Appium 的 codegen 与提示词 |
| `config.yaml` | `gate.min_coverage_tags` / `gate.require_assert` | 质量门阈值 | 用例至少带 N 个覆盖标签、是否强制必须有断言 |

前端源码改了之后，点 UI 上的「**重载目录**」按钮（对应 `POST /api/reload-catalog`）重新扫描，无需重启服务。

### 3. 界面功能分区

- **用例列表**：所有已落库用例，每行带「最近状态」徽标（`success` / `failed` / 无记录）。可搜索、新建、编辑、删除。
- **新建 / 编辑**：内联 JSON 编辑器，粘贴符合 `case` schema 的 JSON（参考 `scenarios/sample_cases/*.json`）。保存即落库。
- **运行参数（全局，置顶）**：对所有「运行」生效的三项：
  - `base_url`：覆盖被测系统地址（默认取 `config.yaml` 的 `framework.web.base_url`）。
  - `heal`：失败自愈开关（自动补 `index`、catalog 重命名 testid）。
  - `repeat`：重复执行次数，`>1` 时做 flaky 检测（结果不一致会提示环境依赖）。
- **异步运行**：点用例的「运行」→ 任务入队，状态 `queued → running → success/failed` 自动刷新；浏览器执行不阻塞页面。
- **结果展开**：点开一条运行记录，看生成的**代码 / 执行输出 / 质量门结论 / Healer 记录 / 元信息（耗时、repeat 稳定性）**。

### 4. 典型操作流程

1. **首次试用**：打开页面 → 点列表里已有的 `tc_purchase_001_raw2_fixed` → 「运行」（确保 `target_demo` 前端 http://localhost:8081 在线）→ 看结果变绿。
2. **接入自己的项目**：改 `config.yaml` 的 `framework.web.frontend_src` 指向你的前端源码 → 重启服务 → 「重载目录」→ 在「提交用例 JSON」里粘贴你的 case → 运行。
3. **生成新用例**：配好 `LLM_API_KEY` → 顶部输入一句话需求 → 「生成」→ 自动落库并跑质量门。
4. **调试失败用例**：运行失败 → 展开结果看「质量门」红字与「输出」报错 → 「编辑」里改 JSON（如给列表元素补 `"index": 0`）→ 重新运行；或开 `heal` 让工具自动修定位器。

### 5. 与底层 API 的对应关系

UI 的每个操作都对应一个 REST 接口（详见上方「阶段0 / 阶段1 接口表」）：

- 新建 / 编辑 / 删除 → `POST /api/cases`、`PUT /api/cases/{id}`、`DELETE /api/cases/{id}`
- 异步运行 → `POST /api/runs`（入队）→ `GET /api/runs/{id}`（轮询）
- 查看历史 → `GET /api/cases`、`GET /api/runs`
- 生成 / 重载目录 → `POST /api/generate`、`POST /api/reload-catalog`

### 6. 注意事项

- **单 worker 串行**：同时只跑一个任务，多任务排队；别开 `uvicorn --workers N`（多进程会重复消费任务）。
- **依赖被测系统在线**：真实跑用例需要 `base_url` 指向的系统可达；只查看用例 / 代码不依赖它。
- **数据落库未二次脱敏**：`data/platform.db` 会原样存 case_json（可能含明文密码），已在 `.gitignore` 中，请勿上传含真实生产凭据的用例或该库文件。
- **状态持久化**：运行记录、用例都存进 SQLite；worker 启动时自动恢复卡住的任务，重启服务不丢。

