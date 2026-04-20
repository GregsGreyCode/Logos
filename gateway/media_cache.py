"""Local cache for user-uploaded media (images, audio, documents).

Content arriving from external sources (messaging platforms, web UI
attachments) is downloaded / decoded to ``$HERMES_HOME/{image,audio,
document}_cache/`` so tools can reference the files by a stable local
path. Ephemeral platform URLs (Telegram file URLs expire after ~1h)
can't be trusted to persist through an agent's tool-use loop; caching
gives us a path that does.

Extracted from ``gateway/channels/base.py`` when per-platform adapters
moved into hermes-in-sandbox (LOG-44.3). Logos still caches media
because the web UI + v2 dispatch attachment enrichment run in the
gateway process.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import List, Tuple

from logos_cli.config import get_hermes_home


# ── Image cache ──────────────────────────────────────────────────────

IMAGE_CACHE_DIR = get_hermes_home() / "image_cache"


def get_image_cache_dir() -> Path:
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_CACHE_DIR


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    cache_dir = get_image_cache_dir()
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_image_from_url(url: str, ext: str = ".jpg") -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        return cache_image_from_bytes(response.content, ext)


def cleanup_image_cache(max_age_hours: int = 24) -> int:
    cache_dir = get_image_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ── Audio cache ──────────────────────────────────────────────────────

AUDIO_CACHE_DIR = get_hermes_home() / "audio_cache"


def get_audio_cache_dir() -> Path:
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_CACHE_DIR


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    cache_dir = get_audio_cache_dir()
    filename = f"audio_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_audio_from_url(url: str, ext: str = ".ogg") -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                "Accept": "audio/*,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        return cache_audio_from_bytes(response.content, ext)


# ── Document cache ───────────────────────────────────────────────────

DOCUMENT_CACHE_DIR = get_hermes_home() / "document_cache"

SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def get_document_cache_dir() -> Path:
    DOCUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return DOCUMENT_CACHE_DIR


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """Save document bytes, preserving the human-readable name with a
    unique prefix (``doc_{uuid12}_{original_filename}``)."""
    cache_dir = get_document_cache_dir()
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in (".", ".."):
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)


def cleanup_document_cache(max_age_hours: int = 24) -> int:
    cache_dir = get_document_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ── Media tag extraction ─────────────────────────────────────────────
# Used by send_message_tool to pull MEDIA:<path> directives out of
# agent replies before rendering the text portion.

_MEDIA_PATTERN = re.compile(
    r'''[`"']?MEDIA:\s*(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|\S+)[`"']?'''
)


def extract_media(content: str) -> Tuple[List[Tuple[str, bool]], str]:
    """Extract ``MEDIA:<path>`` tags and ``[[audio_as_voice]]`` directives.

    Returns ``(list of (path, is_voice) pairs, cleaned content)``.
    """
    media: List[Tuple[str, bool]] = []
    cleaned = content

    has_voice_tag = "[[audio_as_voice]]" in content
    cleaned = cleaned.replace("[[audio_as_voice]]", "")

    for match in _MEDIA_PATTERN.finditer(content):
        path = match.group("path").strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
            path = path[1:-1].strip()
        path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
        if path:
            media.append((path, has_voice_tag))

    if media:
        cleaned = _MEDIA_PATTERN.sub('', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    return media, cleaned
