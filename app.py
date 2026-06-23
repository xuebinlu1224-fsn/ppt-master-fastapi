#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".service_tasks"
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"

app = FastAPI(title="ppt-master-agent", version="0.2.0")


class PrepareTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str = Field(..., description="Absolute path to the local ppt-master repository.")
    prompt: str = Field(..., description="Deck generation instruction.")
    task_id: Optional[str] = Field(default=None, description="Optional project id under projects/.")
    canvas_format: str = Field(default="ppt169", description="ppt-master canvas format passed to project_manager.py init.")


class AgentPlanRequest(BaseModel):
    repo_dir: str
    task_id: str
    model: str = Field(default=DEFAULT_DEEPSEEK_MODEL)
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
    model: str = Field(default=DEFAULT_DEEPSEEK_MODEL)


class GenerateSvgsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_dir: str
    task_id: str
    model: str = Field(default=DEFAULT_DEEPSEEK_MODEL)
    max_pages: int = Field(default=30, ge=1, le=100)


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


def service_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return (
        os.environ.get(name)
        or read_env_file(BASE_DIR / ".env").get(name)
        or read_env_file(BASE_DIR / "agent.env").get(name)
        or default
    )


def build_task_id(custom_task_id: Optional[str]) -> str:
    if custom_task_id:
        return custom_task_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{uuid.uuid4().hex[:6]}"


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
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        command,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    append_run_log(paths["run_log_file"], label, command, completed)
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


def _get_deepseek_client() -> "OpenAI":
    api_key = service_env("DEEPSEEK_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DEEPSEEK_API_KEY in service environment or .env.")
    base_url = service_env("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Missing dependency: openai") from exc
    return OpenAI(api_key=api_key, base_url=base_url)


def call_deepseek_strategist(
    task_prompt: str,
    canvas_format: str,
    project_state: dict[str, Any],
    model: str,
) -> dict[str, str]:
    client = _get_deepseek_client()
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
        },
        ensure_ascii=False,
        indent=2,
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        stream=False,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek strategist did not return valid JSON: {exc}") from exc
    if not isinstance(parsed.get("design_spec_md"), str) or not isinstance(parsed.get("spec_lock_md"), str):
        raise HTTPException(status_code=500, detail="Strategist response missing design_spec_md or spec_lock_md fields.")
    return {
        "design_spec_md": parsed["design_spec_md"],
        "spec_lock_md": parsed["spec_lock_md"],
    }


def parse_spec_lock_pages(spec_lock_md: str) -> list[dict[str, Optional[str]]]:
    import re

    def strip_val(v: str) -> str:
        return v.strip().strip('"').strip("'")

    pages: dict[str, dict[str, Optional[str]]] = {}

    # Parse page_rhythm: P01: "cover"  (primary source of page IDs)
    rhythm_section = _extract_section(spec_lock_md, "page_rhythm")
    if rhythm_section:
        for match in re.finditer(r"^\s*(P\d+):\s*(.+)", rhythm_section, re.MULTILINE):
            pid = match.group(1)
            rhythm_val = strip_val(match.group(2))
            if pid not in pages:
                pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}
            pages[pid]["rhythm"] = rhythm_val

    # Parse page_layouts: extract layout names (layout_cover, etc.) without P prefix
    layouts_section = _extract_section(spec_lock_md, "page_layouts")
    layout_names: list[str] = []
    if layouts_section:
        layout_names = re.findall(r"^\s*(\w+):\s*$", layouts_section, re.MULTILINE)
        # Remove known non-layout entries
        layout_names = [ln for ln in layout_names if ln not in ("type",)]

    # Match layouts to pages by trying rhythm_value in layout name
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

    # Parse page_charts: try P02_xxx match
    charts_section = _extract_section(spec_lock_md, "page_charts")
    if charts_section:
        chart_names = re.findall(r"^\s*(P\d+_\w+):\s*$", charts_section, re.MULTILINE)
        for pid, page in pages.items():
            for cn in chart_names:
                if cn.startswith(pid + "_"):
                    page["chart"] = cn
                    break

    # Fallback: extract page IDs from design_spec style content_outline section
    if not pages:
        outline = _extract_section(spec_lock_md, "content_outline")
        if outline:
            for match in re.finditer(r"^\s*(P\d+)", outline, re.MULTILINE):
                pid = match.group(1)
                if pid not in pages:
                    pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}

    result = sorted(pages.values(), key=lambda p: p["page"])
    if not result:
        # Last resort: search for any P\d+ pattern in the entire spec
        all_page_ids = sorted(set(re.findall(r"\b(P\d+)\b", spec_lock_md)))
        for pid in all_page_ids:
            pages[pid] = {"page": pid, "rhythm": None, "layout": None, "chart": None}
        result = sorted(pages.values(), key=lambda p: p["page"])
    return result


def _extract_section(md_text: str, section_name: str) -> str:
    import re
    # Match section_name: or ## section_name (with optional # prefix, optional colon)
    pattern = re.compile(rf"^(?:#+\s*)?{re.escape(section_name)}\s*:?\s*$", re.MULTILINE)
    match = pattern.search(md_text)
    if not match:
        return ""
    start = match.end()
    # End at next non-indented key: line or # header
    next_key = re.compile(r"^(?:#+\s*)?\w[\w\s]*:?\s*$", re.MULTILINE)
    next_match = next_key.search(md_text, start)
    end = next_match.start() if next_match else len(md_text)
    return md_text[start:end]


def call_deepseek_svg_page(
    spec_lock_md: str,
    page_meta: dict[str, Any],
    design_spec_md: str,
    model: str,
) -> str:
    client = _get_deepseek_client()
    page_id = page_meta.get("page", "unknown")
    rhythm = page_meta.get("rhythm", "dense")
    layout = page_meta.get("layout", "")
    chart = page_meta.get("chart", "")
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
        f"{chart_hint}\n\n"
        "=== SPEC LOCK (execution contract) ===\n"
        f"{spec_lock_md}\n\n"
        "=== DESIGN SPEC (content outline for this page) ===\n"
        f"{design_spec_md}\n\n"
        "Generate the SVG for this page now. Output only the <svg> element."
    )
    response = client.chat.completions.create(
        model=model,
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


def call_deepseek_plan(request: AgentPlanRequest, repo_dir: Path, metadata: dict[str, Any], project_state: dict[str, Any]) -> dict[str, Any]:
    api_key = service_env("DEEPSEEK_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DEEPSEEK_API_KEY in service environment or .env.")
    base_url = service_env("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Missing dependency: openai") from exc

    client = OpenAI(api_key=api_key, base_url=base_url)
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
        model=request.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ],
        stream=False,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek did not return valid JSON: {exc}") from exc
    usage = getattr(response, "usage", None)
    return {
        "provider": "deepseek",
        "model": request.model,
        "base_url": base_url,
        "generated_at": datetime.now().isoformat(),
        "plan": parsed,
        "usage": usage.model_dump() if usage else None,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "ui" / "index.html")


@app.post("/tasks/prepare", response_model=TaskStatusResponse)
def prepare_task(request: PrepareTaskRequest) -> TaskStatusResponse:
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    task_id = build_task_id(request.task_id)
    paths = task_paths(repo_dir, task_id)
    if paths["state_dir"].exists():
        raise HTTPException(status_code=409, detail=f"Task state already exists: {paths['state_dir']}")

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
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
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

    write_task_prompt(request.prompt, project_dir, paths["prompt_file"], task_id)
    metadata = {
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
        "repo_dir": str(repo_dir),
        "project_dir": str(project_dir),
        "prompt_file": str(paths["prompt_file"]),
        "user_prompt": request.prompt.strip(),
        "canvas_format": request.canvas_format,
    }
    paths["metadata_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["metadata_file"].write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist_last_run(paths, {"status": "ok", "step": "prepare", "task_id": task_id})

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
    plan_payload = call_deepseek_plan(request, repo_dir, metadata, project_state)
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
    if request.source:
        export_command.extend(["-s", request.source])
    if request.svg_snapshot:
        export_command.append("--svg-snapshot")
    if request.no_merge:
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


@app.post("/tasks/{task_id}/strategist")
def run_strategist(task_id: str, request: StrategistRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, metadata, project_dir = ensure_task(repo_dir, task_id)
    project_state = build_project_state(project_dir)
    task_prompt = metadata.get("user_prompt", "")
    canvas_format = metadata.get("canvas_format", "ppt169")

    strat_result = call_deepseek_strategist(
        task_prompt=task_prompt,
        canvas_format=canvas_format,
        project_state=project_state,
        model=request.model,
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
    }
    persist_last_run(paths, payload)
    return payload


@app.post("/tasks/{task_id}/generate-svgs")
def generate_svgs(task_id: str, request: GenerateSvgsRequest) -> dict[str, Any]:
    if request.task_id != task_id:
        raise HTTPException(status_code=400, detail="Path task_id does not match request body task_id.")
    repo_dir = ensure_repo_dir(Path(request.repo_dir))
    paths, _, project_dir = ensure_task(repo_dir, task_id)

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

    max_pages = min(len(pages), request.max_pages)
    pages = pages[:max_pages]

    generated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for page_meta in pages:
        page_id = page_meta["page"]
        svg_path = svg_output_dir / f"{page_id.lower()}_{page_meta.get('rhythm', 'slide')}.svg"
        try:
            spec_lock_md = spec_lock_path.read_text(encoding="utf-8")
            svg_text = call_deepseek_svg_page(
                spec_lock_md=spec_lock_md,
                page_meta=page_meta,
                design_spec_md=design_spec_md,
                model=request.model,
            )
            valid, error = validate_minimal_svg(svg_text)
            if not valid:
                spec_lock_md = spec_lock_path.read_text(encoding="utf-8")
                svg_text = call_deepseek_svg_page(
                    spec_lock_md=spec_lock_md,
                    page_meta=page_meta,
                    design_spec_md=design_spec_md,
                    model=request.model,
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
