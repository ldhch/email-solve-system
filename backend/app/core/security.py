"""Password hashing (bcrypt), JWT session helpers (M-16) and Fernet
secrets-at-rest (M-20, TECH 6.7).

Phase 4 (M-20): sensitive values from `.env` can be encrypted into
``data/secrets.bin`` with a Fernet key (``ENCRYPTION_KEY``). The runtime
``get_settings()`` decrypts the file and overrides the matching settings
fields; a missing file / missing key / bad key falls back to `.env` so local
development and tests are unaffected.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from app.config import Settings

SECRET_FIELDS = [
    "email_password",
    "deepseek_api_key",
    "openai_api_key",
    "secret_key",
    "agent_service_token",
]

# In-memory logout denylist: stateless JWTs are otherwise impossible to revoke.
# Single-process app, so this is enough; the list clears on restart (the
# session cookie is short-lived anyway, 24h).
_REVOKED_JTIS: set[str] = set()


def hash_password(password: str) -> str:
    """Return a bcrypt hash with cost 12 (TECH 6.7)."""

    if not password:
        raise ConfigurationError("Owner password must not be empty")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(settings: Settings, user_id: int, username: str, role: str) -> str:
    """Create an HS256 JWT with a `jti` so logout can revoke it."""

    if not settings.secret_key:
        raise ConfigurationError("SECRET_KEY is required for owner login (set it in .env)")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": secrets.token_hex(8),
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_expire_seconds),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Decode + validate a JWT; raises `jwt.PyJWTError` on any failure."""

    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    if payload.get("jti") in _REVOKED_JTIS:
        raise jwt.InvalidTokenError("token has been revoked by logout")
    return payload


def revoke_access_token(jti: str | None) -> None:
    if jti:
        _REVOKED_JTIS.add(jti)


# ---------- Fernet secrets-at-rest (M-20) ----------


def generate_encryption_key() -> str:
    """Generate a fresh Fernet key (base64 URL-safe, 32 random bytes)."""

    return Fernet.generate_key().decode()


def default_secrets_file(settings=None) -> Path:
    """Return the secrets-at-rest path, always next to the data root.

    With runtime settings available the file lives beside the attachments
    directory (`<DATA_DIR>/secrets.bin`), which is the same directory as the
    SQLite DB in both local dev (repo-root `data/`) and the container
    (`/app/data`, the appdata volume). Without settings it falls back to the
    repo-root `data/` directory for CLI use outside a configured context.
    """

    attachment_dir = getattr(settings, "attachment_dir", None)
    if attachment_dir:
        return Path(attachment_dir).parent / "secrets.bin"
    from app.config import REPO_ROOT

    return REPO_ROOT / "data" / "secrets.bin"


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            "ENCRYPTION_KEY is invalid; generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def encrypt_secrets(values: dict[str, str], key: str) -> dict[str, str]:
    """Encrypt a {settings_field: plaintext} mapping into Fernet tokens."""

    fernet = _fernet(key)
    return {
        field: fernet.encrypt(str(value).encode("utf-8")).decode("utf-8")
        for field, value in values.items()
        if field and value
    }


def decrypt_secrets(ciphertext: dict[str, str], key: str) -> dict[str, str]:
    """Decrypt a Fernet token mapping back into plaintext values."""

    fernet = _fernet(key)
    plain: dict[str, str] = {}
    for field, token in ciphertext.items():
        try:
            plain[field] = fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ConfigurationError(
                f"Failed to decrypt secrets.bin field '{field}' "
                "(wrong ENCRYPTION_KEY?)"
            ) from exc
    return plain


def write_secrets_file(
    values: dict[str, str],
    key: str,
    path: Path | None = None,
) -> Path:
    """Encrypt `values` and persist them as JSON at `data/secrets.bin`."""

    target = path or default_secrets_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    encrypted = encrypt_secrets(values, key)
    # Restrict access to the process owner; the file contains secrets.
    target.write_text(json.dumps(encrypted, indent=2), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - Windows / restricted FS
        pass
    return target


def read_secrets_file(key: str, path: Path | None = None) -> dict[str, str]:
    """Read and decrypt `data/secrets.bin`; empty dict when the file is absent."""

    target = path or default_secrets_file()
    if not target.exists():
        return {}
    raw = json.loads(target.read_text(encoding="utf-8"))
    return decrypt_secrets(raw, key)
