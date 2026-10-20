"""Upload admission limits shared with the existing batch pipeline, without provider imports."""
import os

MAX_FILES = int(os.environ.get("VAULT_UPLOAD_MAX_FILES") or 40)
MAX_BYTES = int(os.environ.get("VAULT_UPLOAD_MAX_MB") or 4000) * 1024 * 1024
MIN_FREE_BYTES = int(os.environ.get("VAULT_UPLOAD_MIN_FREE_MB") or 2048) * 1024 * 1024
CHUNK_BYTES = 8 * 1024 * 1024
