#!/usr/bin/env python3
"""
Agnes AI image generation backend.

Configuration keys:
  AGNES_API_KEY     (required)
  AGNES_BASE_URL    (optional)
  AGNES_MODEL       (optional)

This backend uses Agnes's OpenAI-compatible images API under a provider-specific
env namespace, so users can select IMAGE_BACKEND=agnes directly without
reusing OPENAI_* variables.
"""

import sys

if __name__ == "__main__":
    print(__doc__)
    print("Use via: python3 skills/ppt-master/scripts/image_gen.py \"prompt\" --backend agnes")
    raise SystemExit(0 if any(arg in {"-h", "--help", "help"} for arg in sys.argv[1:]) else 1)

import os
import threading
from contextlib import contextmanager

import image_backends.backend_openai as openai_backend


DEFAULT_MODEL = "agnes-image-2.1-flash"
DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"

_ENV_LOCK = threading.Lock()
_OPENAI_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_SIZE_PRESET",
    "OPENAI_RESPONSE_FORMAT",
    "OPENAI_QUALITY",
    "OPENAI_OUTPUT_FORMAT",
    "OPENAI_OUTPUT_COMPRESSION",
    "OPENAI_BACKGROUND",
    "OPENAI_MODERATION",
)
_AGNES_TO_OPENAI = {
    "AGNES_API_KEY": "OPENAI_API_KEY",
    "AGNES_BASE_URL": "OPENAI_BASE_URL",
    "AGNES_MODEL": "OPENAI_MODEL",
    "AGNES_SIZE_PRESET": "OPENAI_SIZE_PRESET",
    "AGNES_RESPONSE_FORMAT": "OPENAI_RESPONSE_FORMAT",
    "AGNES_QUALITY": "OPENAI_QUALITY",
    "AGNES_OUTPUT_FORMAT": "OPENAI_OUTPUT_FORMAT",
    "AGNES_OUTPUT_COMPRESSION": "OPENAI_OUTPUT_COMPRESSION",
    "AGNES_BACKGROUND": "OPENAI_BACKGROUND",
    "AGNES_MODERATION": "OPENAI_MODERATION",
}


@contextmanager
def _mapped_openai_env():
    """Temporarily map AGNES_* variables onto OPENAI_* for the shared backend."""
    with _ENV_LOCK:
        original = {key: os.environ.get(key) for key in _OPENAI_ENV_KEYS}
        try:
            api_key = os.environ.get("AGNES_API_KEY")
            if not api_key:
                raise ValueError(
                    "No API key found. Set AGNES_API_KEY in the current environment or a .env file."
                )

            os.environ["OPENAI_API_KEY"] = api_key
            os.environ["OPENAI_BASE_URL"] = os.environ.get("AGNES_BASE_URL", DEFAULT_BASE_URL)
            os.environ["OPENAI_MODEL"] = os.environ.get("AGNES_MODEL", DEFAULT_MODEL)
            os.environ["OPENAI_RESPONSE_FORMAT"] = os.environ.get("AGNES_RESPONSE_FORMAT", "omit")

            for agnes_key, openai_key in _AGNES_TO_OPENAI.items():
                if agnes_key in {"AGNES_API_KEY", "AGNES_BASE_URL", "AGNES_MODEL"}:
                    continue
                value = os.environ.get(agnes_key)
                if value:
                    os.environ[openai_key] = value

            yield
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def generate(prompt: str,
             aspect_ratio: str = "1:1", image_size: str = "1K",
             output_dir: str = None, filename: str = None,
             model: str = None, max_retries: int = openai_backend.MAX_RETRIES) -> str:
    """Generate an image via Agnes using the shared OpenAI-compatible implementation."""
    with _mapped_openai_env():
        return openai_backend.generate(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            output_dir=output_dir,
            filename=filename,
            model=model or os.environ.get("AGNES_MODEL") or DEFAULT_MODEL,
            max_retries=max_retries,
        )
