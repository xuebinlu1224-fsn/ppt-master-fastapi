#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".service_tasks"
STATE_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE_STAGING_DIR = STATE_DIR / "_templates"
TEMPLATE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_LLM_PROVIDER = "deepseek"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-pro"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


def _safe_print(msg: str) -> None:
    """Print to stderr with flush, matching the convention used by
    render_template_preview.py and visual_review.py."""
    import sys
    print(msg, file=sys.stderr, flush=True)


app = FastAPI(title="ppt-master-agent", version="0.3.0")


# Preview rendering constants
PREVIEW_MAX_PAGES = 5
PREVIEW_SERVER_URL = "http://localhost:5050"
EXAMPLES_DIR_NAME = "examples"


def get_examples_dir(repo_dir: Path) -> Path:
    """Return the path to the examples directory."""
    return repo_dir / EXAMPLES_DIR_NAME


def resolve_ppt_master_repo(base_dir: Path) -> Optional[Path]:
    """Try to locate the embedded ppt-master repository at startup.

    Looks for <base>/ppt-master/ first, then for a sibling
    ppt-master/ that contains skills/ppt-master/. Returns None if
    neither can be resolved.
    """
    candidates: list[Path] = []
    nested = base_dir / "ppt-master"
    if nested.is_dir() and (nested / "skills" / "ppt-master").is_dir():
        candidates.append(nested)
    for parent in [base_dir.parent, base_dir]:
        direct = parent
        if direct.is_dir() and (direct / "skills" / "ppt-master").is_dir():
            candidates.append(direct)
    # De-duplicate while preserving order
    seen: set[Path] = set()
    for c in candidates:
        rc = c.resolve()
        if rc not in seen:
            seen.add(rc)
            return rc
    return None


def list_examples(repo_dir: Path) -> list[dict[str, Any]]:
    """List all example templates under <repo>/examples/.

    Each example is a directory containing svg_final/ subdirectory.
    """
    examples_dir = get_examples_dir(repo_dir)
    if not examples_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(examples_dir.iterdir()):
        if not entry.is_dir():
            continue
        svg_dir = entry / "svg_final"
        if not svg_dir.is_dir():
            continue
        preview_dir = entry / ".preview"
        preview_count = 0
        if preview_dir.is_dir():
            preview_count = sum(1 for _ in preview_dir.glob("preview_*.png"))
        svg_count = sum(1 for _ in svg_dir.glob("*.svg"))
        items.append({
            "example_id": entry.name,
            "svg_dir": str(svg_dir),
            "preview_dir": str(preview_dir),
            "svg_count": svg_count,
            "preview_count": preview_count,
            "preview_available": preview_count >= min(PREVIEW_MAX_PAGES, svg_count),
        })
    return items


def list_examples_with_previews(
    repo_dir: Path, server_url: str = PREVIEW_SERVER_URL,
) -> list[dict[str, Any]]:
    """List example templates and ensure their previews are rendered.

    For each example whose preview is missing or incomplete, attempt to
    render the first 5 pages. Failures are reported in the returned dict
    but do not stop other examples from rendering.
    """
    examples = list_examples(repo_dir)
    if not examples:
        return examples

    python_bin = resolve_python_bin()
    repo_dir_resolved = repo_dir.resolve()
    script = script_path(repo_dir_resolved, "render_template_preview.py")

    for ex in examples:
        try:
            svg_count = ex.get("svg_count", 0)
            if svg_count == 0:
                ex["render_status"] = "skipped_no_svgs"
                continue

            if ex.get("preview_available"):
                ex["render_status"] = "already_available"
                continue

            svg_dir = Path(ex["svg_dir"])
            preview_dir = Path(ex["preview_dir"])
            preview_dir.mkdir(parents=True, exist_ok=True)

            cmd = [
                python_bin,
                str(script),
                str(svg_dir),
                "-o", str(preview_dir),
                "--server-url", server_url,
                "--pages", str(PREVIEW_MAX_PAGES),
            ]
            _safe_print(f"[startup] rendering preview for example: {ex['example_id']}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=build_subprocess_env(),
            )
            if result.returncode == 0:
                ex["render_status"] = "rendered"
                ex["preview_count"] = min(PREVIEW_MAX_PAGES, svg_count)
                ex["preview_available"] = True
            else:
                ex["render_status"] = f"failed:rc={result.returncode}"
                ex["render_error"] = (result.stderr or result.stdout)[:500]
                _safe_print(
                    f"[startup] preview render failed for {ex['example_id']}: "
                    f"rc={result.returncode} stderr={result.stderr[:200]}"
                )
        except subprocess.TimeoutExpired:
            ex["render_status"] = "failed:timeout"
            _safe_print(f"[startup] preview render timeout for {ex['example_id']}")
        except Exception as e:  # noqa: BLE001
            ex["render_status"] = f"failed:{type(e).__name__}"
            ex["render_error"] = str(e)[:200]
            _safe_print(f"[startup] preview render error for {ex['example_id']}: {e}")

    return examples


@app.on_event("startup")
async def startup_check_example_previews() -> None:
    """At app startup, ensure all example templates have preview PNGs.

    Renders only missing previews. Uses a thread-based background task so
    it does not block the server from accepting requests.
    """
    import threading

    def _runner() -> None:
        try:
            repo = resolve_ppt_master_repo(BASE_DIR)
            if repo is None:
                _safe_print(
                    "[startup] could not locate ppt-master repo; "
                    "skipping example preview check"
                )
                return
            examples_dir = get_examples_dir(repo)
            if not examples_dir.is_dir():
                _safe_print(
                    f"[startup] no examples/ directory under {repo}; "
                    "skipping preview check"
                )
                return

            examples = list_examples(repo)
            if not examples:
                _safe_print("[startup] no example templates found")
                return

            to_render = [e for e in examples if not e.get("preview_available")]
            _safe_print(
                f"[startup] found {len(examples)} example(s) under {repo}, "
                f"{len(to_render)} need preview rendering"
            )

            if to_render:
                list_examples_with_previews(repo)
                _safe_print("[startup] example preview check complete")
        except Exception as e:  # noqa: BLE001
            _safe_print(f"[startup] example preview check failed: {e}")

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()


class PrepareTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str = Field(..., description="Absolute path to the local ppt-master repository.")
    prompt: str = Field(..., description="Deck generation instruction.")
    task_id: Optional[str] = Field(default=None, description="Optional project id under projects/.")
    canvas_format: str = Field(default="ppt169", description="ppt-master canvas format passed to project_manager.py init.")
    template_id: Optional[str] = Field(default=None, description="Optional template id (from templates/decks/) to apply as design constraint.")


class AgentPlanRequest(BaseModel):
    repo_dir: str
    task_id: str
    model: Optional[str] = Field(default=None)
    extra_instructions: Optional[str] = None


class GenerateImageRequest(BaseModel):
    repo_dir: str
    task_id: str
    prompt: Optional[str] = None
    manifest_path: Optional[str] = None
    backend: Optional[str] = None
    output_dir: Optional[str] = None
    aspect_ratio: str = Field(default="16:9")
    image_size: str = Field(default="1K")
    filename: Optional[str] = Field(default=None, description="Filename without extension for single-prompt mode.")
    model: Optional[str] = None
    concurrency: Optional[int] = None

    @model_validator(mode="after")
    def validate_source(self) -> "GenerateImageRequest":
        if bool(self.prompt) == bool(self.manifest_path):
            raise ValueError("Provide exactly one of prompt or manifest_path.")
        return self


class ExportTaskRequest(BaseModel):
    repo_dir: str
    task_id: str
    source: Optional[Literal["output", "final"]] = None
    svg_snapshot: bool = False
    no_merge: bool = False


class ConfirmationDataRequest(BaseModel):
    repo_dir: str
    task_id: str
    payload: dict[str, Any]


class StrategistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str
    task_id: str
    model: Optional[str] = Field(default=None)


class GenerateSvgsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str
    task_id: str
    model: Optional[str] = Field(default=None)
    max_pages: int = Field(default=30, ge=1, le=100)


class RunPipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str
    task_id: str
    strategist_model: Optional[str] = Field(default=None)
    svg_model: Optional[str] = Field(default=None)
    max_pages: int = Field(default=30, ge=1, le=100)
    source: Optional[Literal["output", "final"]] = None
    svg_snapshot: bool = False
    no_merge: bool = False


class TemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str
    template_id: str
    model: Optional[str] = Field(default=None)


class TaskStatusResponse(BaseModel):
    task_id: str
    project_dir: str
    prompt_file: str
    metadata_file: str
    run_log_file: str
    user_prompt: str
    last_run: Optional[dict[str, Any]] = None


class TaskFileResponse(BaseModel):
    task_id: str
    file_key: str
    path: str
    content: str


class TaskListItem(BaseModel):
    task_id: str
    project_dir: str
    created_at: Optional[str] = None
    user_prompt: str
    has_result: bool
    has_plan: bool


class TaskArtifactsResponse(BaseModel):
    task_id: str
    project_dir: str
    exports: list[str]
    images: list[str]
    available_files: dict[str, str]
    last_run: Optional[dict[str, Any]] = None


ALLOWED_TASK_FILES = {
    "prompt": "prompt_file",
    "metadata": "metadata_file",
    "run_log": "run_log_file",
    "result": "result_file",
    "plan": "plan_file",
    "confirmation": "confirmation_file",
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def root_config_env() -> dict[str, str]:
    """Return merged root-level config from agent.env and .env.

    Precedence matches service_env(): process env > .env > agent.env.
    This helper only returns file-backed values, so callers should merge it
    under os.environ with setdefault semantics.
    """
    values: dict[str, str] = {}
    for path in (BASE_DIR / "agent.env", BASE_DIR / ".env"):
        values.update(read_env_file(path))
    return values


def service_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return (
        os.environ.get(name)
        or root_config_env().get(name)
        or default
    )


def service_env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = service_env(name)
        if value:
            return value
    return default


def build_task_id(custom_task_id: Optional[str]) -> str:
    if custom_task_id:
        return custom_task_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{uuid.uuid4().hex[:6]}"


def build_template_id(custom_template_id: Optional[str]) -> str:
    if custom_template_id:
        return custom_template_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"tpl_{timestamp}_{uuid.uuid4().hex[:6]}"


def ensure_repo_dir(repo_dir: Path) -> Path:
    repo_dir = repo_dir.expanduser().resolve()
    if not repo_dir.exists() or not repo_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Invalid repo_dir: {repo_dir}")
    skill_dir = repo_dir / "skills" / "ppt-master"
    if not skill_dir.exists():
        raise HTTPException(status_code=400, detail=f"repo_dir does not look like a ppt-master clone: {repo_dir}")
    return repo_dir


def resolve_repo_path(repo_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else repo_dir / candidate).expanduser().resolve()
    try:
        resolved.relative_to(repo_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Path escapes repo_dir: {raw_path}") from exc
    return resolved


def resolve_download_path(repo_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else repo_dir / candidate).expanduser().resolve()
    allowed_roots = (repo_dir, STATE_DIR.resolve())
    if not any(root == resolved or root in resolved.parents for root in allowed_roots):
        raise HTTPException(status_code=400, detail=f"Path escapes allowed download roots: {raw_path}")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def resolve_python_bin() -> str:
    configured = service_env("PPTMASTER_PYTHON_BIN")
    project_venv_candidates = [
        BASE_DIR / ".venv" / "bin" / "python",
        BASE_DIR / "venv" / "bin" / "python",
        BASE_DIR / "ppt-master" / ".venv" / "bin" / "python",
        BASE_DIR / "ppt-master" / "venv" / "bin" / "python",
    ]
    candidates = [
        configured,
        *(str(path) for path in project_venv_candidates),
        shutil.which("python3.12"),
        shutil.which("python3"),
        shutil.which("python"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    raise HTTPException(status_code=500, detail="No usable Python interpreter found for ppt-master scripts.")


def build_subprocess_env(extra_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build child-process env with root config available to vendored scripts."""
    env = os.environ.copy()
    for key, value in root_config_env().items():
        env.setdefault(key, value)
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)
    return env


def script_path(repo_dir: Path, script_name: str) -> Path:
    path = repo_dir / "skills" / "ppt-master" / "scripts" / script_name
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Missing ppt-master script: {path}")
    return path


def task_paths(repo_dir: Path, task_id: str) -> dict[str, Path]:
    state_dir = STATE_DIR / task_id
    return {
        "state_dir": state_dir,
        "prompt_file": state_dir / "task_prompt.txt",
        "metadata_file": state_dir / "task_metadata.json",
        "run_log_file": state_dir / "run.log",
        "result_file": state_dir / "last_run.json",
        "plan_file": state_dir / "agent_plan.json",
        "confirmation_file": state_dir / "confirmation_data.json",
    }


def template_paths(template_id: str) -> dict[str, Path]:
    staging_dir = TEMPLATE_STAGING_DIR / template_id
    workspace_dir = staging_dir / "workspace"
    return {
        "staging_dir": staging_dir,
        "source_file": staging_dir / "source.pptx",
        "workspace_dir": workspace_dir,
        "manifest_file": workspace_dir / "manifest.json",
        "summary_file": workspace_dir / "summary.md",
        "identity_file": workspace_dir / "identity.json",
        "svg_dir": workspace_dir / "svg-flat",
        "assets_dir": workspace_dir / "assets",
        "state_file": staging_dir / "template_state.json",
    }


def get_template_lib_dir(repo_dir: Path, template_id: str) -> Path:
    return repo_dir / "skills" / "ppt-master" / "templates" / "decks" / template_id


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_task_prompt(prompt: str, project_dir: Path, prompt_file: Path, task_id: str) -> None:
    content = (
        "You are orchestrating a local ppt-master project.\n\n"
        f"Task ID: {task_id}\n"
        f"Project directory: {project_dir}\n\n"
        "User request:\n"
        f"{prompt.strip()}\n"
    )
    prompt_file.write_text(content, encoding="utf-8")


def available_task_files(paths: dict[str, Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for file_key, path_key in ALLOWED_TASK_FILES.items():
        file_path = paths[path_key]
        if file_path.exists():
            result[file_key] = str(file_path)
    return result


def append_run_log(log_path: Path, label: str, command: list[str], completed: subprocess.CompletedProcess[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat()}] {label}\n")
        handle.write("$ " + shlex.join(command) + "\n")
        if completed.stdout:
            handle.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                handle.write("\n")
        if completed.stderr:
            handle.write("[stderr]\n")
            handle.write(completed.stderr)
            if not completed.stderr.endswith("\n"):
                handle.write("\n")
        handle.write(f"[return_code] {completed.returncode}\n\n")


def run_logged_command(
    *,
    repo_dir: Path,
    paths: dict[str, Path],
    label: str,
    command: list[str],
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    paths["run_log_file"].parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        env=build_subprocess_env(extra_env),
    )
    append_run_log(paths["run_log_file"], label, command, completed)
    return {
        "label": label,
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout[-20000:],
        "stderr": completed.stderr[-12000:],
    }


def run_logged_command_simple(
    *,
    repo_dir: Path,
    log_path: Path,
    label: str,
    command: list[str],
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        env=build_subprocess_env(extra_env),
    )
    append_run_log(log_path, label, command, completed)
    return {
        "label": label,
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout[-20000:],
        "stderr": completed.stderr[-12000:],
    }


def list_exports(project_dir: Path) -> list[str]:
    exports_dir = project_dir / "exports"
    if not exports_dir.exists():
        return []
    return sorted(str(path) for path in exports_dir.glob("*.pptx"))


def list_images(project_dir: Path) -> list[str]:
    images_dir = project_dir / "images"
    if not images_dir.exists():
        return []
    return sorted(str(path) for path in images_dir.iterdir() if path.is_file())


def persist_last_run(paths: dict[str, Path], payload: dict[str, Any]) -> None:
    paths["result_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["result_file"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_task(repo_dir: Path, task_id: str) -> tuple[dict[str, Path], dict[str, Any], Path]:
    paths = task_paths(repo_dir, task_id)
    metadata = load_json(paths["metadata_file"])
    if metadata is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    project_dir = Path(metadata["project_dir"]).expanduser().resolve()
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail=f"Project directory missing for task {task_id}: {project_dir}")
    return paths, metadata, project_dir


def extract_created_project_path(stdout: str) -> Path:
    for line in stdout.splitlines():
        prefix = "Project created: "
        if line.startswith(prefix):
            return Path(line[len(prefix):].strip()).expanduser().resolve()
    raise HTTPException(status_code=500, detail="Could not parse created project path from project_manager.py output.")


def build_project_state(project_dir: Path) -> dict[str, Any]:
    svg_output = project_dir / "svg_output"
    svg_final = project_dir / "svg_final"
    notes = project_dir / "notes"
    images = project_dir / "images"
    return {
        "design_spec_exists": (project_dir / "design_spec.md").exists(),
        "spec_lock_exists": (project_dir / "spec_lock.md").exists(),
        "image_prompts_manifest_exists": (images / "image_prompts.json").exists(),
        "svg_output_count": len(list(svg_output.glob("*.svg"))) if svg_output.exists() else 0,
        "svg_final_count": len(list(svg_final.glob("*.svg"))) if svg_final.exists() else 0,
        "notes_count": len(list(notes.glob("*.md"))) if notes.exists() else 0,
        "exports_count": len(list((project_dir / "exports").glob("*.pptx"))) if (project_dir / "exports").exists() else 0,
        "images_count": len([p for p in images.iterdir() if p.is_file()]) if images.exists() else 0,
    }


def build_export_readiness(project_dir: Path) -> dict[str, Any]:
    project_state = build_project_state(project_dir)
    missing_requirements = [
        requirement
        for requirement, present in (
            ("design_spec.md", project_state["design_spec_exists"]),
            ("spec_lock.md", project_state["spec_lock_exists"]),
            ("svg_output/*.svg", project_state["svg_output_count"] > 0),
        )
        if not present
    ]
    return {
        "project_state": project_state,
        "ready_for_export": not missing_requirements,
        "missing_requirements": missing_requirements,
    }


def read_template_design_spec(repo_dir: Path, template_id: str) -> Optional[str]:
    template_dir = get_template_lib_dir(repo_dir, template_id)
    spec_path = template_dir / "design_spec.md"
    if spec_path.exists():
        return spec_path.read_text(encoding="utf-8")
    return None


def execute_strategist(task_id: str, repo_dir: Path, model: str) -> dict[str, Any]:
    paths, metadata, project_dir = ensure_task(repo_dir, task_id)
    project_state = build_project_state(project_dir)
    task_prompt = metadata.get("user_prompt", "")
    canvas_format = metadata.get("canvas_format", "ppt169")

    template_id = metadata.get("template_id")
    template_design_spec = None
    if template_id:
        template_design_spec = read_template_design_spec(repo_dir, template_id)

    strat_result = call_llm_strategist(
        task_prompt=task_prompt,
        canvas_format=canvas_format,
        project_state=project_state,
        model=model,
        template_design_spec=template_design_spec,
    )
    design_spec_path = project_dir / "design_spec.md"
    spec_lock_path = project_dir / "spec_lock.md"
    design_spec_path.write_text(strat_result["design_spec_md"], encoding="utf-8")
    spec_lock_path.write_text(strat_result["spec_lock_md"], encoding="utf-8")

    payload = {
        "status": "ok",
        "task_id": task_id,
        "step": "strategist",
        "design_spec_path": str(design_spec_path),
        "spec_lock_path": str(spec_lock_path),
        "design_spec_size": len(strat_result["design_spec_md"]),
        "spec_lock_size": len(strat_result["spec_lock_md"]),
        "template_applied": bool(template_id),
    }
    persist_last_run(paths, payload)
    return payload


def execute_generate_svgs(task_id: str, repo_dir: Path, model: str, max_pages: int) -> dict[str, Any]:
    paths, metadata, project_dir = ensure_task(repo_dir, task_id)

    spec_lock_path = project_dir / "spec_lock.md"
    if not spec_lock_path.exists():
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "task_id": task_id,
                "message": "spec_lock.md is required before SVG generation. Run /strategist first.",
                "missing_requirements": ["spec_lock.md"],
            },
        )

    design_spec_path = project_dir / "design_spec.md"
    design_spec_md = design_spec_path.read_text(encoding="utf-8") if design_spec_path.exists() else ""

    svg_output_dir = project_dir / "svg_output"
    svg_output_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = project_dir / "templates"
    template_svgs: dict[str, str] = {}
    if templates_dir.exists():
        for svg_file in sorted(templates_dir.glob("*.svg")):
            template_svgs[svg_file.name] = svg_file.read_text(encoding="utf-8")

    def _match_template_svg(page_meta: dict[str, Any]) -> Optional[str]:
        if not template_svgs:
            return None
        rhythm = page_meta.get("rhythm", "")
        layout = page_meta.get("layout", "")
        for name, content in template_svgs.items():
            name_lower = name.lower()
            if rhythm and rhythm.lower() in name_lower:
                return content
            if layout and layout.lower() in name_lower:
                return content
        svg_names = sorted(template_svgs.keys())
        pid_str = page_meta.get("page", "")
        idx = int(pid_str[1:]) - 1 if pid_str and pid_str[1:].isdigit() else -1
        if idx == 0 and svg_names:
            return template_svgs[svg_names[0]]
        return None

    spec_lock_md = spec_lock_path.read_text(encoding="utf-8")
    pages = parse_spec_lock_pages(spec_lock_md)
    if not pages:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "task_id": task_id,
                "message": "Could not parse any page entries from spec_lock.md. "
                "Check that spec_lock.md has page_rhythm, page_layouts, or content_outline sections.",
            },
        )

    pages = pages[: min(len(pages), max_pages)]

    generated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for page_meta in pages:
        page_id = page_meta["page"]
        svg_path = svg_output_dir / f"{page_id.lower()}_{page_meta.get('rhythm', 'slide')}.svg"
        try:
            spec_lock_md = spec_lock_path.read_text(encoding="utf-8")
            tpl_svg = _match_template_svg(page_meta)
            svg_text = call_llm_svg_page(
                spec_lock_md=spec_lock_md,
                page_meta=page_meta,
                design_spec_md=design_spec_md,
                model=model,
                template_svg=tpl_svg,
            )
            valid, error = validate_minimal_svg(svg_text)
            if not valid:
                spec_lock_md = spec_lock_path.read_text(encoding="utf-8")
                svg_text = call_llm_svg_page(
                    spec_lock_md=spec_lock_md,
                    page_meta=page_meta,
                    design_spec_md=design_spec_md,
                    model=model,
                    template_svg=tpl_svg,
                )
                valid, error = validate_minimal_svg(svg_text)
            if not valid:
                failed.append({"page": page_id, "reason": f"SVG validation failed after retry: {error}"})
                continue
            svg_path.write_text(svg_text, encoding="utf-8")
            generated.append({"page": page_id, "file": str(svg_path), "rhythm": page_meta.get("rhythm")})
        except Exception as exc:
            failed.append({"page": page_id, "reason": str(exc)})

    python_bin = resolve_python_bin()
    quality_checker = script_path(repo_dir, "svg_quality_checker.py")
    quality_result = run_logged_command(
        repo_dir=repo_dir,
        paths=paths,
        label="svg_quality_checker",
        command=[python_bin, str(quality_checker), str(project_dir)],
    )

    payload = {
        "status": "ok" if not failed else "partial",
        "step": "generate-svgs",
        "task_id": task_id,
        "pages_generated": len(generated),
        "pages_failed": len(failed),
        "generated": generated,
        "failed": failed,
        "quality_report": quality_result,
        "svg_output_dir": str(svg_output_dir),
    }
    persist_last_run(paths, payload)
    return payload


def execute_export_task(
    task_id: str,
    repo_dir: Path,
    source: Optional[Literal["output", "final"]],
    svg_snapshot: bool,
    no_merge: bool,
) -> dict[str, Any]:
    paths, _, project_dir = ensure_task(repo_dir, task_id)
    python_bin = resolve_python_bin()
    readiness = build_export_readiness(project_dir)
    if not readiness["ready_for_export"]:
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "task_id": task_id,
                "message": "Project is not ready for export.",
                "missing_requirements": readiness["missing_requirements"],
                "project_state": readiness["project_state"],
            },
        )

    total_md_split = script_path(repo_dir, "total_md_split.py")
    finalize_svg = script_path(repo_dir, "finalize_svg.py")
    svg_to_pptx = script_path(repo_dir, "svg_to_pptx.py")

    before_exports = set(list_exports(project_dir))
    steps = [
        run_logged_command(
            repo_dir=repo_dir,
            paths=paths,
            label="export:total_md_split",
            command=[python_bin, str(total_md_split), str(project_dir)],
        ),
        run_logged_command(
            repo_dir=repo_dir,
            paths=paths,
            label="export:finalize_svg",
            command=[python_bin, str(finalize_svg), str(project_dir)],
        ),
    ]

    export_command = [python_bin, str(svg_to_pptx), str(project_dir)]
    if source:
        export_command.extend(["-s", source])
    if svg_snapshot:
        export_command.append("--svg-snapshot")
    if no_merge:
        export_command.append("--no-merge")
    steps.append(
        run_logged_command(
            repo_dir=repo_dir,
            paths=paths,
            label="export:svg_to_pptx",
            command=export_command,
        )
    )

    status = "ok"
    for step in steps:
        if step["return_code"] != 0:
            if step["label"] == "export:total_md_split":
                step["note"] = "total_md_split failed (speaker notes missing) — non-blocking"
                continue
            status = "error"
            break

    after_exports = set(list_exports(project_dir))
    payload = {
        "status": status,
        "task_id": task_id,
        "steps": steps,
        "new_exports": sorted(after_exports - before_exports),
        "all_exports": sorted(after_exports),
    }
    persist_last_run(paths, payload)
    if status != "ok":
        raise HTTPException(status_code=500, detail=payload)
    return payload


def copy_template_to_project(repo_dir: Path, template_id: str, project_dir: Path) -> bool:
    template_dir = get_template_lib_dir(repo_dir, template_id)
    if not template_dir.exists():
        return False
    dest = project_dir / "templates"
    dest.mkdir(parents=True, exist_ok=True)
    for item in template_dir.iterdir():
        dest_item = dest / item.name
        if item.is_dir():
            if dest_item.exists():
                shutil.rmtree(dest_item)
            shutil.copytree(item, dest_item)
        else:
            shutil.copy2(item, dest_item)
    return True


def list_template_index(repo_dir: Path, kind: str) -> list[dict[str, Any]]:
    templates_dir = repo_dir / "skills" / "ppt-master" / "templates"
    kind_dir_map = {"deck": "decks", "layout": "layouts", "brand": "brands"}
    dir_name = kind_dir_map.get(kind, kind)
    # Index file lives in the per-kind subdirectory, e.g. decks/decks_index.json
    index_path = templates_dir / dir_name / f"{dir_name}_index.json"
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    items = []
    if isinstance(data, dict):
        for tid, entry in data.items():
            item = {"template_id": tid, "kind": kind}
            if isinstance(entry, dict):
                item.update(entry)
            items.append(item)
    items.sort(key=lambda x: x.get("template_id", ""))
    return items


def read_template_info(repo_dir: Path, template_id: str) -> Optional[dict[str, Any]]:
    template_dir = get_template_lib_dir(repo_dir, template_id)
    if not template_dir.exists():
        return None
    spec_path = template_dir / "design_spec.md"
    pages = sorted(str(p.name) for p in template_dir.glob("*.svg"))
    assets = sorted(
        str(p.name) for p in template_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )
    return {
        "template_id": template_id,
        "kind": "deck",
        "design_spec": spec_path.read_text(encoding="utf-8") if spec_path.exists() else "",
        "pages": pages,
        "assets": assets,
    }


def persist_template_state(paths: dict[str, Path], payload: dict[str, Any]) -> None:
    paths["state_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["state_file"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_template_state(paths: dict[str, Path]) -> Optional[dict[str, Any]]:
    return load_json(paths["state_file"])


def _validate_uploaded_template_file(file: UploadFile) -> None:
    if not file.filename or not file.filename.lower().endswith(".pptx"):
        raise HTTPException(status_code=400, detail="Only .pptx files are accepted.")
    if file.size and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Maximum {MAX_UPLOAD_SIZE // 1024 // 1024} MB.")


def _build_template_analysis(
    *,
    manifest: Optional[dict[str, Any]],
    identity: Optional[dict[str, Any]],
    svg_dir: Path,
    assets_dir: Path,
) -> dict[str, Any]:
    svg_files = sorted(str(p.name) for p in svg_dir.glob("*.svg")) if svg_dir.exists() else []
    assets_files = sorted(str(p.name) for p in assets_dir.iterdir()) if assets_dir.exists() else []
    return {
        "manifest_extracted": manifest is not None,
        "identity_extracted": identity is not None,
        "canvas": identity.get("canvas", {}) if identity else {},
        "theme": identity.get("theme", {}) if identity else {},
        "page_type_candidates": manifest.get("pageTypeCandidates", {}) if manifest else {},
        "svg_file_count": len(svg_files),
        "assets_count": len(assets_files),
    }


def analyze_uploaded_template(
    *,
    repo: Path,
    template_id: str,
    source_name: str,
    source_bytes: bytes,
) -> dict[str, Any]:
    python_bin = resolve_python_bin()
    tp = template_paths(template_id)
    tp["staging_dir"].mkdir(parents=True, exist_ok=True)
    tp["source_file"].write_bytes(source_bytes)

    log_path = tp["staging_dir"] / "import.log"

    manifest_cmd = [
        python_bin,
        str(script_path(repo, "pptx_template_import.py")),
        str(tp["source_file"]),
        "-o", str(tp["workspace_dir"]),
        "--manifest-only",
    ]
    manifest_result = run_logged_command_simple(
        repo_dir=repo,
        log_path=log_path,
        label="pptx_template_import:manifest-only",
        command=manifest_cmd,
    )

    svg_cmd = [
        python_bin,
        str(script_path(repo, "pptx_template_import.py")),
        str(tp["source_file"]),
        "-o", str(tp["workspace_dir"]),
        "--skip-manifest",
        "--inheritance-mode", "flat",
    ]
    svg_result = run_logged_command_simple(
        repo_dir=repo,
        log_path=log_path,
        label="pptx_template_import:svg-flat",
        command=svg_cmd,
    )

    identity_cmd = [
        python_bin,
        str(script_path(repo, "beautify_identity.py")),
        str(tp["source_file"]),
        "-o", str(tp["identity_file"]),
    ]
    identity_result = run_logged_command_simple(
        repo_dir=repo,
        log_path=log_path,
        label="beautify_identity",
        command=identity_cmd,
    )

    manifest = load_json(tp["manifest_file"])
    identity = load_json(tp["identity_file"])
    analysis = _build_template_analysis(
        manifest=manifest,
        identity=identity,
        svg_dir=tp["svg_dir"],
        assets_dir=tp["assets_dir"],
    )
    state = read_template_state(tp) or {}
    state.update({
        "template_id": template_id,
        "status": "analyzed",
        "source_name": source_name,
        "created_at": state.get("created_at") or datetime.now().isoformat(),
        "repo_dir": str(repo),
        "analysis": analysis,
        "registered": False,
        "template_dir": None,
        "pages": [],
        "step_results": {
            "manifest": manifest_result,
            "svg_conversion": svg_result,
            "identity": identity_result,
        },
    })
    persist_template_state(tp, state)
    return {
        "template_id": template_id,
        "status": "analyzed",
        "source_name": source_name,
        "analysis": analysis,
        "registered": False,
        "staging_dir": str(tp["staging_dir"]),
    }


def create_registered_template_from_staging(
    *,
    repo: Path,
    template_id: str,
    model: str,
    source: str,
) -> dict[str, Any]:
    tp = template_paths(template_id)
    if not tp["manifest_file"].exists():
        raise HTTPException(status_code=404, detail=f"Template analysis not found for: {template_id}. Upload first via POST /templates/upload.")
    if not tp["identity_file"].exists():
        raise HTTPException(status_code=404, detail=f"Identity not yet extracted for: {template_id}.")

    python_bin = resolve_python_bin()
    log_path = tp["staging_dir"] / "import.log"
    manifest_json = tp["manifest_file"].read_text(encoding="utf-8")
    identity_json = tp["identity_file"].read_text(encoding="utf-8")

    svg_samples: list[dict[str, str]] = []
    svg_dir = tp["svg_dir"]
    if svg_dir.exists():
        svg_list = sorted(svg_dir.glob("slide_*.svg"))
        indices = [0]
        if len(svg_list) > 2:
            indices.append(len(svg_list) // 2)
        if len(svg_list) > 1:
            indices.append(len(svg_list) - 1)
        for i in indices:
            if 0 <= i < len(svg_list):
                svg_path = svg_list[i]
                svg_samples.append({
                    "name": svg_path.name,
                    "content": svg_path.read_text(encoding="utf-8"),
                    "note": "first slide" if i == 0 else "last slide" if i == len(svg_list) - 1 else "middle slide",
                })

    result = call_llm_template_creator(
        manifest_json=manifest_json,
        identity_json=identity_json,
        svg_samples=svg_samples,
        model=model,
    )

    template_dir = get_template_lib_dir(repo, template_id)
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "design_spec.md").write_text(result["design_spec_md"], encoding="utf-8")

    pages = []
    for filename, svg_content in result.get("template_svgs", {}).items():
        svg_path = template_dir / filename
        svg_path.write_text(svg_content, encoding="utf-8")
        pages.append(filename)
    pages.sort()

    assets_dir = tp["assets_dir"]
    if assets_dir.exists():
        for asset in assets_dir.iterdir():
            if asset.is_file():
                shutil.copy2(asset, template_dir / asset.name)

    register_cmd = [
        python_bin,
        str(script_path(repo, "register_template.py")),
        template_id,
        "--kind", "deck",
    ]
    register_result = run_logged_command_simple(
        repo_dir=repo,
        log_path=log_path,
        label="register_template",
        command=register_cmd,
    )

    state = read_template_state(tp) or {}
    state.update({
        "status": "registered" if register_result["return_code"] == 0 else "registration_failed",
        "template_dir": str(template_dir),
        "pages": pages,
        "registered": register_result["return_code"] == 0,
        "registered_at": datetime.now().isoformat(),
        "registration_source": source,
    })
    persist_template_state(tp, state)

    return {
        "template_id": template_id,
        "status": state["status"],
        "template_dir": str(template_dir),
        "design_spec_path": str(template_dir / "design_spec.md"),
        "pages": pages,
        "registered": register_result["return_code"] == 0,
        "source": source,
    }

# --- LLM clients ---

def _get_llm_config() -> dict[str, str]:
    provider = service_env_first("LLM_PROVIDER", default=DEFAULT_LLM_PROVIDER) or DEFAULT_LLM_PROVIDER
    provider = provider.strip().lower()

    api_key = service_env_first("LLM_API_KEY", "DEEPSEEK_API_KEY")
    if not api_key and provider == "minimax":
        api_key = service_env("MINIMAX_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing LLM API key in service environment or .env. "
                "Set LLM_API_KEY (recommended) or keep using legacy DEEPSEEK_API_KEY."
            ),
        )

    base_url = service_env_first("LLM_BASE_URL", "DEEPSEEK_BASE_URL", default=DEFAULT_LLM_BASE_URL)
    if provider == "minimax":
        base_url = service_env_first(
            "LLM_BASE_URL",
            "MINIMAX_LLM_BASE_URL",
            "DEEPSEEK_BASE_URL",
            default="https://api.minimaxi.com/v1",
        )

    default_model = service_env_first("LLM_MODEL", "DEEPSEEK_MODEL", default=DEFAULT_LLM_MODEL) or DEFAULT_LLM_MODEL
    if provider == "minimax":
        default_model = service_env_first(
            "LLM_MODEL",
            "MINIMAX_LLM_MODEL",
            "DEEPSEEK_MODEL",
            default="MiniMax-M3",
        ) or "MiniMax-M3"

    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "default_model": default_model,
    }


def _get_llm_client() -> tuple["OpenAI", dict[str, str]]:
    config = _get_llm_config()
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Missing dependency: openai") from exc
    return OpenAI(api_key=config["api_key"], base_url=config["base_url"]), config


def _resolve_requested_model(config: dict[str, str], requested_model: Optional[str]) -> str:
    default_model = config["default_model"]
    if not requested_model or not requested_model.strip():
        return default_model

    candidate = requested_model.strip()
    provider = config["provider"]
    lowered = candidate.lower()

    # Guard against stale UI / caller defaults leaking a model from another provider.
    if provider == "minimax" and lowered.startswith("deepseek"):
        return default_model
    if provider == "deepseek" and lowered.startswith("minimax"):
        return default_model

    return candidate


def _extract_json_object_text(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("empty response content")

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if text.startswith("{") and text.endswith("}"):
        return text

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object start found")

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]

    raise ValueError("no complete JSON object found")


def _parse_model_json_content(raw_text: str, provider: str, label: str) -> dict[str, Any]:
    try:
        return json.loads(_extract_json_object_text(raw_text))
    except (json.JSONDecodeError, ValueError) as exc:
        preview = (raw_text or "")[:1200]
        raise HTTPException(
            status_code=500,
            detail=f"{provider} {label} did not return valid JSON content: {exc}. Raw={preview}",
        ) from exc


def _resolve_minimax_llm_url(base_url: str) -> str:
    override = service_env("MINIMAX_LLM_ENDPOINT")
    if override:
        return override.rstrip("/")
    base = base_url.rstrip("/")
    if base.endswith("/chatcompletion_v2"):
        return base
    if base.endswith("/v1/text"):
        return base + "/chatcompletion_v2"
    if base.endswith("/v1"):
        return base + "/text/chatcompletion_v2"
    return base + "/v1/text/chatcompletion_v2"


def _extract_minimax_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("reply"), str) and payload["reply"].strip():
        return payload["reply"]

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
            messages = first.get("messages")
            if isinstance(messages, list) and messages:
                first_message = messages[0]
                if isinstance(first_message, dict):
                    text = first_message.get("text")
                    if isinstance(text, str) and text.strip():
                        return text

    data = payload.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("reply"), str) and data["reply"].strip():
            return data["reply"]
        if isinstance(data.get("text"), str) and data["text"].strip():
            return data["text"]

    raise HTTPException(
        status_code=500,
        detail=f"MiniMax response missing text payload: {json.dumps(payload, ensure_ascii=False)[:1200]}",
    )


def _call_minimax_text_api(
    *,
    config: dict[str, str],
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    url = _resolve_minimax_llm_url(config["base_url"])
    prompt_text = (
        "System Instructions:\n"
        f"{system_prompt.strip()}\n\n"
        "User Input:\n"
        f"{user_prompt.strip()}"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "sender_type": "USER",
                "sender_name": "user",
                "text": prompt_text,
            }
        ],
        "reply_constraints": {
            "sender_type": "BOT",
            "sender_name": "assistant",
        },
        "tokens_to_generate": 4096,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=500,
            detail=(
                f"MiniMax HTTP {exc.code} calling {url}. "
                f"Model={model}. Response={error_body[:1200]}"
            ),
        ) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MiniMax request failed for {url}: {exc.reason}",
        ) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"MiniMax did not return valid JSON from {url}: {body[:1200]}",
        ) from exc

    base_resp = parsed.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = base_resp.get("status_code")
        if status_code not in (None, 0, "0"):
            raise HTTPException(
                status_code=500,
                detail=f"MiniMax API error from {url}: {json.dumps(parsed, ensure_ascii=False)[:1200]}",
            )

    text = _extract_minimax_text(parsed)
    usage = parsed.get("usage")
    return {
        "content": text,
        "usage": usage if isinstance(usage, dict) else None,
        "raw": parsed,
        "url": url,
    }


def call_llm_strategist(
    task_prompt: str,
    canvas_format: str,
    project_state: dict[str, Any],
    model: str,
    template_design_spec: Optional[str] = None,
) -> dict[str, str]:
    client, config = _get_llm_client()
    resolved_model = _resolve_requested_model(config, model)
    template_block = ""
    if template_design_spec:
        template_block = (
            "\n\n=== TEMPLATE CONSTRAINT (MANDATORY) ===\n"
            "This project uses a pre-existing design template. The following design_spec.md "
            "defines the template's visual identity. You MUST strictly preserve:\n"
            "- Color palette (all HEX values from the template)\n"
            "- Typography system (font families, sizes, roles)\n"
            "- Layout principles and visual style\n"
            "Adapt the template to the user's content needs, but do NOT change the core "
            "visual identity elements. Override only when the user's prompt explicitly "
            "demands a different style.\n\n"
            "TEMPLATE DESIGN SPEC:\n"
            f"{template_design_spec}\n"
        )
    system_prompt = (
        "You are a PPT design strategist for the ppt-master system. "
        "Your job is to produce two markdown files for a presentation project: "
        "design_spec.md (human-readable design narrative) and "
        "spec_lock.md (machine-readable execution contract).\n\n"
        "Follow these rules:\n"
        "1. Auto-infer all Eight Confirmations from the user's prompt and project context. "
        "Do not ask questions — make your best judgment.\n"
        "2. design_spec.md must follow this 11-section structure: "
        "I. Project Information (name, canvas, page count, audience, style), "
        "II. Canvas Specification (format, viewBox, margins), "
        "III. Visual Theme (mode, visual_style, 11-role color scheme with HEX values, gradients), "
        "IV. Typography System (font stacks for CJK+Latin per role, body-based size hierarchy), "
        "V. Layout Principles (page structure, spacing specs), "
        "VI. Icon Usage (library name, stroke_width if applicable), "
        "VII. Visualization Reference List (per-page chart templates), "
        "VIII. Image Resource List (per-image: filename, dimensions, purpose, acquire_via), "
        "IX. Content Outline (per-page: title, core_message, content_blocks), "
        "X. Speaker Notes Requirements, "
        "XI. Technical Constraints (forbidden SVG features).\n"
        "3. spec_lock.md must be a KEY-VALUE style execution contract — NO markdown headers (##) anywhere. "
        "Every section is a top-level key followed by colon then indented values. "
        "Sections: "
        "canvas, mode, visual_style, colors (bg/primary/accent/secondary_accent/text/text_secondary/border "
        "with HEX), typography (font_family/title_family/body_family, body px baseline, title/subtitle/annotation sizes), "
        "icons (library, stroke_width), images (key-to-path), "
        "page_rhythm (MUST use format: P01: cover, P02: dense — one Pxx: value per line), "
        "page_layouts (MUST use format: P01: layout_cover, P02: layout_dense — one Pxx: layoutname per line, "
        "do NOT nest layout definitions), "
        "page_charts (MUST use format: P02: chart_bar_grouped — one Pxx: chartname per line, "
        "do NOT nest chart definitions), "
        "forbidden (banned SVG features).\n"
        "4. Colors must be valid HEX values (e.g. #1A1A2E). Use 6-8 distinct roles.\n"
        "5. Typography: suggest system-available CJK+Latin font pairs. body_size baseline 14-18px for 1920x1080 canvas.\n"
        "6. Page count: infer from user prompt. For 16:9 format use viewBox='0 0 1920 1080'.\n"
        "7. Forbidden SVG features: foreignObject, external fonts, complex filters, "
        "animations, script tags, cross-file references, <use> elements (use inline shapes instead).\n\n"
        "Return a JSON object with exactly two string fields: "
        '{"design_spec_md": "...", "spec_lock_md": "..."}. '
        "The markdown content must escape double quotes and newlines properly for JSON."
    )
    user_message = json.dumps(
        {
            "task_prompt": task_prompt,
            "canvas_format": canvas_format,
            "project_state": project_state,
            "instructions": (
                "Generate both design_spec.md and spec_lock.md based on the task_prompt above. "
                "The canvas_format is already selected — use appropriate viewBox. "
                "Estimate page count from the prompt's content scope. "
                "Design a cohesive visual theme with a clear color palette and typography system."
            ),
            "template_context": template_design_spec or "",
        },
        ensure_ascii=False,
        indent=2,
    )
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": system_prompt + template_block},
            {"role": "user", "content": user_message},
        ],
        stream=False,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    parsed = _parse_model_json_content(content, config["provider"], "strategist")
    if not isinstance(parsed.get("design_spec_md"), str) or not isinstance(parsed.get("spec_lock_md"), str):
        raise HTTPException(status_code=500, detail="Strategist response missing design_spec_md or spec_lock_md fields.")
    return {
        "design_spec_md": parsed["design_spec_md"],
        "spec_lock_md": parsed["spec_lock_md"],
    }


def call_llm_template_creator(
    manifest_json: str,
    identity_json: str,
    svg_samples: list[dict[str, str]],
    model: str,
) -> dict[str, str]:
    client, config = _get_llm_client()
    resolved_model = _resolve_requested_model(config, model)
    svg_sample_block = ""
    for sample in svg_samples:
        svg_sample_block += f"\n\n### {sample['name']}\n```svg\n{sample['content'][:3000]}\n```\n"

    system_prompt = (
        "You are a template designer for the ppt-master system. "
        "Your job is to create a reusable deck template from an uploaded PPTX file.\n\n"
        "You will receive:\n"
        "1. A manifest.json describing the PPTX structure (slide size, theme colors, fonts, per-slide layout info)\n"
        "2. An identity.json with extracted visual identity (color palette, fonts, sizes)\n"
        "3. A few sample SVG conversions of key slides (cover, content, ending)\n\n"
        "Your task:\n"
        "1. Produce a design_spec.md with YAML frontmatter and these sections:\n"
        "   - Template Overview (summary, use case, style)\n"
        "   - Color Scheme (exact HEX values from identity, mapped to ppt-master roles)\n"
        "   - Signature Design Elements (key visual motifs, decorations, layout patterns)\n"
        "   - Page Roster (list each template SVG with its purpose)\n"
        "2. Produce template SVG pages. For each page type (cover, toc, chapter, content, ending), "
        "create a clean, maintainable SVG that captures the visual style of the original PPTX. "
        "Replace original text content with {{PLACEHOLDER}} markers:\n"
        "   - {{TITLE}} for main titles\n"
        "   - {{SUBTITLE}} for subtitles\n"
        "   - {{CONTENT}} for body content areas\n"
        "   - {{CHAPTER_NUM}} / {{CHAPTER_TITLE}} for chapter dividers\n"
        "   - {{THANK_YOU}} for ending pages\n"
        "   - {{AUTHOR}} / {{DATE}} for metadata\n"
        "3. The SVGs should be simplified reconstructions — not 1:1 copies. "
        "Preserve the color palette, font choices, decoration style, and layout rhythm.\n\n"
        "Return a JSON object with exactly two fields:\n"
        '{"design_spec_md": "...", "template_svgs": {"01_cover.svg": "...", "02_toc.svg": "...", ...}}\n'
        "Each SVG value is the full SVG markup string. Use viewBox matching the canvas size."
    )
    user_message = json.dumps(
        {
            "manifest": json.loads(manifest_json),
            "identity": json.loads(identity_json),
            "svg_samples_list": [{"name": s["name"], "note": s.get("note", "")} for s in svg_samples],
            "instructions": (
                "Create a designer-friendly deck template from this source PPTX. "
                "Generate at minimum: cover, chapter, content, ending pages. "
                "Also include TOC if the source has one. "
                "Use the exact colors and fonts from the identity. "
                "Keep SVGs clean and maintainable — simplify decorations but preserve the visual signature."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": svg_sample_block + "\n\n" + user_message},
        ],
        stream=False,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    parsed = _parse_model_json_content(content, config["provider"], "template creator")
    if not isinstance(parsed.get("design_spec_md"), str):
        raise HTTPException(status_code=500, detail="Template creator response missing design_spec_md field.")
    return {
        "design_spec_md": parsed["design_spec_md"],
        "template_svgs": parsed.get("template_svgs", {}),
    }


def parse_spec_lock_pages(spec_lock_md: str) -> list[dict[str, Optional[str]]]:
    import re

    def strip_val(v: str) -> str:
        return v.strip().strip('"').strip("'")

    pages: dict[str, dict[str, Optional[str]]] = {}

    rhythm_section = _extract_section(spec_lock_md, "page_rhythm")
    if rhythm_section:
        for match in re.finditer(r"^\s*(P\d+):\s*(.+)", rhythm_section, re.MULTILINE):
            pid = match.group(1)
            rhythm_val = strip_val(match.group(2))
            if pid not in pages:
                pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}
            pages[pid]["rhythm"] = rhythm_val

    layouts_section = _extract_section(spec_lock_md, "page_layouts")
    layout_names: list[str] = []
    if layouts_section:
        layout_names = re.findall(r"^\s*(\w+):\s*$", layouts_section, re.MULTILINE)
        layout_names = [ln for ln in layout_names if ln not in ("type",)]

    for pid, page in pages.items():
        rhythm = page.get("rhythm", "")
        if rhythm:
            for ln in layout_names:
                if rhythm in ln:
                    page["layout"] = ln
                    break
            if not page["layout"] and layout_names:
                idx = int(pid[1:]) - 1 if pid[1:].isdigit() else 0
                if 0 <= idx < len(layout_names):
                    page["layout"] = layout_names[idx]

    charts_section = _extract_section(spec_lock_md, "page_charts")
    if charts_section:
        chart_names = re.findall(r"^\s*(P\d+_\w+):\s*$", charts_section, re.MULTILINE)
        for pid, page in pages.items():
            for cn in chart_names:
                if cn.startswith(pid + "_"):
                    page["chart"] = cn
                    break

    if not pages:
        outline = _extract_section(spec_lock_md, "content_outline")
        if outline:
            for match in re.finditer(r"^\s*(P\d+)", outline, re.MULTILINE):
                pid = match.group(1)
                if pid not in pages:
                    pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}

    result = sorted(pages.values(), key=lambda p: p["page"])
    if not result:
        all_page_ids = sorted(set(re.findall(r"\b(P\d+)\b", spec_lock_md)))
        for pid in all_page_ids:
            pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}
        result = sorted(pages.values(), key=lambda p: p["page"])
    return result


def _extract_section(md_text: str, section_name: str) -> str:
    import re
    pattern = re.compile(rf"^(?:#+\s*)?{re.escape(section_name)}\s*:?\s*$", re.MULTILINE)
    match = pattern.search(md_text)
    if not match:
        return ""
    start = match.end()
    next_key = re.compile(r"^(?:#+\s*)?\w[\w\s]*:?\s*$", re.MULTILINE)
    next_match = next_key.search(md_text, start)
    end = next_match.start() if next_match else len(md_text)
    return md_text[start:end]


def call_llm_svg_page(
    spec_lock_md: str,
    page_meta: dict[str, Any],
    design_spec_md: str,
    model: str,
    template_svg: Optional[str] = None,
) -> str:
    client, config = _get_llm_client()
    resolved_model = _resolve_requested_model(config, model)
    page_id = page_meta.get("page", "unknown")
    rhythm = page_meta.get("rhythm", "dense")
    layout = page_meta.get("layout", "")
    chart = page_meta.get("chart", "")
    template_hint = ""
    if template_svg:
        template_hint = (
            "\n\n=== TEMPLATE SVG REFERENCE (base layout and decorations) ===\n"
            f"{template_svg[:4000]}\n"
            "=== END TEMPLATE ===\n"
            "Keep the same decoration, background, and layout structure from the template. "
            "Replace {{PLACEHOLDER}} text with the actual content from design_spec."
        )
    system_prompt = (
        "You are an SVG designer for PowerPoint presentations. "
        "Generate a single clean, well-structured SVG slide that will be converted to PPTX.\n\n"
        "CRITICAL RULES:\n"
        "1. Output ONLY the <svg>...</svg> element. No markdown, no explanations, no code fences.\n"
        "2. Use EXACTLY the colors, fonts, and dimensions from the provided spec_lock.\n"
        "3. Do NOT use <foreignObject>, external fonts, animations, <script>, or cross-file references.\n"
        "4. All text must use <text> elements with proper font-family, font-size, fill from the spec.\n"
        "5. Use <rect>, <circle>, <line>, <path> for shapes. Use <g> for grouping.\n"
        "6. Keep the design clean and modern. Use the visual_style and mode from the spec.\n"
        "7. viewBox must match the canvas spec. No overflowing content beyond viewBox.\n"
        "8. For charts (bar/line/pie), use simple <rect>/<path> based visualizations — "
        "no complex chart libraries.\n"
        "9. Text content must be concrete and contextual — derive it from the design_spec content outline "
        "for this page. Do NOT use placeholder text like 'Lorem ipsum' or 'Content here'.\n"
        "10. Include a slide number if appropriate."
    )
    layout_hint = f"Layout template: {layout}" if layout else ""
    chart_hint = f"Chart type: {chart}" if chart else ""
    user_message = (
        f"Page: {page_id}\n"
        f"Rhythm: {rhythm}\n"
        f"{layout_hint}\n"
        f"{chart_hint}\n"
        f"{template_hint}\n\n"
        "=== SPEC LOCK (execution contract) ===\n"
        f"{spec_lock_md}\n\n"
        "=== DESIGN SPEC (content outline for this page) ===\n"
        f"{design_spec_md}\n\n"
        "Generate the SVG for this page now. Output only the <svg> element."
    )
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        stream=False,
    )
    svg_text = response.choices[0].message.content or ""
    svg_text = svg_text.strip()
    if svg_text.startswith("```"):
        lines = svg_text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        svg_text = "\n".join(lines).strip()
    return svg_text


def validate_minimal_svg(svg: str) -> tuple[bool, str]:
    if not svg:
        return False, "empty SVG string"
    if "<svg" not in svg.lower():
        return False, "missing <svg tag"
    if "</svg>" not in svg.lower():
        return False, "missing </svg> closing tag"
    import re
    viewbox_match = re.search(r'viewBox\s*=\s*["\']([^"\']+)["\']', svg, re.IGNORECASE)
    if not viewbox_match:
        return False, "missing viewBox attribute"
    return True, ""


def call_llm_plan(request: AgentPlanRequest, repo_dir: Path, metadata: dict[str, Any], project_state: dict[str, Any]) -> dict[str, Any]:
    client, config = _get_llm_client()
    resolved_model = _resolve_requested_model(config, request.model)
    system_prompt = (
        "You are the orchestration planner for a local ppt-master API service. "
        "Return strict JSON only. Do not invent completed work. "
        "If design_spec/spec_lock/SVG pages are missing, say the project is not ready for export yet. "
        "Recommend using the /strategist endpoint to auto-generate design_spec.md + spec_lock.md, "
        "then /generate-svgs to produce SVG pages, then /export to produce the PPTX. "
        "Prefer using ppt-master abilities such as project_manager.py, image_gen.py, analyze_images.py, "
        "total_md_split.py, finalize_svg.py, and svg_to_pptx.py."
    )
    user_payload = {
        "task_id": request.task_id,
        "repo_dir": str(repo_dir),
        "user_prompt": metadata.get("user_prompt", ""),
        "project_state": project_state,
        "supported_service_endpoints": [
            "POST /tasks/prepare",
            "POST /tasks/{task_id}/agent-plan",
            "POST /tasks/{task_id}/strategist",
            "POST /tasks/{task_id}/generate-image",
            "POST /tasks/{task_id}/generate-svgs",
            "POST /tasks/{task_id}/run-pipeline",
            "POST /tasks/{task_id}/export",
            "GET /tasks/{task_id}/readiness",
            "GET /tasks/{task_id}",
            "GET /tasks/{task_id}/artifacts",
            "GET /tasks/{task_id}/files/{file_key}",
        ],
        "supported_ppt_master_capabilities": [
            "project_manager.py init",
            "image_gen.py single prompt",
            "image_gen.py --manifest",
            "strategist (design_spec.md + spec_lock.md generation)",
            "executor (per-page SVG generation)",
            "total_md_split.py",
            "finalize_svg.py",
            "svg_to_pptx.py",
            "svg_quality_checker.py",
        ],
        "extra_instructions": request.extra_instructions or "",
        "required_response_schema": {
            "summary": "string",
            "current_stage": "string",
            "ready_for_export": "boolean",
            "blocking_gaps": ["string"],
            "recommended_next_actions": [
                {
                    "action": "string",
                    "reason": "string",
                    "service_endpoint": "string",
                }
            ],
        },
    }
    response = client.chat.completions.create(
        model=resolved_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ],
        stream=False,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    parsed = _parse_model_json_content(content, config["provider"], "plan")
    usage = getattr(response, "usage", None)
    return {
        "provider": config["provider"],
        "model": resolved_model,
        "base_url": config["base_url"],
        "generated_at": datetime.now().isoformat(),
        "plan": parsed,
        "usage": usage.model_dump() if usage else None,
    }


# ============================================================================
# Endpoints: Health, Index
# ============================================================================

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "ui" / "index.html")


# ============================================================================
# Endpoints: Templates
# ============================================================================

@app.post("/templates/upload")
async def upload_template(
    repo_dir: str = Form(...),
    file: UploadFile = File(...),
    template_id: Optional[str] = Form(None),
) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    _validate_uploaded_template_file(file)
    tid = build_template_id(template_id)
    source_bytes = await file.read()
    return analyze_uploaded_template(
        repo=repo,
        template_id=tid,
        source_name=file.filename,
        source_bytes=source_bytes,
    )


@app.post("/templates/official/upload")
async def upload_official_template(
    repo_dir: str = Form(...),
    file: UploadFile = File(...),
    template_id: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    _validate_uploaded_template_file(file)
    tid = build_template_id(template_id)
    source_bytes = await file.read()
    analyze_uploaded_template(
        repo=repo,
        template_id=tid,
        source_name=file.filename,
        source_bytes=source_bytes,
    )
    return create_registered_template_from_staging(
        repo=repo,
        template_id=tid,
        model=model,
        source="official_upload",
    )


@app.post("/templates/official/{template_id}/create")
def create_official_template(template_id: str, request: TemplateCreateRequest) -> dict[str, Any]:
    if request.template_id != template_id:
        raise HTTPException(status_code=400, detail="Path template_id does not match request body template_id.")
    repo = ensure_repo_dir(Path(request.repo_dir))
    return create_registered_template_from_staging(
        repo=repo,
        template_id=template_id,
        model=request.model,
        source="official_create",
    )


@app.post("/templates/{template_id}/create")
def create_template_compat(template_id: str, request: TemplateCreateRequest) -> dict[str, Any]:
    return create_official_template(template_id, request)


@app.get("/templates")
def list_templates(repo_dir: str, kind: Optional[str] = None) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    result: dict[str, list[dict[str, Any]]] = {}
    kinds = [kind] if kind else ["deck", "layout", "brand"]
    for k in kinds:
        items = list_template_index(repo, k)
        # For deck templates, also check preview availability
        if k == "deck" and items:
            for item in items:
                tid = item.get("template_id", "")
                preview_dir = get_template_lib_dir(repo, tid) / ".preview"
                if preview_dir.is_dir():
                    png_count = sum(1 for _ in preview_dir.glob("preview_*.png"))
                    item["preview_count"] = png_count
                    item["preview_available"] = png_count >= min(
                        PREVIEW_MAX_PAGES, item.get("page_count", 0) or 0
                    )
                else:
                    item["preview_count"] = 0
                    item["preview_available"] = False
        if items:
            result[k + "s"] = items
    if not result:
        result = {"decks": [], "layouts": [], "brands": []}
    return result


@app.get("/templates/{template_id}")
def get_template(template_id: str, repo_dir: str) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    info = read_template_info(repo, template_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Template not found: {template_id}")
    return info


def _resolve_template_preview_dir(repo_dir: Path, template_id: str) -> Optional[Path]:
    """Return the .preview directory for a deck template, or None if missing."""
    template_dir = get_template_lib_dir(repo_dir, template_id)
    if not template_dir.exists():
        return None
    preview_dir = template_dir / ".preview"
    return preview_dir if preview_dir.is_dir() else None


@app.post("/templates/{template_id}/render-preview")
def render_template_preview_endpoint(
    template_id: str, repo_dir: str, force: bool = False,
) -> dict[str, Any]:
    """Manually trigger preview rendering for a deck template.

    Skips already-rendered files unless force=True. Returns a summary of
    rendered/skipped/failed pages.
    """
    repo = ensure_repo_dir(Path(repo_dir))
    template_dir = get_template_lib_dir(repo, template_id)
    if not template_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"Template not found: {template_id}"
        )

    svg_files = sorted(p.name for p in template_dir.glob("*.svg"))
    if not svg_files:
        raise HTTPException(
            status_code=400, detail=f"No SVG files in template: {template_id}"
        )

    preview_dir = template_dir / ".preview"
    preview_dir.mkdir(parents=True, exist_ok=True)

    python_bin = resolve_python_bin()
    script = script_path(repo, "render_template_preview.py")

    cmd = [
        python_bin,
        str(script),
        str(template_dir),
        "-o", str(preview_dir),
        "--server-url", PREVIEW_SERVER_URL,
        "--pages", str(PREVIEW_MAX_PAGES),
    ]
    if force:
        cmd.append("--force")

    _safe_print(f"[render-preview] template={template_id} cmd={' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=build_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Preview render timed out")

    return {
        "template_id": template_id,
        "preview_dir": str(preview_dir),
        "return_code": result.returncode,
        "stdout": (result.stdout or "")[:1000],
        "stderr": (result.stderr or "")[:1000],
    }


@app.get("/templates/{template_id}/preview/{page_num}")
def get_template_preview(
    template_id: str, repo_dir: str, page_num: int,
) -> FileResponse:
    """Return a preview PNG for the given template page (1-5)."""
    if page_num < 1 or page_num > PREVIEW_MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"page_num must be 1..{PREVIEW_MAX_PAGES}",
        )
    repo = ensure_repo_dir(Path(repo_dir))
    preview_dir = _resolve_template_preview_dir(repo, template_id)
    if preview_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"Preview not available for template: {template_id}",
        )
    png_path = preview_dir / f"preview_{page_num:02d}.png"
    if not png_path.is_file():
        raise HTTPException(
            status_code=404, detail=f"Preview page {page_num} not found",
        )
    return FileResponse(png_path, media_type="image/png")


@app.get("/examples")
def list_examples_endpoint(repo_dir: str) -> dict[str, Any]:
    """List all example templates under <repo>/examples/ with preview status.

    Previews are auto-rendered at startup if missing. This endpoint reports
    the current state and per-example render status.
    """
    repo = ensure_repo_dir(Path(repo_dir))
    items = list_examples(repo)
    return {"examples": items, "count": len(items)}


@app.get("/examples/{example_id}/preview/{page_num}")
def get_example_preview(
    repo_dir: str, example_id: str, page_num: int,
) -> FileResponse:
    """Return a preview PNG for the given example page (1-5)."""
    if page_num < 1 or page_num > PREVIEW_MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"page_num must be 1..{PREVIEW_MAX_PAGES}",
        )
    repo = ensure_repo_dir(Path(repo_dir))
    example_dir = get_examples_dir(repo) / example_id
    if not example_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"Example not found: {example_id}"
        )
    preview_dir = example_dir / ".preview"
    if not preview_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Preview not available for example: {example_id}",
        )
    png_path = preview_dir / f"preview_{page_num:02d}.png"
    if not png_path.is_file():
        raise HTTPException(
            status_code=404, detail=f"Preview page {page_num} not found",
        )
    return FileResponse(png_path, media_type="image/png")


@app.delete("/templates/{template_id}")
def delete_template(template_id: str, repo_dir: str) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    template_dir = get_template_lib_dir(repo, template_id)
    staging_dir = TEMPLATE_STAGING_DIR / template_id

    deleted = []
    if template_dir.exists():
        shutil.rmtree(template_dir)
        deleted.append(str(template_dir))
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
        deleted.append(str(staging_dir))

    python_bin = resolve_python_bin()
    register_cmd = [python_bin, str(script_path(repo, "register_template.py")), "--kind", "deck", "--rebuild-all"]
    subprocess.run(
        register_cmd,
        cwd=repo,
        capture_output=True,
        text=True,
        env=build_subprocess_env(),
    )

    return {"template_id": template_id, "deleted": deleted, "status": "ok"}


# ============================================================================
# Endpoints: Tasks
# ============================================================================

@app.post("/tasks/prepare", response_model=TaskStatusResponse)
def prepare_task(request: PrepareTaskRequest) -> TaskStatusResponse:
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    task_id = build_task_id(request.task_id)
    paths = task_paths(repo_dir, task_id)
    if paths["state_dir"].exists():
        raise HTTPException(status_code=409, detail=f"Task state already exists: {paths['state_dir']}")

    if request.template_id:
        template_dir = get_template_lib_dir(repo_dir, request.template_id)
        if not template_dir.exists() or not (template_dir / "design_spec.md").exists():
            raise HTTPException(status_code=400, detail=f"Template not found or missing design_spec.md: {request.template_id}")

    python_bin = resolve_python_bin()
    project_manager = script_path(repo_dir, "project_manager.py")
    init_command = [
        python_bin,
        str(project_manager),
        "init",
        task_id,
        "--format",
        request.canvas_format,
        "--dir",
        str(repo_dir / "projects"),
    ]
    init_completed = subprocess.run(
        init_command,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        env=build_subprocess_env(),
    )
    init_result = {
        "label": "project_manager:init",
        "command": init_command,
        "return_code": init_completed.returncode,
        "stdout": init_completed.stdout[-20000:],
        "stderr": init_completed.stderr[-12000:],
    }
    if init_result["return_code"] != 0:
        raise HTTPException(status_code=500, detail=init_result)
    project_dir = extract_created_project_path(init_completed.stdout)
    append_run_log(paths["run_log_file"], "project_manager:init", init_command, init_completed)

    template_copied = False
    if request.template_id:
        template_copied = copy_template_to_project(repo_dir, request.template_id, project_dir)

    write_task_prompt(request.prompt, project_dir, paths["prompt_file"], task_id)
    metadata = {
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
        "repo_dir": str(repo_dir),
        "project_dir": str(project_dir),
        "prompt_file": str(paths["prompt_file"]),
        "user_prompt": request.prompt.strip(),
        "canvas_format": request.canvas_format,
        "template_id": request.template_id,
    }
    paths["metadata_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["metadata_file"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_last_run(paths, {"status": "ok", "step": "prepare", "task_id": task_id, "template_id": request.template_id, "template_copied": template_copied})

    return TaskStatusResponse(
        task_id=task_id,
        project_dir=str(project_dir),
        prompt_file=str(paths["prompt_file"]),
        metadata_file=str(paths["metadata_file"]),
        run_log_file=str(paths["run_log_file"]),
        user_prompt=request.prompt.strip(),
        last_run=load_json(paths["result_file"]),
    )


@app.post("/tasks/{task_id}/agent-plan")
def build_agent_plan(task_id: str, request: AgentPlanRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, metadata, project_dir = ensure_task(repo_dir, task_id)
    project_state = build_project_state(project_dir)
    plan_payload = call_llm_plan(request, repo_dir, metadata, project_state)
    paths["plan_file"].write_text(json.dumps(plan_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_last_run(paths, {"status": "ok", "step": "agent-plan", "plan_file": str(paths["plan_file"])})
    return plan_payload


@app.post("/tasks/{task_id}/confirmation")
def save_confirmation(task_id: str, request: ConfirmationDataRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, metadata, project_dir = ensure_task(repo_dir, task_id)
    payload = {
        "task_id": task_id,
        "repo_dir": str(repo_dir),
        "project_dir": str(project_dir),
        "saved_at": datetime.now().isoformat(),
        "user_prompt": metadata.get("user_prompt", ""),
        "confirmation_data": request.payload,
    }
    paths["confirmation_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["confirmation_file"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_last_run(
        paths,
        {
            "status": "ok",
            "step": "confirmation",
            "confirmation_file": str(paths["confirmation_file"]),
        },
    )
    return payload


@app.get("/tasks/{task_id}/confirmation")
def get_confirmation(task_id: str, repo_dir: str) -> dict[str, Any]:
    repo_dir_path = ensure_repo_dir(Path(repo_dir))
    paths, _, _ = ensure_task(repo_dir_path, task_id)
    payload = load_json(paths["confirmation_file"])
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Confirmation data not found for task: {task_id}")
    return payload


@app.post("/tasks/{task_id}/generate-image")
def generate_image(task_id: str, request: GenerateImageRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, _, project_dir = ensure_task(repo_dir, task_id)
    python_bin = resolve_python_bin()
    image_gen = script_path(repo_dir, "image_gen.py")

    output_dir = resolve_repo_path(repo_dir, request.output_dir) if request.output_dir else project_dir / "images"
    before_files = {path.resolve() for path in output_dir.glob("*")} if output_dir.exists() else set()

    if request.manifest_path:
        manifest_path = resolve_repo_path(repo_dir, request.manifest_path)
        command = [python_bin, str(image_gen), "--manifest", str(manifest_path), "-o", str(output_dir)]
        if request.backend:
            command.extend(["--backend", request.backend])
        if request.image_size:
            command.extend(["--image_size", request.image_size])
        if request.model:
            command.extend(["--model", request.model])
        if request.concurrency is not None:
            command.extend(["--concurrency", str(request.concurrency)])
        result = run_logged_command(repo_dir=repo_dir, paths=paths, label="image_gen:manifest", command=command)
        render_result = run_logged_command(
            repo_dir=repo_dir,
            paths=paths,
            label="image_gen:render-md",
            command=[python_bin, str(image_gen), "--render-md", str(manifest_path)],
        )
        result["render_md"] = render_result
        manifest_md = str(manifest_path.with_suffix(".md"))
    else:
        command = [
            python_bin,
            str(image_gen),
            request.prompt or "",
            "--aspect_ratio",
            request.aspect_ratio,
            "--image_size",
            request.image_size,
            "-o",
            str(output_dir),
        ]
        if request.filename:
            command.extend(["--filename", request.filename])
        if request.backend:
            command.extend(["--backend", request.backend])
        if request.model:
            command.extend(["--model", request.model])
        result = run_logged_command(repo_dir=repo_dir, paths=paths, label="image_gen:single", command=command)
        manifest_md = None

    after_files = {path.resolve() for path in output_dir.glob("*")} if output_dir.exists() else set()
    new_files = sorted(str(path) for path in (after_files - before_files) if path.is_file())
    payload = {
        "status": "ok" if result["return_code"] == 0 else "error",
        "task_id": task_id,
        "output_dir": str(output_dir),
        "new_files": new_files,
        "manifest_markdown": manifest_md,
        "result": result,
    }
    persist_last_run(paths, payload)
    if result["return_code"] != 0:
        raise HTTPException(status_code=500, detail=payload)
    return payload


@app.post("/tasks/{task_id}/export")
def export_task(task_id: str, request: ExportTaskRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    return execute_export_task(task_id, repo_dir, request.source, request.svg_snapshot, request.no_merge)


@app.post("/tasks/{task_id}/run-pipeline")
def run_pipeline(task_id: str, request: RunPipelineRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, _, project_dir = ensure_task(repo_dir, task_id)

    before_state = build_project_state(project_dir)
    steps: list[dict[str, Any]] = []

    if not before_state["design_spec_exists"] or not before_state["spec_lock_exists"]:
        steps.append(
            {
                "step": "strategist",
                "result": execute_strategist(task_id, repo_dir, request.strategist_model),
            }
        )

    readiness_after_strategist = build_export_readiness(project_dir)
    if "svg_output/*.svg" in readiness_after_strategist["missing_requirements"]:
        svg_result = execute_generate_svgs(task_id, repo_dir, request.svg_model, request.max_pages)
        steps.append({"step": "generate-svgs", "result": svg_result})
        if svg_result["status"] != "ok":
            payload = {
                "status": "error",
                "task_id": task_id,
                "message": "SVG generation did not complete for all pages.",
                "steps": steps,
                "project_state": build_project_state(project_dir),
            }
            persist_last_run(paths, payload)
            raise HTTPException(status_code=500, detail=payload)

    export_result = execute_export_task(task_id, repo_dir, request.source, request.svg_snapshot, request.no_merge)
    steps.append({"step": "export", "result": export_result})

    payload = {
        "status": "ok",
        "task_id": task_id,
        "step": "run-pipeline",
        "steps": steps,
        "project_state_before": before_state,
        "project_state_after": build_project_state(project_dir),
        "new_exports": export_result["new_exports"],
        "all_exports": export_result["all_exports"],
    }
    persist_last_run(paths, payload)
    return payload


@app.post("/tasks/{task_id}/strategist")
def run_strategist(task_id: str, request: StrategistRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    return execute_strategist(task_id, repo_dir, request.model)


@app.post("/tasks/{task_id}/generate-svgs")
def generate_svgs(task_id: str, request: GenerateSvgsRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    return execute_generate_svgs(task_id, repo_dir, request.model, request.max_pages)


@app.get("/tasks/{task_id}/readiness")
def task_readiness(task_id: str, repo_dir: str) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    _, _, project_dir = ensure_task(repo, task_id)
    readiness = build_export_readiness(project_dir)
    return {
        "task_id": task_id,
        "project_dir": str(project_dir),
        **readiness,
    }


@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str, repo_dir: str) -> TaskStatusResponse:
    repo = ensure_repo_dir(Path(repo_dir))
    paths, metadata, _ = ensure_task(repo, task_id)
    return TaskStatusResponse(
        task_id=task_id,
        project_dir=metadata["project_dir"],
        prompt_file=metadata["prompt_file"],
        metadata_file=str(paths["metadata_file"]),
        run_log_file=str(paths["run_log_file"]),
        user_prompt=metadata["user_prompt"],
        last_run=load_json(paths["result_file"]),
    )


@app.get("/tasks")
def list_tasks(repo_dir: str) -> dict[str, Any]:
    repo = ensure_repo_dir(Path(repo_dir))
    items: list[TaskListItem] = []
    if not STATE_DIR.exists():
        return {"items": []}

    for state_dir in sorted((p for p in STATE_DIR.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        task_id = state_dir.name
        if task_id.startswith("_"):
            continue
        paths = task_paths(repo, task_id)
        metadata = load_json(paths["metadata_file"])
        if metadata is None:
            continue
        items.append(
            TaskListItem(
                task_id=task_id,
                project_dir=metadata.get("project_dir", ""),
                created_at=metadata.get("created_at"),
                user_prompt=metadata.get("user_prompt", ""),
                has_result=paths["result_file"].exists(),
                has_plan=paths["plan_file"].exists(),
            )
        )
    return {"items": [item.model_dump() for item in items]}


@app.get("/tasks/{task_id}/artifacts", response_model=TaskArtifactsResponse)
def get_task_artifacts(task_id: str, repo_dir: str) -> TaskArtifactsResponse:
    repo = ensure_repo_dir(Path(repo_dir))
    paths, metadata, project_dir = ensure_task(repo, task_id)
    return TaskArtifactsResponse(
        task_id=task_id,
        project_dir=metadata["project_dir"],
        exports=list_exports(project_dir),
        images=list_images(project_dir),
        available_files=available_task_files(paths),
        last_run=load_json(paths["result_file"]),
    )


@app.get("/tasks/{task_id}/files/{file_key}", response_model=TaskFileResponse)
def get_task_file(task_id: str, file_key: str, repo_dir: str) -> TaskFileResponse:
    repo = ensure_repo_dir(Path(repo_dir))
    paths, _, _ = ensure_task(repo, task_id)
    if file_key not in ALLOWED_TASK_FILES:
        raise HTTPException(status_code=400, detail=f"Unsupported file key: {file_key}")
    file_path = paths[ALLOWED_TASK_FILES[file_key]]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found for key: {file_key}")
    return TaskFileResponse(
        task_id=task_id,
        file_key=file_key,
        path=str(file_path),
        content=file_path.read_text(encoding="utf-8"),
    )


@app.get("/files/download")
def download_file(repo_dir: str, path: str):
    repo = ensure_repo_dir(Path(repo_dir))
    file_path = resolve_download_path(repo, path)
    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=file_path.name,
    )
