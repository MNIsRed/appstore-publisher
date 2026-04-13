"""Utility helpers: signing, hashing, file operations."""

import hashlib
import base64
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def md5_file(path: Path, chunk_size: int = 8192) -> str:
    """Compute MD5 hex digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def md5_sign(params: dict, secret: str) -> str:
    """Generate MD5 signature from sorted params + secret suffix."""
    sorted_params = sorted(params.items())
    sign_str = "&".join(f"{k}={v}" for k, v in sorted_params) + secret
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def rsa_sign_md5(params: dict, private_key_pem: str) -> str:
    """Sign concatenated param values with RSA private key (PKCS1v15 + MD5).

    Used by Tencent Yingyongbao: MD5 all param values, then RSA-sign.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    # Concatenate all param values
    values = "".join(str(v) for v in sorted(params.values()))
    md5_digest = hashlib.md5(values.encode("utf-8")).digest()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    signature = private_key.sign(
        md5_digest,
        padding.PKCS1v15(),
        hashes.MD5(),
    )
    return base64.b64encode(signature).decode("utf-8")


def load_pem_key(path: Path) -> str:
    """Read a PEM key file and return its contents as string."""
    return path.read_text(encoding="utf-8")


def hmac_sha256_sign(params: dict, secret: str) -> str:
    """Generate HMAC-SHA256 signature from sorted params (used by Vivo)."""
    import hmac
    sorted_params = sorted(params.items())
    sign_str = "&".join(f"{k}={v}" for k, v in sorted_params)
    return hmac.new(
        secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def retry_request(func, max_retries: int = 3, backoff_factor: float = 1.0):
    """Retry a function with exponential backoff."""
    import time
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exc = e
            wait = backoff_factor * (2 ** attempt)
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]
