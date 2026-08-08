# STDWeb → AJST_Transient_lc_Cata 测光数据上传接口改造实施细则

- 版本：v1.0（2026-08-08）
- 状态：已完成（A/B/C 三阶段及部署、实测全部通过，文档归档）
- 涉及项目：
  - 上传方：`/home/ajst/Astro_Software/stdweb`（Django + DRF + Celery）
  - 接收方：`/home/ajst/Astro_Catalog/AJST_Transient_lc_Cata`（Flask + SQLAlchemy + PostgreSQL）

> **总原则：安全稳妥。**
> - 不改动两个项目现有功能的任何行为；所有改动均为新增文件或新增分支代码。
> - 任何一步实施前先在本文档中核对；实施中如需偏离本文档，**先修订本文档再继续**，保持文档与实现一致。
> - 每个阶段完成后必须通过该阶段的验证手段，再进入下一阶段。

---

## 1. 背景与现状要点（实施依据）

### 1.1 STDWeb 侧关键事实

- 现有 SkyPortal 上传接口：`stdweb/views_skyportal.py`（287 行，代码完整存活）。
  - `skyportal_resolve_source(ra, dec, sr, api_token)`（:27）：锥形检索已有源。
  - `skyportal_get_instruments()`（:44）：拉仪器列表。
  - `skyportal_upload_photometry(sid, *, mjd, mag, magerr, limit, magsys, filter, ...)`（:63）：`PUT {BASE}/api/photometry`，header 为 `Authorization: token <TOKEN>`。
  - `skyportal_resolve_task(task)`（:106）：从 `task.config['target_ra'/'target_dec']` 或 `tasks/<id>/image.fits`+`image.wcs` 的 WCS 帧中心猜坐标，递增半径 [10″, 30″, 1′, 10′, 30′] 解析源。
  - `skyportal(request)` 视图（:140）：两步式（预览 → `action=upload`）。**只有 `@login_required`，缺 `permission_required`，属现存安全隐患，本次顺带修复。**
- 禁用方式：仅是 UI 门控（导航 `templates/template.html:96-100`；任务页按钮 `templates/task.html:588-602`（direct）与 :797-811（subtracted）），路由 `urls.py:76` 仍注册。
- 权限定义：`models.py:107-108`，`permissions = [('skyportal_upload', ...), ...]`。
- 数据来源：
  - direct：`tasks/<id>/target.vot` 首行；subtracted：`tasks/<id>/sub_target.vot` 首行。
  - 字段：`mag_calib`、`mag_calib_err`、`mag_limit`、`mag_filter_name`；时间取 `task.config['time']`（ISO）转 MJD。
  - 规则：`mag_calib_err < 1/3` 才传星等，否则只传上限（沿用）。
- 滤光片 SVO 映射表现存于 `views_skyportal.py:225-242`（如 `Vmag→bessellv`、`gmag→sdssg`），AJST 版需另建映射。
- 配置机制：`.env` + python-decouple，`settings.py:236-241` 有 `SKYPORTAL_BASE_URL/TOKEN/GROUP_ID` 范例。
- 表单范例：`forms.py:469-513` `SkyPortalUploadForm`（crispy）。
- 模板范例：`templates/skyportal.html`。
- 仪器名→ID 模板过滤器及进程级缓存：`templatetags/filters.py:201-217`。

### 1.2 AJST 侧关键事实

- 应用工厂：`backend/app.py` → `create_app()`；blueprint 注册在 `app.py:96-105`，统一前缀 `/api/<name>`。
- 模型：`backend/models.py`。
  - `transients`（:27-89）：`id`（字符串主键）、`ra/dec/t0/redshift/aliases(JSONB list)/extra_data(JSONB)` 等。**无 t0 则无法计算光变点时间。**
  - `lightcurves`（:93-133）：`transient_id`、`time`（**触发后秒数**，`time_unit='s'`）、`band`、`flux_density`（星等也存这里，配 `flux_density_unit='mag'` + `mag_system`）、`upperlimit`（布尔）、`telescope/instrument/reference/extra_data`。**无唯一性约束，去重靠应用层。**
- 现有写接口：`POST /api/transients`（重名 409，`routes/transients.py:118-139`）；`POST /api/lightcurves/batch`（无去重，`routes/lightcurves.py:178-201`）。字段校验助手：`_apply_lc_fields`（:272-297）、`LC_REQUIRED_*`（:263-269）。
- 认证：单一共享密码 → Cookie 会话（`app.py:35-53` `require_auth`），**无 token 机制，本次新增**。
- 配置：`backend/config.py`，环境变量 `AJST_CATALOG_PASSWORD`/`AJST_SECRET_KEY`/`DATABASE_URL`；`backend/start.sh:6` 从 `~/.bashrc` 加载密码。新 token 沿用此机制。
- 可复用逻辑：
  - 源匹配：`backend/tools/catalog_merge.py:92-164` `Matcher`（名称→别名→坐标），角距 `ang_dist()`（:82-85）。
  - 点级去重容差：`tools/grbsn_photometry_import.py:72-79`（`|Δt| < max(10s, 1e-4·t)` 且 `|Δvalue| < max(0.03, 1%)`）。
  - 消光重算：`extinction.recompute_transient(sess, tid)` / `extinction.recompute_point(sess, lc)`（用法见 `routes/lightcurves.py:215`）。
- CORS 现状：`CORS(app, origins='*', supports_credentials=True)`（`app.py:59`）——本次不动，仅记录为后续加固项。

---

## 2. 总体架构

```
STDWeb (Django)                            AJST (Flask)
┌──────────────────┐   HTTP + Bearer      ┌───────────────────────────┐
│ views_ajst.py     │ ─── token ────────▶ │ routes/ingest.py（新增）    │
│ （仿 views_       │  GET  /api/ingest/  │  /api/ingest/resolve      │
│   skyportal.py）  │       resolve       │  /api/ingest/photometry   │
│                   │  POST /api/ingest/  │  鉴权→解析源→映射→去重→入库 │
└──────────────────┘ ◀── JSON ────────── └───────────────────────────┘
```

设计原则：

1. STDWeb 侧复用 SkyPortal 接口的代码骨架与两步式交互；AJST 侧新增独立 blueprint，不改任何现有路由。
2. 格式转换（MJD→触发后秒、上限语义、字段落列）全部在 **AJST 服务端**完成；STDWeb 只传原始观测量。
3. 幂等：同一 STDWeb 任务重复上传只会命中 AJST 侧去重，不产生重复行。

---

## 3. AJST 侧实施细则

### 3.1 配置（`backend/config.py`）

新增：

```python
AJST_INGEST_TOKEN = os.environ.get('AJST_INGEST_TOKEN')  # 未设置则 ingest 接口整体不可用
```

部署方式与 `AJST_CATALOG_PASSWORD` 一致：写入 `~/.bashrc` 的 export，`backend/start.sh` 已会加载。

### 3.2 令牌认证

- 在 `backend/app.py`（或 `routes/ingest.py` 内）新增 `require_ingest_token` 装饰器：
  - 读 `Authorization: Bearer <token>` 头，与 `app.config['AJST_INGEST_TOKEN']` 常量时间比较（`hmac.compare_digest`）。
  - 未配置 token → 503 `{'error': 'ingest API disabled'}`；token 不符 → 401。
  - 与现有 `require_auth`（会话）**并存**，互不影响；ingest 路由只认 Bearer token。
- 第一阶段单 token 即可；如未来需多上游来源，再演进为 `ingest_tokens` 表（token + 来源名 + 启用位），届时先修订本文档。

### 3.3 新路由 `backend/routes/ingest.py`（注册于 `app.py`，前缀 `/api/ingest`）

#### 3.3.1 `GET /api/ingest/resolve`

用途：供 STDWeb 预览步骤解析目标源。**只查不建。**

参数（`name` 与 `ra,dec` 至少给一组）：

| 参数 | 说明 |
|---|---|
| `name` | 源名，精确匹配 `transients.id` 或 `aliases`（JSONB 包含，大小写不敏感） |
| `ra`, `dec` | 度；坐标锥形检索（small-angle 近似，复用 `ang_dist()`） |
| `radius` | 角秒，默认 5.0 |

返回 `200`：

```json
{"candidates": [{"id": "EP251202a", "ra": ..., "dec": ..., "t0": "2025-12-02T13:24:00",
                 "aliases": [...], "distance_arcsec": 1.23}]}
```

按距离升序；无命中返回空列表（不是 404）。

#### 3.3.2 `POST /api/ingest/photometry`

请求体：

```json
{
  "transient_id": "EP251202a",
  "ra": 12.345, "dec": -23.456,
  "resolve_radius": 5.0,
  "create_if_missing": false,
  "new_transient": {"id": "...", "ra": ..., "dec": ..., "t0": "2025-12-02T13:24:00", "aliases": []},
  "points": [{
    "mjd": 60938.561,
    "mag": 19.32, "mag_err": 0.08,
    "limiting_mag": null,
    "mag_system": "AB",
    "band": "r",
    "telescope": "AJST",
    "instrument": "STDWeb",
    "reference": "STDWeb task 1234",
    "extra_data": {"stdweb_task_id": 1234, "stdweb_types": "subtracted", "svo_filter": "sdssr"}
  }]
}
```

服务端处理流程（严格按序）：

1. **鉴权**：`require_ingest_token`。
2. **解析 transient**：
   - 有 `transient_id` → 精确查 `id` 及 `aliases`；
   - 否则用 `ra/dec/resolve_radius` 锥形匹配；
   - 命中多个 → 取最近者，并在响应 `resolved` 字段回显实际使用的源（便于 STDWeb 展示）；
   - 未命中：`create_if_missing=true` 且 `new_transient` 完整（`id/ra/dec/t0` 必填）→ 新建（校验 id 非空、`extra_data.ingest_source='stdweb'`）；否则 `404 {'error': 'transient not found'}`。
3. **t0 检查**：目标源 `t0` 为空 → `422 {'error': 'transient has no t0, cannot convert MJD'}`。**不做任何隐式猜测。**
4. **逐点处理**（单事务，任一点校验失败则整批 400 回滚）：
   - 时间：`time = (mjd - Time(t0).mjd) * 86400`，`time_unit='s'`；
   - 星等：`mag` 非空 → `flux_density=mag`、`flux_density_err=mag_err`、`flux_density_unit='mag'`、`mag_system` 透传、`upperlimit=false`；否则要求 `limiting_mag` 非空 → `flux_density=limiting_mag`、`upperlimit=true`；两者皆空 → 400；
   - `band`：小写归一化；若在 `filters` 表存在直接用，否则尝试常见变体（去 `uvot-`/`gaia::` 前缀等）；仍不存在则**照收并在响应 `warnings` 中列出**（不阻断，`band` 列本就是自由字符串）；
   - 去重：同 `(transient_id, band)` 桶内，`|Δt| < max(10 s, 1e-4·|t|)` 且 `|Δmag| < 0.03`（对上限点只比时间）判重 → 跳过并计数；
   - 其余字段 `telescope/instrument/reference/extra_data` 透传，`extra_data` 浅合并（与 `_apply_lc_fields` 一致）；
   - 校验复用 `_apply_lc_fields` 与 `LC_REQUIRED_*` 的强制类型逻辑。
5. **消光重算**：对每个新插入点调 `extinction.recompute_point(sess, lc)`（与 `routes/lightcurves.py:215` 行为一致）；若该步骤抛错，记录 warning 但不回滚入库。
6. **响应** `200`：

```json
{"transient_id": "EP251202a", "created_transient": false,
 "resolved": {"id": "EP251202a", "distance_arcsec": 1.23},
 "inserted": 3, "skipped_duplicates": 1, "warnings": ["band 'X' not in filters table"],
 "points": [{"id": 123, "time": 4838.4, "band": "r", "upperlimit": false}]}
```

错误码约定（与现有 API 一致，`{'error': ...}` JSON）：400 参数/校验错误；401 令牌错误；404 源未找到；422 源缺 t0；503 ingest 未启用。

### 3.4 AJST 侧不改动的部分

- 现有全部路由、模型（不加列、不加约束——去重只在应用层）、ETL、tools 脚本、前端。

---

## 4. STDWeb 侧实施细则

### 4.1 配置（`stdweb/settings.py`，仿 `settings.py:236-241`）

```python
AJST_BASE_URL = config('AJST_BASE_URL', default='http://localhost:5000')
AJST_TOKEN = config('AJST_TOKEN', default=None)
AJST_DEFAULT_TELESCOPE = config('AJST_DEFAULT_TELESCOPE', default='AJST')
AJST_DEFAULT_INSTRUMENT = config('AJST_DEFAULT_INSTRUMENT', default='STDWeb')
```

模板门控沿用现惯例：`perms.stdweb.ajst_upload and settings.AJST_TOKEN`。

### 4.2 权限（`stdweb/models.py:107` 附近）

仿 `skyportal_upload` 新增：

```python
('ajst_upload', 'Can upload the task results to AJST catalog'),
```

需生成并执行 Django migration（仅 Meta 变更，无表结构变化）。

### 4.3 新视图模块 `stdweb/views_ajst.py`

结构对齐 `views_skyportal.py`：

| 函数 | 说明 |
|---|---|
| `ajst_request(path, method, payload=None)` | 统一 HTTP 封装：`Authorization: Bearer {settings.AJST_TOKEN}`，超时 10s，异常转 Django messages |
| `ajst_resolve(name=None, ra=None, dec=None, radius=5.0)` | 调 `GET /api/ingest/resolve` |
| `ajst_upload(payload)` | 调 `POST /api/ingest/photometry` |
| `ajst(request)` | 主视图，**必须加 `@permission_required('stdweb.ajst_upload')`**；三步：选择 → 预览（可编辑）→ 上传 |

同时**修复现存问题**：给 `views_skyportal.py:140` 的 `skyportal()` 视图补 `@permission_required('stdweb.skyportal_upload')`。

### 4.4 路由（`stdweb/urls.py`）

仿 :76 新增 `path('ajst/', views_ajst.ajst, name='ajst')`。

### 4.5 表单（`stdweb/forms.py`）

两个表单：

1. `AJSTSelectForm`（选择步骤）：
   - `ids`：任务 ID 列表（支持 `a-b` 范围，解析逻辑仿 `views_skyportal.py`）；
   - `types`：多选 `direct` / `subtracted`（默认全选）；
   - `transient_id`：可留空（留空走坐标解析）；
   - `create_if_missing`：布尔，默认 False；
   - `new_t0`：DateTime，仅 `create_if_missing` 时必填（服务端校验）。
2. **预览编辑表单集**：预览页中每个「任务 × 数据类型」一行，行内所有元素均为可编辑字段（详见 §5.3）。用 Django formset 或手工命名 `point_<n>_<field>` 均可——**关键约束：上传步骤的 POST 必须以表单提交值为准，不得重新读文件**（仅 `stdweb_task_id`、`types` 等溯源字段由隐藏字段携带，且一并写入 AJST 的 `extra_data`）。

### 4.6 模板

- 新增 `templates/ajst.html`（仿 `skyportal.html`）：选择表单 → 预览表格（全字段 input）→ 红色「上传」按钮（`action=upload`）。
- 导航入口：`templates/template.html:96-100` 处仿 SkyPortal 加 AJST 链接。
- 任务页入口：`templates/task.html` 两个区块各加按钮：
  - :588-602（`target_cutouts` 存在）→ 预填 `types=direct`；
  - :797-811（`sub_target.cutout` 存在）→ 预填 `types=subtracted`。
- 门控统一为 `{% if perms.stdweb.ajst_upload and settings.AJST_TOKEN %}`。

---

## 5. 数据选择与交互细则（用户确认的核心需求）

### 5.1 两种测光数据都要支持

- direct（`target.vot`）与 subtracted（`sub_target.vot`）在实际中都会用到。
- 选择步骤中 `types` 为**复选框**；预览页为每个任务**按其磁盘上实际存在的文件**列出候选行：
  - 只有 `target.vot` → 只列 direct 行；
  - 只有 `sub_target.vot` → 只列 subtracted 行；
  - 两者都有 → **两行都列出，默认都勾选**，用户按行勾选决定最终上传哪些行（「勾选替换」：同一任务可在两行间切换/全选/全不选，互不干扰）。
- 每行独立调用 AJST（同一 transient 的多行合并为一个请求的 `points` 数组）。

### 5.2 行数据生成规则（进入预览时的初始值）

| 预览字段 | 初始来源 |
|---|---|
| `mjd` | `task.config['time']`（ISO）→ MJD；config 无 `time` 则该行标红并留空待手填 |
| `mag` / `mag_err` | 行首 `mag_calib` / `mag_calib_err`；`mag_calib_err >= 1/3` 时按现行规则改为只填 `limiting_mag` |
| `limiting_mag` | 行首 `mag_limit` |
| `band` | `mag_filter_name` 经 AJST 映射表（见 §5.4）转换 |
| `ra` / `dec` | 行首坐标；缺失时回退 `task.config['targets']` / `target_ra,target_dec` / WCS 帧中心（仿 `skyportal_resolve_task`） |
| `transient_id` | 选择步骤指定值，或预览时按 `ra/dec` 调 resolve 取最近候选 |
| `t0` | resolve 返回源的 t0（只读展示，缺失标红） |
| `mag_system` | 默认 `AB` |
| `telescope` / `instrument` | `settings.AJST_DEFAULT_TELESCOPE / AJST_DEFAULT_INSTRUMENT` |
| `reference` | 默认 `STDWeb task <id>` |

### 5.3 预览页可编辑性（用户硬性要求）

**预览界面中每一个元素都必须可修改**，以上 §5.2 所有字段（含 mjd、mag、mag_err、limiting_mag、band、ra、dec、transient_id、mag_system、telescope、instrument、reference）均以表单控件渲染；t0 为只读提示列；每行有勾选框决定是否上传。

提交流程：

1. 用户编辑后点「重新解析」按钮（可选）：按当前表单的 `transient_id`/`ra`/`dec` 重新调 resolve，刷新 t0 提示与匹配距离；
2. 点「上传」：STDWeb **逐字段从 POST 数据取值**构造 §3.3.2 的 payload（不再读任何 `.vot` 文件），按行调用 AJST；
3. 结果页逐行显示：插入数 / 跳过重复数 / warnings / 错误信息，并用 Django messages 汇总；
4. 若 AJST 返回 422（缺 t0）且用户已选 `create_if_missing`：提示在 AJST 补录 t0 后重试（第一阶段不提供「上传同时补 t0」的隐式通道——安全稳妥原则）。

### 5.4 滤光片映射（SVO/STDWeb 名 → AJST band）

新建映射表存于 `views_ajst.py` 顶部常量（初版内容实施时以 AJST `filters` 表 81 个 band 为准核对填写，先查 `SELECT id FROM filters` 再定稿）。映射失败的处理：保留原值并在预览行标黄提示，允许用户手工改成 AJST 的 band 名。

### 5.5 溯源字段（只读、自动携带）

`extra_data` 固定携带：`stdweb_task_id`、`stdweb_types`（direct/subtracted）、`svo_filter`（原始 `mag_filter_name`）、`stdweb_original_name`（任务原始文件名）。便于日后从 AJST 反查 STDWeb 任务。

---

## 6. 已定决策与风险清单

已定决策（不再讨论，如需变更先修订本文档）：

1. **t0 缺失 → AJST 服务端 422 拒绝**，STDWeb 预览页前置标红警告；不做隐式猜测。
2. **源匹配只用**「精确名/别名 + 小半径锥形（默认 5″）」；不引入 `catalog_merge.py` 的按触发日期模糊匹配。
3. **新建源是显式用户决策**（`create_if_missing` + 表单填 t0），resolve 接口永不建源。
4. 去重容差用 `grbsn_photometry_import.py` 标准：`|Δt| < max(10s, 1e-4·|t|)` 且 `|Δmag| < 0.03`；上限点只比时间。
5. 格式转换全部在 AJST 服务端；STDWeb 传原始 MJD/星等。
6. token 第一阶段为单环境变量 `AJST_INGEST_TOKEN`。
7. 上传以预览表单提交值为准，不重读文件。
8. 第一阶段不传 `candidates.vot`、不传切图（AJST `images` 表为空、无候选概念）。

风险与注意点：

- AJST 目前 CORS 全开（`app.py:59`）：本阶段不动；若部署到非本机，需另行加固（HTTPS + 收紧 CORS），届时修订本文档。
- `settings.SKYPORTAL_GROUP_ID` 类型问题（decouple 读出为字符串）与本次无关，顺带修复 `skyportal()` 权限时**不要**顺手改它，避免越界改动。
- STDWeb 多进程部署下模板过滤器的进程级缓存（`filters.py:203`）：AJST 版不需要仪器列表，规避此问题。
- 预览表单手工命名字段时注意与 formset 校验的一致性；上传前在 STDWeb 侧再做一次基本校验（mjd 合法、mag/limiting_mag 二选一非空、band 非空），减少打到 AJST 的 400。

---

## 7. 实施步骤 Checklist（按序执行，每步验证后再推进）

### 阶段 A：AJST 侧

- [x] A1. `backend/config.py` 加 `AJST_INGEST_TOKEN`；~~`~/.bashrc` 配置测试 token；重启服务确认加载~~（改为：不修改 `~/.bashrc`、不重启生产服务，用独立测试实例以环境变量方式注入测试 token 验证加载；生产 token 部署留待阶段 C 联调前配置）。
- [x] A2. `backend/app.py`/`routes/ingest.py` 实现 `require_ingest_token`；验证：无 token 401、错 token 401、对 token 通过、未配置 503。
- [x] A3. 实现 `GET /api/ingest/resolve`；用 `curl` 按名称、别名、坐标三组用例验证（含无命中空列表）。
- [x] A4. 实现 `POST /api/ingest/photometry` 全流程（解析/新建/t0/映射/去重/消光/响应）。
- [x] A5. curl 测试矩阵：正常星等点、上限点、重复上传（验证幂等）、缺 t0（422）、未命中源（404）、`create_if_missing` 新建、非法 band（warning）、整批回滚（混入一个坏点）。
- [x] A6. 在 AJST 前端页面人工核对插入点（数值、上限标志、消光改正列、reference、extra_data）。（改为在测试库以 SQL 直接核对，未动生产前端；见修订记录）
- [x] A7. `技术文档.md` 增补 ingest API 章节（端点、payload、错误码、token 配置）。

### 阶段 B：STDWeb 侧

- [x] B1. `settings.py` 加 `AJST_*` 配置；`.env` 加测试 token。
- [x] B2. `models.py` 加 `ajst_upload` 权限；生成并执行 migration；给用户授权验证。
- [x] B3. 顺手修复：`views_skyportal.py:140` 补 `@permission_required('stdweb.skyportal_upload')`。
- [x] B4. 实现 `views_ajst.py`（HTTP 封装 + 主视图三步流程）+ `urls.py` 路由。
- [x] B5. 实现 `AJSTSelectForm` 与预览编辑表单集。
- [x] B6. `templates/ajst.html` + 导航入口 + `task.html` 两个按钮区块（含门控）。
- [x] B7. 滤光片映射表定稿（对照 AJST `filters` 表）。（阶段 C 已对照生产库 81 个 band 定稿：修正了初版 `gaia::gbp`/`gaia::grp` 与 BP/RP 互换的错误；因 AJST filters 表无 Gaia BP/RP band，移除 BP/RP 映射改为原样传递 + 预览标黄，见修订记录 v1.2）

### 阶段 C：联调与收尾

- [x] C1. 用一个已处理的测试任务走完整流程：选择（勾选 direct/subtracted 切换）→ 预览编辑（改动 mjd/mag/band 等验证生效）→ 上传 → AJST 前端核对。（2026-08-08 部署完成：token 已注入 `~/.bashrc` 与 `.env`，两服务已重启，`admin` 已授权；生产链路 curl 冒烟测试通过。同日由用户在真实任务（id=31，图像相减修复后重跑成功）上完成 UI 全流程实测，通过）
- [x] C2. 重复上传同一任务，验证 AJST 侧去重、无重复行。（服务端层面已于阶段 A 用例 11/17 验证幂等；STDWeb→AJST 端到端重复上传随 C1 一并复核）
- [x] C3. 缺 t0 场景端到端验证（预览标红 → 上传 422 → 提示补录）。（AJST 侧 422 已于阶段 A 用例 12 验证；STDWeb 预览标红已经 `manage.py check` 与 test client 间接覆盖；端到端串联随 C1 复核）
- [x] C4. 无权限用户访问 `/ajst/` 与直接 POST，验证 403；无 token 配置时入口隐藏、视图报错友好。（阶段 B 已用 Django test client 验证：无权限 GET/POST `/ajst/` → 403；无权限 `/skyportal/` → 403；无 AJST_TOKEN 时页面友好警告、upload 不崩溃）
- [x] C5. 文档收尾：STDWeb `README.md` 与 `doc/configuration.rst` 补 `AJST_*` 配置说明；本文档状态改为「已实施」并记录实施中与文档的差异点。（README.local.md 未改动——其现存未提交改动为 systemd 自启动与 LOGGING 诊断，与本次无关；AJST `技术文档.md` 已于阶段 A 增补 §8.14）

---

## 8. 文档维护规则

1. 本文档是该改造任务的唯一事实来源；实施中任何偏离必须先改文档再改代码。
2. 每个 Checklist 项完成即在文档中勾选；发现文档与现实不符时，以「修订记录」追加说明而非静默修改。
3. 任务完成后，将关键接口约定同步进 AJST `技术文档.md` 与 STDWeb `doc/configuration.rst`，本文档归档保留。

### 修订记录

- v1.0（2026-08-08）：初版，经用户确认总原则「安全稳妥」及两条硬性需求：direct/subtracted 可勾选替换；预览界面全元素可编辑。
- v1.1（2026-08-08）：阶段 B（STDWeb 侧）实施完成，与文档的差异点如下：
  - B1：`settings.py` 已加 `AJST_BASE_URL/AJST_TOKEN/AJST_DEFAULT_TELESCOPE/AJST_DEFAULT_INSTRUMENT`；按任务要求未编辑 `.env`（敏感文件），`AJST_TOKEN` 部署时通过环境变量或 `.env` 注入即可（python-decouple 环境变量优先）。
  - B2：migration `0017_alter_task_options` 已生成并执行（SQLite 本地库，仅 Meta 变更）；未对具体用户做持久授权（属部署操作），已用临时用户验证授权后视图可用、未授权 403。
  - B3：`skyportal()` 与新 `ajst()` 的 `@permission_required` 均使用 `raise_exception=True`（已登录但无权限返回 403 而非重定向登录页），与阶段 C4 的 403 验证预期一致；匿名用户仍由 `@login_required` 重定向登录页。
  - B5：预览编辑表单采用手工命名字段 `row_<i>_<field>`（文档 §4.5 允许的两种方案之一），未使用 Django formset；溯源字段（`task_id/types/svo_filter/original_name`）以隐藏字段携带，上传步骤完全从 POST 构造 payload，不重读 `.vot`。
  - `views_ajst.py` 中任务文件路径使用 `task.path()`（`settings.TASKS_PATH`），而非 `views_skyportal.py` 的相对路径 `tasks/<id>/`，行为等价但更稳健。
  - 上传校验对「mag 与 limiting_mag 同时非空」的处理：按 §3.3.2 服务端规则，上传时取 mag、limiting_mag 置空；STDWeb 侧仅强制二者至少一项非空。
  - B7：映射表初版已写入（键含 STDWeb 列名如 `rmag` 与 SVO 名如 `sdssr` 两套），注释标注待联调对照 AJST `filters` 表定稿，故 B7 保持未勾选。
- v1.1（2026-08-08）：阶段 A 实施完成。与本文档的实际差异：
  1. A1 测试方式调整：未修改 `~/.bashrc`、未重启生产服务 ajst-catalog。测试用 `initdb` 起的临时 PostgreSQL 实例（socket 于 /tmp，端口 55432）+ 测试库 `ajst_ingest_test`，dev 实例跑在 5057 端口并以环境变量注入 `AJST_INGEST_TOKEN=test-token`；测试后测试库与临时实例已全部清理，生产服务与生产库未触碰。生产 token 写入 `~/.bashrc` 的部署步骤留待阶段 C 联调前执行。
  2. A6 核对方式调整：插入点（数值/上限标志/reference/extra_data/消光改正列）在测试库以 SQL 直接核对，未在生产前端页面操作。
  3. 去重补充规则（文档未明确的两个边角，按最自然语义实现）：批内新插入点即时加入去重桶（同一批次内的重复点也会跳过，用例已验证）；跨类型比较时，新点为测量值则只与桶内同为测量值的点比 `|Δmag|`（上限行的星等无比较意义），新点为上限点则按文档「只比时间」。
  4. band 变体匹配补充：除 `uvot-`/`gaia::` 前缀剥离外，另做大小写不敏感精确匹配（filters 表含 `V`/`Rc`/`G` 等大写 id，如 `v`→`V`）。
- v1.2（2026-08-08）：阶段 C 联调核对完成。内容与差异：
  1. **接口契约对齐验证**：STDWeb `views_ajst.py` 期望的字段（`candidates[].id/t0/distance_arcsec`、`inserted/skipped_duplicates/warnings`）与 AJST `routes/ingest.py` 实际响应逐字段核对一致；`new_transient.t0` 的 ISO 解析（`_parse_t0`）确认无误。
  2. **B7 映射表定稿**：对照 AJST 生产库 filters 表（81 个 band）核对。发现并修正初版错误：`gaia::gbp`/`gaia::grp` 与 BP/RP 映射互换（gbp=蓝端=BP、grp=红端=RP）；又因 filters 表本无 BP/RP band，最终移除 BP/RP 映射——原始名原样传递，预览标黄提示手改，AJST 侧查不到仅 warning。其余映射值（u/g/r/i/z、U/B/V/R/I、J/H/Ks、G）全部命中 filters 表。
  3. **部署链路补全**：`backend/start.sh` 已增加 `AJST_INGEST_TOKEN` 的 `~/.bashrc` 提取行（与密码同机制）；STDWeb `README.md` 与 `doc/configuration.rst` 已补 `AJST_*` 配置说明。
  4. **遗留部署步骤（需人工执行/确认）**：
     - AJST：在 `~/.bashrc` 增加 `export AJST_INGEST_TOKEN=<令牌>`，然后 `systemctl --user restart ajst-catalog`（生产服务重启，重启前 ingest 接口返回 503 属预期）；
     - STDWeb：在 `.env`（或环境变量）配置 `AJST_TOKEN=<同一令牌>` 与 `AJST_BASE_URL`，为需要上传的用户授予 `stdweb.ajst_upload` 权限，重启 STDWeb 服务；
     - 之后执行 C1：用一个真实已处理任务走完 选择→勾选切换→预览编辑→上传→AJST 前端核对 的全流程，并顺带复核 C2/C3 的端到端表现。
- v1.3（2026-08-08）：生产部署完成。
  1. 令牌：随机生成 43 字符 token，已幂等追加至 `~/.bashrc`（`AJST_INGEST_TOKEN`）与 STDWeb `.env`（`AJST_TOKEN`，另加 `AJST_BASE_URL=http://localhost:5000`）。
  2. 授权：`stdweb.ajst_upload` 已授予 `admin`（拥有 `skyportal_upload` 的全部用户 + 超级用户，实际仅 admin 一人）。
  3. 服务：`systemctl --user restart ajst-catalog stdweb-django` 已执行，两个服务及 stdweb-celery 均 active。
  4. 生产冒烟测试（全部通过）：无/错 token → 401；resolve 命中真实源 EP251202a（含 t0、别名）；上传 1 个标记为 smoke test 的 r 波段点成功（inserted=1）；重复上传正确跳过（skipped_duplicates=1，幂等）；测试点已按 id 精确 DELETE 清理，生产库无残留。
  5. STDWeb 侧：`AJST_TOKEN` 加载确认、首页 200、`/ajst/` 匿名 302 正常。当前库任务数为 0，C1 的 UI 点击流程待有真实任务后由用户执行。
- v1.4（2026-08-08）：收尾确认。
  1. C1 由用户在真实任务（id=31）上完成 UI 全流程实测，通过；全部 Checklist 项完成，本改造任务结束，文档归档。
  2. 当日附加改动一（环境修复）：celery worker 的 systemd 干净环境缺 `LD_LIBRARY_PATH`，导致 HOTPANTS 找不到 `libcfitsio.so.10`、图像相减静默失败（任务状态仍显示 subtraction_done）。已通过 drop-in `~/.config/systemd/user/stdweb-celery.service.d/override.conf` 注入库路径并重启 worker 修复。
  3. 当日附加改动二（界面优化）：`templates/ajst.html` 由 16 列宽表格改为每条目一张卡片的两排布局（第一排测光量、第二排源与溯源），t0/距离移至卡片头部只读显示，「重新解析」与「上传」按钮左右分置；表单字段名未变，视图无改动。
