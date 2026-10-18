"""Shared permanent vault still paths, image signatures and atomic persistence.

No provider, planner, session or database imports. The legacy ladder and native
worker use this same store; credentials and ownership stay with their callers.
"""
from pathlib import Path
import os
import tempfile

MAX_STILL_BYTES = 8 * 1024 * 1024
THUMB_DIR = Path(os.environ.get("VAULT_THUMB_DIR", "/tmp/of-relay-vault-thumbs"))
POSTER_DIR = Path(os.environ.get("VAULT_POSTER_DIR", "/tmp/of-relay-vault-posters"))
IMAGE_DIR = Path(os.environ.get("VAULT_IMAGE_DIR", "/tmp/of-relay-vault-images"))


def image_path(account_id: str, media_id: int, *, root: Path | None = None) -> Path:
    return (IMAGE_DIR if root is None else root) / account_id / f"{media_id}.jpg"


def image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
    return None


def write(p: Path, data: bytes) -> bool:
    """Atomic, private sibling temp + rename; a failure never leaves a partial file.

    Unique temporary names also allow threads warming the same still to race
    without truncating or deleting another writer's temporary file.
    """
    temporary = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=p.parent, prefix=f".{p.name}.", suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, p)
        return True
    except Exception:
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
        return False
