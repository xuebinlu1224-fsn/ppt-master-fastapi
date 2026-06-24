#!/usr/bin/env python3
"""
PPT Master - Template Preview Renderer

Renders the first 5 SVG pages of a template directory to 1280x720 PNGs.
Reuses the Playwright-based rendering approach from visual_review.py so the
preview matches the live-preview browser view (inlined <use data-icon>,
resolved <image href>, full font fallback including CJK).

Usage:
    # Render an example template
    python3 scripts/render_template_preview.py <svg_dir> -o <output_dir>

    # Render a deck template
    python3 scripts/render_template_preview.py templates/decks/中国电信

    # Specify pages to render (default: first 5)
    python3 scripts/render_template_preview.py <svg_dir> --pages 3

Output:
    PNG files named preview_01.png, preview_02.png, ... in <output_dir>.

Notes:
    - This script does NOT start the live-preview server; the caller is
      expected to have one running, or the rendering will fail gracefully.
    - The caller (e.g. app.py) is responsible for starting the server before
      invoking this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path


ALL_BG_THRESHOLD = 0.99
DEFAULT_PAGES = 5
DEFAULT_VIEWPORT_W = 1280
DEFAULT_VIEWPORT_H = 720


def _safe_print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@contextmanager
def file_lock(lock_path: Path, timeout: float = 30.0):
    """POSIX advisory lock via fcntl. Falls back to lockless on Windows."""
    try:
        import fcntl
    except ImportError:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fp = open(lock_path, 'w')
    deadline = time.monotonic() + timeout
    while True:
        try:
            import fcntl as _fcntl
            _fcntl.flock(fp.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fp.close()
                raise TimeoutError(f'render lock contended for {timeout}s at {lock_path}')
            time.sleep(0.1)
    try:
        fp.write(str(os.getpid()))
        fp.flush()
        yield
    finally:
        try:
            import fcntl as _fcntl
            _fcntl.flock(fp.fileno(), _fcntl.LOCK_UN)
        except Exception:
            pass
        fp.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def is_all_background(png_bytes: bytes) -> bool:
    """Histogram check: quantize each channel to 4 bits, count dominant bucket.
    Returns True only when the PNG is essentially monochrome (blank render)."""
    try:
        from PIL import Image
    except ImportError:
        return False

    import io
    img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
    pixels = list(img.getdata())
    total = len(pixels)
    if total == 0:
        return True
    counts: dict = {}
    for r, g, b in pixels:
        key = (r >> 4, g >> 4, b >> 4)
        counts[key] = counts.get(key, 0) + 1
    dominant = max(counts.values())
    return dominant / total >= ALL_BG_THRESHOLD


def fetch_slide_text(server_url: str, page_name: str, timeout: float = 5.0) -> int:
    """Probe that the server can return the slide."""
    url = f"{server_url.rstrip('/')}/api/slide/{page_name}"
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    if 'content' not in payload:
        raise RuntimeError(f'unexpected response shape from {url}: {payload!r}')
    return len(payload['content'])


def check_server(server_url: str) -> None:
    """Probe server liveness via /api/slides."""
    url = f"{server_url.rstrip('/')}/api/slides"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f'{url} returned HTTP {resp.status}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'live-preview server not reachable at {server_url}: {e}')


def render_pages(
    server_url: str,
    pages: list[str],
    output_dir: Path,
    prefix: str = "preview",
) -> list[dict]:
    """Render pages to PNG files. Output: <output_dir>/<prefix>_01.png, etc.

    Each page is named based on its position in the input list (1-indexed).
    """
    from playwright.sync_api import sync_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    inject_js = """
async (pageName) => {
    const res = await fetch('/api/slide/' + pageName + '?_=' + Date.now());
    if (!res.ok) throw new Error('fetch /api/slide/' + pageName + ' returned ' + res.status);
    const data = await res.json();
    document.documentElement.innerHTML =
        '<head><style>html,body{margin:0;padding:0;background:#0E1116;overflow:hidden}'
        + ' svg{display:block;width:1280px;height:720px}</style></head>'
        + '<body>' + data.content + '</body>';
    return { len: data.content.length };
}
"""

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                viewport={'width': DEFAULT_VIEWPORT_W, 'height': DEFAULT_VIEWPORT_H}
            )
            for idx, page_name in enumerate(pages, start=1):
                rec: dict = {
                    'page': page_name,
                    'index': idx,
                    'ok': False,
                }
                try:
                    fetch_slide_text(server_url, page_name)
                except urllib.error.URLError as e:
                    rec['error'] = f'server_unreachable: {e!r}'
                    records.append(rec)
                    continue
                except Exception as e:  # noqa: BLE001
                    rec['error'] = f'{type(e).__name__}: {e}'
                    records.append(rec)
                    continue

                stem = page_name[:-4] if page_name.endswith('.svg') else page_name
                out_name = f'{prefix}_{idx:02d}.png'
                out_path = output_dir / out_name

                try:
                    pg = context.new_page()
                    pg.goto(server_url, wait_until='domcontentloaded')
                    pg.evaluate(inject_js, page_name)
                    pg.wait_for_timeout(100)
                    png_bytes = pg.screenshot(type='png', full_page=False)
                    pg.close()

                    out_path.write_bytes(png_bytes)
                    rec['ok'] = True
                    rec['path'] = str(out_path)
                    rec['bytes'] = len(png_bytes)
                    rec['all_background'] = is_all_background(png_bytes)
                except Exception as e:  # noqa: BLE001
                    rec['error'] = f'{type(e).__name__}: {e}'
                records.append(rec)
        finally:
            browser.close()

    return records


def discover_svg_pages(svg_dir: Path, max_count: int) -> list[str]:
    """Return up to max_count SVG files from svg_dir, sorted."""
    if not svg_dir.is_dir():
        raise FileNotFoundError(f'no SVG dir: {svg_dir}')
    all_svgs = sorted(p.name for p in svg_dir.glob('*.svg'))
    return all_svgs[:max_count]


def render_template_preview(
    svg_dir: Path,
    output_dir: Path,
    server_url: str = "http://localhost:5050",
    max_pages: int = DEFAULT_PAGES,
    force: bool = False,
    lock_timeout: float = 30.0,
) -> dict:
    """Render up to max_pages preview PNGs from svg_dir to output_dir.

    Skips already-rendered files unless force=True.
    Returns a summary dict with ok/fail counts.
    """
    svg_dir = svg_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine which pages to render
    try:
        all_pages = discover_svg_pages(svg_dir, max_pages)
    except FileNotFoundError as e:
        return {
            'ok': False,
            'error': str(e),
            'rendered': 0,
            'failed': 0,
        }

    if not all_pages:
        return {
            'ok': False,
            'error': f'no SVG files in {svg_dir}',
            'rendered': 0,
            'failed': 0,
        }

    pages_to_render: list[tuple[int, str]] = []
    skipped: list[str] = []
    for idx, page_name in enumerate(all_pages, start=1):
        out_name = f'preview_{idx:02d}.png'
        out_path = output_dir / out_name
        if out_path.exists() and not force:
            skipped.append(str(out_path))
            continue
        pages_to_render.append((idx, page_name))

    if not pages_to_render:
        return {
            'ok': True,
            'rendered': 0,
            'failed': 0,
            'skipped': len(skipped),
            'pages': [],
        }

    # Check server
    try:
        check_server(server_url)
    except RuntimeError as e:
        return {
            'ok': False,
            'error': str(e),
            'rendered': 0,
            'failed': 0,
        }

    # Render
    page_names = [p[1] for p in pages_to_render]
    lock_path = output_dir / '.render.lock'

    try:
        with file_lock(lock_path, timeout=lock_timeout):
            try:
                records = render_pages(server_url, page_names, output_dir)
            except Exception as e:  # noqa: BLE001
                _safe_print(f'browser session failed: {type(e).__name__}: {e}')
                return {
                    'ok': False,
                    'error': f'browser_session_failed: {type(e).__name__}: {e}',
                    'rendered': 0,
                    'failed': len(page_names),
                }
    except TimeoutError as e:
        return {
            'ok': False,
            'error': str(e),
            'rendered': 0,
            'failed': 0,
        }

    rendered_count = sum(1 for r in records if r['ok'])
    failed_count = sum(1 for r in records if not r['ok'])

    for rec in records:
        if not rec['ok']:
            _safe_print(f"[FAIL] {rec['page']}: {rec.get('error')}")
        elif rec.get('all_background'):
            _safe_print(f"[WARN] {rec['page']}: PNG rendered but is all-background")

    return {
        'ok': failed_count == 0,
        'rendered': rendered_count,
        'failed': failed_count,
        'skipped': len(skipped),
        'pages': records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Render template SVGs to PNG previews.',
    )
    parser.add_argument(
        'svg_dir', type=Path,
        help='Path to directory containing SVG files',
    )
    parser.add_argument(
        '-o', '--output', type=Path, default=None,
        help='Output directory for PNGs (default: <svg_dir>/../.preview)',
    )
    parser.add_argument(
        '--server-url', default='http://localhost:5050',
        help='Live-preview server URL (default: http://localhost:5050)',
    )
    parser.add_argument(
        '--pages', type=int, default=DEFAULT_PAGES,
        help=f'Maximum number of pages to render (default: {DEFAULT_PAGES})',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Re-render even if preview files already exist',
    )
    parser.add_argument(
        '--lock-timeout', type=float, default=30.0,
        help='Seconds to wait for render lock (default: 30)',
    )
    args = parser.parse_args()

    svg_dir = args.svg_dir.resolve()
    if not svg_dir.is_dir():
        _safe_print(f'SVG dir not found: {svg_dir}')
        return 2

    output_dir = args.output
    if output_dir is None:
        output_dir = svg_dir.parent / '.preview'
    output_dir = output_dir.resolve()

    # Check playwright
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _safe_print(
            'playwright not installed. Install with:\n'
            '    pip install playwright\n'
            '    python3 -m playwright install chromium'
        )
        return 3

    summary = render_template_preview(
        svg_dir=svg_dir,
        output_dir=output_dir,
        server_url=args.server_url,
        max_pages=args.pages,
        force=args.force,
        lock_timeout=args.lock_timeout,
    )

    print(json.dumps({
        'svg_dir': str(svg_dir),
        'output_dir': str(output_dir),
        **summary,
    }, indent=2, ensure_ascii=False))

    if not summary.get('ok'):
        return 4 if summary.get('failed', 0) > 0 else 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
