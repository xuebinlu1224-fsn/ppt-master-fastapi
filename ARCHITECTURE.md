# ppt-master-agent 项目架构梳理

> 面向 `ppt-master` 的外层编排服务。**外层不重复实现 PPT 能力**,只负责"任务管理 + DeepSeek 规划 + HTTP API + UI",所有真正的生成链路都委托给 vendored 的 `ppt-master/` 子模块脚本。

---

## 1. 项目整体定位

| 角色 | 职责 | 边界 |
|---|---|---|
| **外层服务(ppt-master-agent)** | FastAPI 编排 + DeepSeek 规划 + 任务账本 + 静态 UI | **不**自己写生图/导出/SVG 生成,只做"调用 + 记账 + 编排" |
| **`ppt-master/`(vendored)** | 真正的 PPT 能力:项目管理 / Strategist 提示词 / Executor SVG 模板 / image_gen / total_md_split / finalize_svg / svg_to_pptx | **只读** upstream 依赖,自身有独立 `.git`、AGENTS.md、CLAUDE.md、SKILL.md |
| **DeepSeek(外部 LLM)** | 充当 Strategist(产出 `design_spec.md` + `spec_lock.md`)、单页 SVG 设计师、编排规划师 | 仅被外层 `app.py` 通过 OpenAI SDK 调用 |
| **Image Backend(外部)** | Agnes / OpenAI / Gemini 等;真正出图 | 由 `ppt-master` 内部脚本(`image_gen.py`)调用,key 不经过外层 |

### 1.1 一句话价值流
**用户 → UI/API → DeepSeek 规划 → 调用 `ppt-master` 脚本 → 写盘 → DeepSeek 改稿 → 再次调用 `ppt-master` 脚本 → 产物回写 UI**

---

## 2. 功能架构图

```
┌───────────────────────────────────────────────────────────────────────┐
│                              用户层                                     │
│   ┌──────────────────────┐                ┌──────────────────────┐    │
│   │  ui/index.html        │  HTTP/JSON     │  curl / 上层 agent    │    │
│   │  (静态控制台,深/浅主题) │  ───────────►  │                      │    │
│   │  - 任务面板           │  ◄───────────  │                      │    │
│   │  - 8 项确认表单       │                │                      │    │
│   │  - 历史记录/产物查看   │                │                      │    │
│   └──────────────────────┘                └──────────────────────┘    │
└─────────────────────────┬─────────────────────────────────────────────┘
                          │ fetch /tasks/*
┌─────────────────────────▼─────────────────────────────────────────────┐
│                     外层编排服务(FastAPI · app.py)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  │
│  │ 路由层       │  │ 任务账本     │  │ Python 解析 │  │ DeepSeek  │  │
│  │ - /tasks/*   │  │ .service_    │  │ resolve_    │  │ 客户端    │  │
│  │ - /health    │  │ tasks/<id>/  │  │ python_bin  │  │ OpenAI SDK│  │
│  │ - /          │  │ prompt/      │  │             │  │           │  │
│  │              │  │ metadata/    │  │ path 越权   │  │ 3 个角色: │  │
│  │ Pydantic     │  │ run.log/     │  │ 检查        │  │ 规划师   │  │
│  │ Schema 校验  │  │ last_run/    │  │             │  │ 策略师   │  │
│  │              │  │ plan/        │  │             │  │ 单页SVG  │  │
│  │              │  │ confirmation │  │             │  │ 设计师   │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  └─────┬─────┘  │
│         │                  │                 │                │        │
│         └──────────────────┴────────┬────────┴────────────────┘        │
│                                    │                                  │
│                  ┌─────────────────▼──────────────────┐               │
│                  │  subprocess.run 编排器             │               │
│                  │  - PYTHONUNBUFFERED=1               │               │
│                  │  - cwd = <repo_dir>                │               │
│                  │  - 追加 run.log(stdout+stderr+rc)  │               │
│                  └─────────────────┬──────────────────┘               │
└────────────────────────────────────┼──────────────────────────────────┘
                                     │ python3 <script> ...
┌────────────────────────────────────▼──────────────────────────────────┐
│            ppt-master/(vendored,read-only)                            │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  ┌────────────┐  │
│  │ 项目管理      │  │ 生图         │  │ SVG 处理    │  │ 导出       │  │
│  │ project_     │  │ image_gen.py │  │ total_md_   │  │ svg_to_    │  │
│  │ manager.py   │  │  - single    │  │ split.py    │  │ pptx.py    │  │
│  │  - init      │  │  - manifest  │  │ finalize_   │  │            │  │
│  │  - validate  │  │  - render-md │  │ svg.py      │  │ (DrawingML │  │
│  │  - import    │  │              │  │ svg_quality_│  │  原生形状) │  │
│  │  -sources    │  │  后端:Agnes/ │  │ checker.py  │  │            │  │
│  │              │  │ OpenAI/      │  │             │  │            │  │
│  │              │  │ Gemini       │  │             │  │            │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬─────┘  └─────┬──────┘  │
│         │                  │                 │               │         │
│         └──────────────────┴────────┬────────┴───────────────┘         │
│                                    │                                  │
│                  ┌─────────────────▼──────────────────┐               │
│                  │ projects/<task_id>/                │               │
│                  │  sources/  images/  svg_output/    │               │
│                  │  design_spec.md  spec_lock.md      │               │
│                  │  notes/  exports/  backup/         │               │
│                  └─────────────────────────────────────┘               │
└───────────────────────────────────────────────────────────────────────┘
                                     │
┌────────────────────────────────────▼──────────────────────────────────┐
│                外部依赖(运行时注入)                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐                  │
│  │ DeepSeek API │  │ Image        │  │ Python 3.10+│                  │
│  │ DEEPSEEK_*   │  │ Backend      │  │ (PEP 604    │                  │
│  │ (根 .env)    │  │ AGNES_/OPENAI│  │  语法依赖)  │                  │
│  │              │  │ _/GEMINI_*   │  │             │                  │
│  │              │  │ (ppt-master  │  │             │                  │
│  │              │  │  进程环境)   │  │             │                  │
│  └──────────────┘  └──────────────┘  └─────────────┘                  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. 逻辑架构(分层)

### 3.1 横向分层

| 层级 | 组件 | 关注点 |
|---|---|---|
| **L1 表现层** | `ui/index.html`(纯静态 SPA) | 任务面板 / 8 项确认表单 / 历史 / 文件查看,所有调用走 `fetch` |
| **L2 接口层** | FastAPI 路由 + Pydantic Schema (`app.py:28-120`) | 入参校验、错误码统一、HTTP/JSON 序列化 |
| **L3 编排层** | `prepare_task` / `agent_plan` / `strategist` / `generate_image` / `generate_svgs` / `export` 路由函数 | 业务流:验权 → 解析 → 调脚本 → 记账 → 落盘 |
| **L4 子进程桥** | `run_logged_command` / `resolve_python_bin` / `script_path` / `resolve_repo_path` | 把 `ppt-master` 的 CLI 包装成有 `run.log` 可审计的过程 |
| **L5 LLM 调用层** | `call_deepseek_plan` / `call_deepseek_strategist` / `call_deepseek_svg_page` | 3 个 prompt 角色:规划师 / 策略师 / 单页 SVG 设计师 |
| **L6 任务账本** | `.service_tasks/<task_id>/` 下的 6 个文件 | 任务的真相源,UI 全靠它回显 |
| **L7 真正的 PPT 能力** | `ppt-master/skills/ppt-master/scripts/*.py` | 上游"上游"的所有脚本,外层只调用不修改 |
| **L8 外部服务** | DeepSeek、Image Backend、Python 3.10+ | 通过 `.env` / `agent.env` 注入 |

### 3.2 数据落盘结构

```
ppt-master-agent/
├── .service_tasks/                ← L6:外层任务账本(命名卷 agent-state)
│   └── <task_id>/
│       ├── task_prompt.txt        ← 用户原始 prompt + 项目上下文
│       ├── task_metadata.json     ← task_id / repo_dir / project_dir / canvas_format
│       ├── run.log                ← 所有子进程 stdout+stderr+rc(append-only)
│       ├── last_run.json          ← 最近一次步骤结果
│       ├── agent_plan.json        ← DeepSeek 规划结果
│       └── confirmation_data.json ← 8 项确认 payload
│
├── ppt-master/                    ← L7:vendored,只读
│   └── projects/
│       └── <task_id>/             ← 由 project_manager.py init 创建
│           ├── sources/           ← 原始素材(PDF/DOCX/URL/MD)
│           ├── images/            ← 采集到的位图(用户/AI/web)
│           ├── design_spec.md     ← Strategist 产出
│           ├── spec_lock.md       ← Executor 执行契约
│           ├── svg_output/        ← 每页 SVG(P01_*.svg … PNN_*.svg)
│           ├── svg_final/         ← finalize_svg 处理后
│           ├── notes/             ← 演讲者备注
│           ├── exports/           ← 最终 .pptx
│           └── backup/            ← 每次导出的 SVG 快照
│
└── jobs/                          ← 手工重新导出/脱机工作,不属于服务产物
```

---

## 4. 端到端协作流程(与 `ppt-master` 协作)

### 4.1 完整 5 阶段流水线

```
        阶段                       外层做了什么                  调了谁/哪个脚本
┌──────────────┐
│ Stage 0      │  POST /tasks/prepare
│ 建项目       │  - 生成 task_id(=时间戳_6位hex)
│              │  - 写 task_prompt.txt + task_metadata.json
│              │  - 调 project_manager.py init <id> --format ppt169
│              │  - 解析 "Project created: ..." 拿到 project_dir
│
│              │  → ppt-master/projects/<id>/ 诞生
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Stage 1      │  POST /tasks/{id}/agent-plan(可选,但推荐)
│ 编排规划     │  - 读 project_state(design_spec? svg? …)
│              │  - DeepSeek(角色:规划师)
│              │    给出 current_stage / blocking_gaps /
│              │    recommended_next_actions(指向本服务的端点)
│              │  - 写 agent_plan.json
│
│              │  → 用户按 plan 决定下一步走哪个端点
└──────┬───────┘
       │
       ├────────────── (走 strategist 分支) ──────────────┐
       ▼                                                ▼
┌──────────────┐                                ┌──────────────┐
│ Stage 2A     │  POST /tasks/{id}/strategist   │ Stage 2A'    │
│ 策略师       │  - 读 task_prompt + canvas     │ 用户手工      │
│ 产出 spec    │  - DeepSeek(角色:策略师)        │ 在 UI 填 8 项 │
│              │    同时产出 design_spec.md      │ POST /confir- │
│              │    和 spec_lock.md             │ mation        │
│              │  - 解析后直接写盘到 project_dir │  落盘 conf-   │
│              │                                │ irmation_data │
│              │  → design_spec.md/spec_lock.md │  .json        │
│              │    进入 ppt-master 项目        │               │
└──────┬───────┘                                └──────┬───────┘
       │                                                │
       └────────────────────┬───────────────────────────┘
                            ▼
                  ┌──────────────────┐
                  │  此时项目"就绪":   │
                  │  sources + spec  │
                  │  + 设计 + 备注     │
                  └────────┬─────────┘
                           ▼
┌──────────────┐
│ Stage 2B     │  POST /tasks/{id}/generate-image(0..N 次)
│ 采集位图     │  - 两种模式:
│              │    a) 单图: prompt + aspect_ratio + filename
│              │    b) 清单: manifest_path → image_gen.py --manifest
│              │  - 走 image_gen.py(用 ppt-master/.env 的 AGNES 等)
│              │  - 前后文件快照对比,得到 new_files
│              │  - 调 image_gen.py --render-md 生成 sidecar
│
│              │  → images/<name>.{png,jpg} 进项目
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Stage 2C     │  POST /tasks/{id}/generate-svgs
│ 逐页出 SVG   │  - 解析 spec_lock.md 的 page_rhythm/page_layouts/page_charts
│              │  - 逐页循环:DeepSeek(角色:单页 SVG 设计师)
│              │    输入: spec_lock + design_spec + page_meta
│              │    输出: 干净的 <svg viewBox=…>...</svg>
│              │  - validate_minimal_svg(校验 svg 标签 + viewBox)
│              │  - 失败自动重试 1 次;再失败计入 failed 列表
│              │  - 写盘 svg_output/<pid>_<rhythm>.svg
│              │  - 全部完成后调 svg_quality_checker.py      (ppt-master 脚本)
│
│              │  → svg_output/ 下有 N 个 SVG
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Stage 3      │  POST /tasks/{id}/export(就绪门禁)
│ 导出 PPTX    │  - readiness = design_spec ✓ spec_lock ✓ svg_output>0
│              │  - 顺序调 3 个 ppt-master 脚本(严格串行):
│              │    1) total_md_split.py   拆演讲者备注
│              │    2) finalize_svg.py     SVG 后处理(图嵌入/文字扁平)
│              │    3) svg_to_pptx.py      导出原生 PPTX(DrawingML)
│              │  - 任一非零退出→HTTP 500 + 把 stderr 带回
│              │    (total_md_split 失败非阻塞)
│              │  - 成功→ 列 new_exports 路径
│
│              │  → exports/<id>_<ts>.pptx
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Stage 4      │  GET /tasks/{id}/artifacts
│ 查产物       │  GET /tasks/{id}/files/{prompt|metadata|run_log|...}
│              │  - 任何时候可拉 run.log 调试
└──────────────┘
```

### 4.2 一次完整往返(ASCII 时序)

```
User            ui/index.html        app.py (FastAPI)        DeepSeek        ppt-master 脚本       文件系统
 │                   │                     │                    │                │                   │
 │ 点"创建任务"      │                     │                    │                │                   │
 ├──────────────────►│  POST /tasks/prepare                      │                │                   │
 │                   ├────────────────────►│                    │                │                   │
 │                   │                     │ subprocess.run    │                │                   │
 │                   │                     ├───────────────────►│  project_manager.py init ────────► projects/<id>/
 │                   │                     │                    │                │ ◄── stdout: "Project created: …"
 │                   │                     │ 写 task_metadata   │                │                   │
 │                   │  TaskStatusResponse │                    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ 点"获取规划"      │                     │                    │                │                   │
 │                   │  POST /…/agent-plan│                    │                │                   │
 │                   ├────────────────────►│  plan prompt       │                │                   │
 │                   │                     ├───────────────────►│ JSON plan      │                │
 │                   │                     │◄───────────────────┤                │                   │
 │                   │  {blocking_gaps,    │  写 agent_plan.json│                │                   │
 │                   │   next_actions}     │                    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ 点"策略师"        │                     │                    │                │                   │
 │                   │  POST /…/strategist│                    │                │                   │
 │                   ├────────────────────►│  2-文件 spec prompt│                │                   │
 │                   │                     ├───────────────────►│ {design_spec,  │                │
 │                   │                     │                    │  spec_lock}    │                │
 │                   │                     │◄───────────────────┤                │                   │
 │                   │                     │ 写 design_spec.md ─┼────────────────┼───────────────────► projects/<id>/design_spec.md
 │                   │                     │ 写 spec_lock.md    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ (如要 AI 图)      │                     │                    │                │                   │
 │                   │  POST /…/generate-image  (manifest)       │                │                   │
 │                   ├────────────────────►│  subprocess.run    │                │                   │
 │                   │                     ├───────────────────►│  image_gen.py --manifest ────────► images/cover.png
 │                   │                     │  render-md         │                │                   │
 │                   │                     ├───────────────────►│  image_gen.py --render-md ─────► image_prompts.md
 │                   │                     │  前后文件 diff      │                │                   │
 │                   │  {new_files:[…]}    │                    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ 点"出 SVG"        │                     │                    │                │                   │
 │                   │  POST /…/generate-svgs                    │                │                   │
 │                   ├────────────────────►│  解析 spec_lock    │                │                   │
 │                   │                     │  for page in pages:│                │                   │
 │                   │                     │    DeepSeek per-page (循环)         │                   │
 │                   │                     ├──── page1 ────────►│ <svg>…</svg>   │                   │
 │                   │                     │◄───────────────────┤                │                   │
 │                   │                     │  写 svg_output/P01_cover.svg       │                   │
 │                   │                     │  … (逐页)          │                │                   │
 │                   │                     │  svg_quality_checker.py ──────────►│ check              │
 │                   │                     │                    │                │                   │
 │                   │  {pages_generated,  │                    │                │                   │
 │                   │   quality_report}   │                    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ 点"导出"          │                     │                    │                │                   │
 │                   │  POST /…/export    │                    │                │                   │
 │                   ├────────────────────►│  readiness 校验    │                │                   │
 │                   │                     │  total_md_split ──►│ ──────────────►│ notes/total.md    │
 │                   │                     │  finalize_svg ────►│ ──────────────►│ svg_final/         │
 │                   │                     │  svg_to_pptx ─────►│ ──────────────►│ exports/<id>.pptx │
 │                   │  {new_exports}      │                    │                │                   │
 │                   │◄────────────────────┤                    │                │                   │
 │ 点"看产物"        │                     │                    │                │                   │
 │                   │  GET /…/artifacts   │                    │                │                   │
 │                   │  GET /…/files/run_log 拉日志              │                │                   │
 │                   │                     │                    │                │                   │
```

---

## 5. 调用的能力清单

### 5.1 外层自研能力(`app.py` 内部)

| 能力 | 实现位置 | 作用 |
|---|---|---|
| **任务账本** | `task_paths()` + 6 个文件 | 跨请求持久化任务状态 |
| **子进程编排** | `run_logged_command()` | 统一 `PYTHONUNBUFFERED=1`、cwd=repo_dir、追加 `run.log` |
| **Python 解释器解析** | `resolve_python_bin()` | 6 级回退(`.venv`→系统→`PPTMASTER_PYTHON_BIN`) |
| **路径越权防御** | `ensure_repo_dir()` + `resolve_repo_path()` | 拒绝指向非 `ppt-master` 根的 `repo_dir`;禁止 manifest/output 跳出 repo |
| **就绪门禁** | `build_export_readiness()` | 导出前必须 `design_spec.md` + `spec_lock.md` + ≥1 个 SVG 齐备 |
| **三大 LLM 角色** | `call_deepseek_*` | 规划师 / 策略师 / 单页 SVG 设计师 |
| **Pydantic 入参校验** | `PrepareTaskRequest` / `GenerateImageRequest` 等 | `model_validator` 强制 `prompt` 与 `manifest_path` 互斥 |
| **静态 UI 服务** | `GET /` → `FileResponse(ui/index.html)` | 同一容器同时托管 API 和控制台 |

### 5.2 调用的 `ppt-master` 脚本

| ppt-master 脚本 | 通过哪个端点 | 用途 |
|---|---|---|
| `project_manager.py init` | `POST /tasks/prepare` | 在 `projects/<id>/` 下建标准化目录结构 |
| `image_gen.py` (单图) | `POST /generate-image` (`prompt` 模式) | 单张一次性出图,支持 aspect_ratio/image_size/backend |
| `image_gen.py --manifest` | `POST /generate-image` (`manifest_path` 模式) | 按 `image_prompts.json` 批量生图,带 manifest 审计 |
| `image_gen.py --render-md` | 同上(自动跟跑) | 渲染 `image_prompts.md` sidecar |
| `svg_quality_checker.py` | `POST /generate-svgs` 末段 | 0 错误门禁后再放行进导出 |
| `total_md_split.py` | `POST /export` 第 1 步 | 演讲者备注按页拆 `notes/total.md` |
| `finalize_svg.py` | `POST /export` 第 2 步 | SVG 后处理:图标嵌入 / 图片裁剪嵌入 / 文字扁平化 / 圆角矩形→path |
| `svg_to_pptx.py` | `POST /export` 第 3 步 | `svg_output/` → 原生 PPTX(可保留 `<use>` 占位、`preserveAspectRatio`→`srcRect`、`rx/ry`→`prstGeom roundRect`) |

### 5.3 调用的 `ppt-master` 模板(只读)

| 模板 | 在 `SKILL.md` 哪一步用 | 外层是否真正消费 |
|---|---|---|
| `templates/design_spec_reference.md` | Step 4 Strategist | **是** — 外层 `/strategist` 在 prompt 里显式要求 11 段结构对齐它 |
| `templates/spec_lock_reference.md` | Step 4 Strategist | **是** — 外层 prompt 强制 section 顺序和字段 |
| `references/strategist.md` | Step 4 | 否 — 由 DeepSeek 在其内部"知晓"即可 |
| `references/executor-base.md` | Step 6 | 否 — 由 SVG 设计师 prompt 内化 |
| `references/modes/<mode>.md` `references/visual-styles/<style>.md` | Step 6 | 否 — 由 SVG 设计师 prompt 内化(spec_lock 已锁) |
| `references/shared-standards.md` | Step 6/7 | 否 — 外层在 prompt 里把禁项(foreignObject、外部字体、滤镜、动画、`<script>`、`<use>`)直接复述 |
| `references/image-generator.md` | Step 5 | 否 — `image_gen.py` 自身按这文档读 manifest |
| `templates/brands/`、`templates/layouts/`、`templates/decks/`、`templates/icons/` | Step 3 | 否 — 外层目前不做模板融合 |

### 5.4 调用的 `ppt-master` workflow(显式未触发)

外层当前**不**路由这些 workflow:`topic-research`、`template-fill-pptx`、`beautify-pptx`、`create-template`、`create-brand`、`resume-execute`、`refine-spec`、`verify-charts`、`customize-animations`、`live-preview`、`visual-review`、`generate-audio`。它们都在 `ppt-master/skills/ppt-master/workflows/`,留给上层 agent 或用户在原生 SKILL.md 流程里触发。

---

## 6. 关键架构约束(踩过的坑)

### 6.1 双 `.env` 规则(最容易踩)

```
项目根 .env / agent.env            ppt-master/.env(或进程环境)
─────────────────────             ───────────────────────────
DEEPSEEK_API_KEY    ← app.py 读    IMAGE_BACKEND=agnes
DEEPSEEK_BASE_URL                  AGNES_API_KEY
DEEPSEEK_MODEL                     AGNES_BASE_URL
PPTMASTER_PYTHON_BIN               AGNES_MODEL
                                    (OpenAI_* / Gemini_* 同理)
```

两套**互不感知**,不要混放。Docker 用 `env_file: ./agent.env` 把 image-backend 键注入到 `image_gen.py` 的进程环境,优先级高于 `ppt-master/.env`(镜像里压根没带 `.env`)。

### 6.2 Python 3.10+ 强依赖

`ppt-master` 脚本大量使用 PEP 604 `X | None` 联合语法。外层服务的 `.venv` 若 < 3.10,**所有子进程**会在第一次 `import` 阶段崩,返回 500。务必用 `PPTMASTER_PYTHON_BIN` 或 `resolve_python_bin()` 的 6 级回退指向 3.10+。

### 6.3 `repo_dir` 语义

请求体里的 `repo_dir` 永远是**被调的** `ppt-master` 仓库的绝对路径,**不是**外层服务自身。`ensure_repo_dir()` 拒绝任何不含 `skills/ppt-master/` 的路径(返回 400)。`manifest_path` / `output_dir` 也限定在 `repo_dir` 之内(`resolve_repo_path` 用 `Path.relative_to` 防御越权)。

### 6.4 vendored `ppt-master/` 只读

有独立 `.git` / `AGENTS.md` / `CLAUDE.md` / `SKILL.md`。**不要**在外层 repo 里 commit 它;改了请走 upstream,再 re-sync clone。Docker `COPY` 时显式排除 `ppt-master/.git`。

### 6.5 串行强约束

`POST /export` 的 3 个脚本必须**严格串行**:先拆备注、再 finalize、最后导出。`run_logged_command` 是阻塞的,所以天然串行;但不要在外层把它们并发成 `asyncio.gather` —— `finalize_svg` 依赖 `total_md_split` 的输出,`svg_to_pptx` 依赖 `finalize_svg` 的 `svg_final/`。

### 6.6 `/export` 的"非阻塞首步"

代码里有个特殊处理:`total_md_split.py` 失败不视为整步失败(标记 `note: non-blocking`),因为备注是辅助,主 PPT 仍可导出。这是显式选择,不是 bug。

### 6.7 当前**未**覆盖的 `ppt-master` 主流程

外层稳定编排的只有:`建项目 → 生图 → 导出`。**仍未**自动产出:
- `design_spec.md` (可由 `/strategist` 触发,但 prompt 与原生 Strategist 不完全等价)
- `spec_lock.md` (同上)
- `svg_output/*.svg` 整组(可由 `/generate-svgs` 触发,但**是 DeepSeek 现编现写,不是按原生 Executor 流程读 `references/modes/...` 切换**)
- 模板融合(brand/layout/deck)与模板分页策略
- Confirm UI 启动(`scripts/confirm_ui/server.py` 仍可独立手动跑)

也就是说:外层把"在浏览器里点确认 → 选模板 → 看 Live Preview"这条**人工循环**换成了"HTTP API + DeepSeek 自动化",但**没有完全复刻原生 SKILL.md 的全部纪律**(例如 spec_lock 强制 inline、page 顺序逐页生成等)。

---

## 7. 端点对照表

| 方法 + 路径 | 行号(`app.py`) | 干什么 | 涉及 `ppt-master` 脚本 |
|---|---|---|---|
| `GET /` | 724 | 返回 `ui/index.html` | — |
| `GET /health` | 719 | 健康检查 | — |
| `POST /tasks/prepare` | 729 | 建项目 | `project_manager.py init` |
| `GET /tasks` | 1149 | 列出所有外层任务 | — |
| `GET /tasks/{id}` | 1134 | 单任务状态 | — |
| `GET /tasks/{id}/readiness` | 1122 | 导出就绪门禁 | — |
| `GET /tasks/{id}/artifacts` | 1175 | 列出 exports / images / 可拉文件 | — |
| `GET /tasks/{id}/files/{file_key}` | 1189 | 拉 prompt/metadata/run_log/result/plan/confirmation | — |
| `POST /tasks/{id}/agent-plan` | 794 | DeepSeek 规划师 | — |
| `POST /tasks/{id}/confirmation` | 807 | 保存 8 项确认 | — |
| `GET /tasks/{id}/confirmation` | 834 | 取 8 项确认 | — |
| `POST /tasks/{id}/strategist` | 992 | DeepSeek 策略师 → 写 `design_spec.md` + `spec_lock.md` | — |
| `POST /tasks/{id}/generate-image` | 844 | 生图(单图或 manifest) | `image_gen.py` |
| `POST /tasks/{id}/generate-svgs` | 1026 | DeepSeek 逐页出 SVG + 质量门 | `svg_quality_checker.py` |
| `POST /tasks/{id}/export` | 913 | 串行导出 3 步 | `total_md_split.py` → `finalize_svg.py` → `svg_to_pptx.py` |

---

## 8. 一句话总结

**ppt-master-agent = FastAPI 任务编排器 + DeepSeek 三个 prompt 角色 + `ppt-master` 脚本子进程网关 + 静态 UI 控制台**,通过 `/tasks/prepare → /agent-plan → /strategist → /generate-image → /generate-svgs → /export` 这条 6 步 HTTP 链,把 `ppt-master` 的多角色创作流水线(Strategist → Image_Generator → Executor → Post-process)拆解为可远程触发的、有 `run.log` 可审计的、有就绪门禁的服务形态;**所有真正的 PPT 能力仍由 `ppt-master/` 自己提供,外层只编排、不替代。**
