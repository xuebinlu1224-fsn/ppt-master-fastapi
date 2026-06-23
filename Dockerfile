FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# Runtime libs for ppt-master scripts: cairo/pango for SVG -> PNG fallback
# (cairosvg / reportlab), libxml2/libxslt for lxml, libjpeg/zlib for Pillow,
# and Noto CJK so generated SVGs render CJK glyphs. No build tools — every
# pip wheel we need is available as a prebuilt wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
        zlib1g \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Outer service dependencies first (cache-friendly).
# Requires Docker BuildKit (DOCKER_BUILDKIT=1 or buildx default in recent
# Docker) for --mount=type=cache; falls back gracefully without it.
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.txt

# Vendored ppt-master + its own requirements (heavy: python-pptx, svglib,
# reportlab, PyMuPDF, mammoth, openpyxl, Pillow, numpy, …).
COPY ppt-master/ /app/ppt-master/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/ppt-master/skills/ppt-master/requirements.txt

# Outer service code, static UI, and the orchestrator entrypoint.
COPY app.py /app/app.py
COPY ui/ /app/ui/

# Image-generation tooling lives under ppt-master/.env (read by image_gen.py
# at run-time). Mount it via `env_file:` in docker-compose.yml; do NOT bake
# any real keys into the image.
# The vendored ppt-master/.env in this repo currently contains a leaked
# AGNES_API_KEY; rotate it and supply a clean file via env_file.

EXPOSE 8080

# Single FastAPI process: serves the API and the static console at /.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
