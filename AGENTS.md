# AGENTS.md

Outer FastAPI orchestrator for the vendored `ppt-master` skill. Owns
task bookkeeping, the DeepSeek planner, and an HTTP API; delegates
every real PPT capability (project creation, image generation, SVG
post-processing, export) to scripts inside `ppt-master/`.

## Read first

- `README.md` (root) — endpoint reference and curl recipes in Chinese.
- `ppt-master/AGENTS.md` and `ppt-master/skills/ppt-master/SKILL.md` —
  authoritative workflow for the inner skill. Read before touching
  anything inside `ppt-master/` or any task that hits image gen / SVG /
  export semantics.

## Layout

- `app.py` — sole service entrypoint. FastAPI app, all routes, Python
  interpreter resolution, subprocess orchestration.
- `requirements.txt` — runtime deps only: `fastapi`, `openai`,
  `uvicorn`. No dev deps.
- `Dockerfile` + `docker-compose.yml` + `agent.env.example` — single
  container that bakes in `ppt-master/`, serves the API and static UI
  on `:8080`, persists `.service_tasks/` to a named volume
  (`agent-state`). Use `cp agent.env.example agent.env` and fill in
  real keys before `docker compose up --build`.
- `.dockerignore` — excludes `jobs/`, `ppt-master/.git`, `__pycache__`,
  `.service_tasks/`, `agent.env`, and other image bloat.
- `ui/index.html` — static console served at `GET /`. Pure
  client-side fetch against the API; no server-side rendering.
- `jobs/` — hand-driven re-export / scratch projects. Has its own
  `design_spec.md`, `spec_lock.md`, `svg_output/`, `svg_final/`,
  `images/`, `exports/`, `backup/<timestamp>/`. Not created by the
  service; used offline. Excluded from the Docker build context.
- `.service_tasks/<task_id>/` — service-managed task state. Created
  by `POST /tasks/prepare` (`app.py:204-214`). In Docker, mounted to
  the `agent-state` named volume so it survives rebuilds.
- `ppt-master/` — **vendored clone, read-only**. Own `.git/`, own
  `README.md`, own `AGENTS.md` / `CLAUDE.md`. See "Vendored
  `ppt-master/`" below. `COPY`d into the image at build time; its
  `.env` is NOT carried over (image-backend keys are passed in via
  `agent.env`).

## Commands

```bash
# install (local dev)
pip install -r requirements.txt

# run (local dev)
uvicorn app:app --reload --port 8000
# console
open http://127.0.0.1:8000/

# Docker (single container, served on :8080 to avoid clashing with the
# sibling `ppt-agent-web` project on :8000)
cp agent.env.example agent.env   # then fill in real keys
docker compose up --build -d
docker compose logs -f backend
open http://127.0.0.1:8080/
docker compose down

# Docker dev mode (live UI reload). `dev.sh` is a thin wrapper around:
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# It bind-mounts ./ui and ./app.py and runs `uvicorn --reload
# --reload-dir /app`, so editing ui/index.html on the host is reflected on
# the next browser refresh, and app.py edits auto-reload. `agent-state`
# volume is shared with the production compose; use `-p pma-dev` to keep
# dev state in a separate project if needed.
./dev.sh
```

No test, lint, typecheck, or formatter commands exist. Do not invent
them.

## Endpoint map (jump to `app.py`)

- `POST /tasks/prepare` — `app.py:438`
- `POST /tasks/{task_id}/agent-plan` — `app.py:526`
- `POST /tasks/{task_id}/confirmation` / `GET …/confirmation` — `app.py:539`, `:566`
- `POST /tasks/{task_id}/generate-image` — `app.py:576`
- `POST /tasks/{task_id}/export` — `app.py:645`
- `GET /tasks`, `GET /tasks/{task_id}`,
  `GET /tasks/{task_id}/artifacts`,
  `GET /tasks/{task_id}/files/{file_key}` — `app.py:725+`
- `GET /health`, `GET /` (serves `ui/index.html`) — `app.py:428-435`

Request/response schemas: `app.py:28-120`. Canonical curl recipes:
root `README.md`.

## Two-env rule (the most common foot-gun)

This service reads **two different `.env` files** in **two different
places**, and neither knows about the other:

- **Root `.env`** (or shell env) — consumed only by `app.py`:
  - `DEEPSEEK_API_KEY` (required for `/agent-plan`)
  - `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`)
  - `DEEPSEEK_MODEL` (default `deepseek-v4-pro`)
  - `PPTMASTER_PYTHON_BIN` (optional manual override for the Python
    interpreter that runs `ppt-master` scripts)
  - Loaded by `service_env()` at `app.py:136-137`.
- **`ppt-master/.env`** — consumed by the `ppt-master` scripts the
  service shells out to (`image_gen.py`, etc.). `app.py` does **not**
  read this file itself. Image-backend config goes here:
  - `IMAGE_BACKEND=agnes` (or other backend)
  - Provider keys: `AGNES_*`, `OPENAI_*`, `GEMINI_*`, …
  - See `ppt-master/README.md` §Configuration for the full list.

Do not put image-backend keys in the root `.env`. Do not put
`DEEPSEEK_*` keys in `ppt-master/.env`.

## Python interpreter resolution

`resolve_python_bin()` at `app.py:176-194` tries in order:

1. `./.venv/bin/python`
2. `./venv/bin/python`
3. `./ppt-master/.venv/bin/python`
4. `./ppt-master/venv/bin/python`
5. `PPTMASTER_PYTHON_BIN` (manual override)
6. `python3.12` / `python3` / `python` on `PATH`

If a sub-script fails to launch with `ModuleNotFoundError`, this is
the first place to inspect. Do not hardcode `/usr/bin/python3` in
changes — let the resolver decide.

## `repo_dir` semantics

Every request body field named `repo_dir` is the absolute path of the
local **ppt-master checkout**, not this repo. `ensure_repo_dir()`
(`app.py:147-154`) rejects paths that don't contain
`skills/ppt-master/` with HTTP 400.

## Vendored `ppt-master/` is read-only

- It has its own `.git/`, own README, own `AGENTS.md` / `CLAUDE.md`
  / `SKILL.md`. Treat it as an upstream dependency.
- Do not edit, commit, or push inside `ppt-master/` from this repo.
  Land changes upstream and re-sync the clone.
- Inner workflow authority: `ppt-master/skills/ppt-master/SKILL.md`.
  Read it before any task involving image generation, SVG
  post-processing, export options, or canvas formats.

## Out of scope for this service

The orchestrator does NOT auto-produce `design_spec.md`,
`spec_lock.md`, or the full `svg_output/*.svg` set. `/export` will
fail on a project missing those. Drive them by calling
`/agent-plan` and acting on its recommendations — don't assume
end-to-end automation.

## State on disk

`.service_tasks/<task_id>/`:

- `task_prompt.txt` — echoed user request plus project context.
- `task_metadata.json` — repo_dir, project_dir, canvas_format, timestamps.
- `run.log` — every subprocess shell-out appended with
  command / stdout / stderr / return code. Writer:
  `append_run_log()` at `app.py:243-288`. Read this when debugging
  failed calls.
- `last_run.json` — most recent step result (`status`, `result`,
  `new_files`, `new_exports`, …).
- `agent_plan.json` — DeepSeek plan payload.
- `confirmation_data.json` — Strategist Eight Confirmations payload
  (`POST /tasks/{task_id}/confirmation`).

Allowed `file_key` values for `GET /tasks/{task_id}/files/{file_key}`
(`app.py:113-120`): `prompt`, `metadata`, `run_log`, `result`,
`plan`, `confirmation`.

## Security

The vendored `ppt-master/.env` currently contains a hardcoded
`AGNES_API_KEY` (committed inside that inner git repo). Rotate the
leaked key. Do not commit a new root `.env` containing
`DEEPSEEK_API_KEY` or other secrets. If you must add `.env`, keep it
git-ignored.

## This repo deliberately does NOT have

- No test suite, no `pytest` / `tests/` config.
- No linter or formatter config (`ruff`, `black`, `mypy`, …).
- No CI workflow — the `.github/` directory only exists inside
  `ppt-master/` (pages deploy, unrelated to this repo).
- No `pyproject.toml`, no `setup.cfg`. `requirements.txt` is the
  only dep manifest.
