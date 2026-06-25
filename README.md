# ppt-master-agent

一个面向 `ppt-master` 的外层编排服务。

它的定位现在是：

- 外层服务负责任务管理、LLM 规划、HTTP API
- `ppt-master` 负责真正的 PPT 能力
- 外层服务不再自己维护独立生图/导出实现，而是直接调用 `ppt-master` 官方脚本

## 保留的能力

- `POST /tasks/prepare`
  - 用 `project_manager.py init`
  - 在 `ppt-master/projects/<task_id>/` 下建立任务工程

- `POST /tasks/{task_id}/agent-plan`
  - 读取任务状态
  - 调用外层配置的 LLM 生成下一步编排建议
  - LLM 配置放在外层服务

- `POST /tasks/{task_id}/generate-image`
  - 直接调用 `ppt-master/skills/ppt-master/scripts/image_gen.py`
  - 使用 `ppt-master` 自己的 `.env` 和后端体系
  - `minimax` / Agnes 等生图配置只需要放在 `ppt-master/.env`

- `POST /tasks/{task_id}/run-pipeline`
  - 后端统一编排导出前置链路
  - 按需自动执行：
    - `strategist` 生成 `design_spec.md` + `spec_lock.md`
    - `generate-svgs` 生成 `svg_output/*.svg`
    - `export` 导出 PPTX

- `POST /tasks/{task_id}/export`
  - 顺序调用：
    - `total_md_split.py`
    - `finalize_svg.py`
    - `svg_to_pptx.py`

- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/readiness`
- `GET /tasks/{task_id}/artifacts`
- `GET /tasks/{task_id}/files/{file_key}`


- `POST /templates/upload`
  - 上传用户自己的模板 PPTX 文件
  - 自动调用 `pptx_template_import.py` 提取 manifest + SVG + identity
  - 仅写入 `.service_tasks/_templates/<template_id>/` 暂存区
  - 返回分析摘要（配色、字体、页面分类），不会自动进入全局模板库

- `POST /templates/official/upload`
  - 官方模板上传入口
  - 上传后立即完成分析、LLM 模板生成和 `register_template.py --kind deck`
  - 成功后写入 `templates/decks/<template_id>/` 并更新 `decks_index.json`

- `POST /templates/official/{template_id}/create`
  - 基于已有 staging 的官方入库入口
  - 读取 `manifest.json` + `identity.json` + `svg-flat` 样本生成 deck 模板
  - 适合“先用户上传分析，再由官方确认入库”的两步链路

- `POST /templates/{template_id}/create`
  - 兼容别名，行为等同于 `POST /templates/official/{template_id}/create`
  - 保留给旧调用方，新的官方入库请优先使用 `official` 路径

- `GET /templates`
  - 列出所有可用模板（decks, layouts, brands）

- `GET /templates/{template_id}`
  - 查看模板详情（design_spec.md, SVG 列表, 资源文件）

- `DELETE /templates/{template_id}`
  - 删除已注册模板及其 staging 数据

- `POST /tasks/prepare` 增加 `template_id` 可选字段
  - 指定模板后，项目将复制模板文件并使用模板约束设计
  - `/tasks/{task_id}/strategist` 会自动锁定模板配色/字体
  - `/tasks/{task_id}/generate-svgs` 会自动参考模板 SVG 布局

## 删除的旧逻辑

以下平行实现已经移除：

- 外层独立 Agnes 生图实现
- 旧的 `agent_executor.py`
- 旧的 `runner.py`

现在外层服务不再尝试“自己实现一套 ppt 生成逻辑”，而是只编排 `ppt-master`。

## 安装

```bash
pip install -r requirements.txt
```

根目录 `requirements.txt` 包含 outer service 自己的运行时依赖，
其中 `python-multipart` 是必需项。它被 FastAPI 的模板上传接口
（`/templates/upload`、`/templates/official/upload`）在应用导入阶段
直接检查；缺失时，`uvicorn app:app` 会在启动前抛出
`Form data requires "python-multipart" to be installed`。

## 配置

### 推荐：统一放在根目录一个配置文件里

推荐直接使用当前仓库根目录的 `agent.env`（或本地开发时使用 `.env`）。
现在外层 `app.py` 和它拉起的 `ppt-master` 子进程都会读取这一份根配置：

```bash
LLM_PROVIDER=deepseek
LLM_API_KEY=your_key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-pro

# 兼容旧配置名，仍然可用：
# DEEPSEEK_API_KEY=your_key
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# DEEPSEEK_MODEL=deepseek-v4-pro

# MiniMax M3 示例：
# LLM_PROVIDER=minimax
# LLM_API_KEY=your_key
# LLM_BASE_URL=https://api.minimaxi.com/v1
# LLM_MODEL=MiniMax-M3

# 可选：手动指定 ppt-master 脚本使用的 Python 解释器（见下方说明）
# PPTMASTER_PYTHON_BIN=/Users/you/.local/bin/python3.12
```

说明：

- `LLM_*` 供外层 `/agent-plan`、`/strategist`、`/generate-svgs` 和模板分析链路使用
- `IMAGE_BACKEND` 及各家 `*_API_KEY` / `*_MODEL` / `*_BASE_URL` 会透传给内层 `ppt-master` 脚本（如 `image_gen.py`）
- `DEEPSEEK_*` 仍然兼容，但只是 legacy fallback
- `PPTMASTER_PYTHON_BIN`（可选）用来指定运行 `ppt-master` 脚本的 Python 解释器。**这是宿主开发环境的常见坑**：项目内 `./.venv` 通常是 outer service 自身的 Python（fastapi / openai 装在那里），如果它是 3.9，而 `ppt-master` 脚本用了 PEP 604 的 `X | None` 类型联合语法（需要 3.10+），所有子进程调用（`/prepare`、`/generate-image`、`/export` …）都会以 `TypeError: unsupported operand type(s) for |` 失败并返回 500。务必把它指向一个 3.10+ 的解释器；留空则按以下顺序自动选择：
  1. `PPTMASTER_PYTHON_BIN`（本变量）
  2. `./.venv/bin/python`
  3. `./venv/bin/python`
  4. `./ppt-master/.venv/bin/python`
  5. `./ppt-master/venv/bin/python`
  6. `python3.12` / `python3` / `python` 在 `PATH` 上

例如 MiniMax 生图：

```bash
IMAGE_BACKEND=minimax
MINIMAX_API_KEY=your_key
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_MODEL=image-01
```

例如 Agnes：

```bash
IMAGE_BACKEND=agnes
AGNES_API_KEY=your_key
AGNES_MODEL=agnes-image-2.1-flash
AGNES_BASE_URL=https://apihub.agnes-ai.com/v1
```

### 兼容回退：`ppt-master/.env`

如果根目录 `agent.env` / `.env` 里没有提供某些生图变量，内层 `ppt-master`
脚本仍然会按它自己的机制回退到 `ppt-master/.env`。但从维护角度，建议把
LLM 和生图配置都统一放在根目录这一份文件里，避免本地开发和 Docker 的行为不一致。

## 启动

### 本地开发

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) 即可使用。
如果启动阶段直接因 multipart 报错退出，优先检查当前运行该命令的
Python 环境是否真的安装了根目录 `requirements.txt`。

### Docker 部署

容器把 `ppt-master/` 烘进镜像，单个服务同时提供 API 和静态 UI，
端口 `8080`（不和 `ppt-agent-web` 的 `8000` 冲突）。`.service_tasks/`
挂到名为 `agent-state` 的 named volume，rebuild 不丢任务。

```bash
cp agent.env.example agent.env       # 填入真实 key（LLM + image backend）
docker compose up --build -d
docker compose logs -f backend
open http://127.0.0.1:8080/

docker compose down                   # 停服务
docker compose down -v                # 停服务并清除 agent-state volume
```

#### Docker 开发模式（前端热改）

需要频繁改 `ui/index.html` 或 `app.py` 时，用 dev override：

```bash
./dev.sh                              # = docker compose -f ... -f docker-compose.dev.yml up --build
```

它做了两件事：

- bind-mount `./ui` 到容器 `/app/ui`，编辑宿主机上的 `ui/index.html` 后
  浏览器刷新即可看到新版本（FastAPI 用 `FileResponse` 每次请求重读磁盘）
- `uvicorn` 启动参数切到 `--reload --reload-dir /app`，
  `app.py` 改动也会自动重启

任务状态（`agent-state` 卷）按 dev override 单独命名，**不会**污染
生产 compose 的状态。生产部署继续用 `docker compose up` 即可。

镜像里 `ppt-master/.env` 不会被打包——`ppt-master` 脚本里 image
backend 的 key 由 `agent.env` 通过 docker compose 的 `env_file` 直接
注入到进程环境（`image_gen.py` 的查找顺序：进程环境 > cwd 的 `.env` >
skill 目录 > repo root），优先级最高。

## 典型流程

### 1. 创建任务工程

```bash
curl -X POST http://127.0.0.1:8000/tasks/prepare \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "prompt": "请基于这份 PDF 生成一份 10 页、16:9、融资汇报风格的可编辑 PPT",
    "canvas_format": "ppt169"
  }'
```

### 2. 让外层 LLM 给出下一步编排建议

```bash
curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/agent-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "task_id": "TASK_ID"
  }'
```

### 3. 调用 `ppt-master` 内部生图

单张：

```bash
curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/generate-image \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "task_id": "TASK_ID",
    "prompt": "一张适合 AI 商业汇报封面的未来感数据中心插画",
    "aspect_ratio": "16:9",
    "image_size": "1K",
    "filename": "cover_visual"
  }'
```

manifest：

```bash
curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/generate-image \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "task_id": "TASK_ID",
    "manifest_path": "projects/TASK_ID/images/image_prompts.json"
  }'
```

### 4. 让后端自动补齐前置步骤并导出 PPT

```bash
curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/run-pipeline \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "task_id": "TASK_ID",
    "strategist_model": "deepseek-v4-pro",
    "svg_model": "deepseek-v4-pro",
    "max_pages": 30
  }'
```

如果你已经手动完成了 `design_spec.md`、`spec_lock.md` 和 `svg_output/*.svg`，
仍然可以继续直接调用 `/export`。但在普通产品链路里，优先推荐
`/run-pipeline`，由后端兜底依赖关系。

### 5. 仅在需要时直接导出 PPT

```bash
curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/export \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_dir": "/absolute/path/to/ppt-master",
    "task_id": "TASK_ID"
  }'
```

### 6. 查看产物

```bash
curl "http://127.0.0.1:8000/tasks/TASK_ID/artifacts?repo_dir=/absolute/path/to/ppt-master"
curl "http://127.0.0.1:8000/tasks/TASK_ID/files/run_log?repo_dir=/absolute/path/to/ppt-master"
curl "http://127.0.0.1:8000/tasks/TASK_ID/files/plan?repo_dir=/absolute/path/to/ppt-master"
```

支持的 `file_key`：

- `prompt`
- `metadata`
- `run_log`
- `result`
- `plan`

## 当前边界

这个服务现在已经是“调用 `ppt-master` 能力”的外层 API，但它还没有替代 `ppt-master` 完整的 Step 4-6 创作过程。

也就是说它已经能稳定编排：

- 建项目
- 调 `ppt-master` 生图
- 跑导出链路

但像：

- 自动产出 `design_spec.md`
- 自动产出 `spec_lock.md`
- 自动生成整套 `svg_output/*.svg`

这些仍然需要你后续继续把“创作型 agent”接到这层 API 上，或者直接由上层 agent 调这个服务逐步完成。
