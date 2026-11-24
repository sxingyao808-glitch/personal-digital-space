"""Local owner authentication primitives.

The service binds to loopback only. Owner access requires a one-time bootstrap
token printed at startup; the resulting signed session cookie is HttpOnly and
SameSite=Strict. Mutating API calls additionally require a CSRF header.
"""

from __future__ import annotations

import hmac
import os
import secrets
import tempfile
from pathlib import Path

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class OwnerSecretError(RuntimeError):
    """The archive's existing signing key is unusable."""


def _read_owner_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        raise OwnerSecretError("无法读取归档会话密钥。请检查数据目录权限或恢复有效密钥。") from None
    if len(value) < 32:
        raise OwnerSecretError("归档会话密钥无效。请恢复有效密钥或按归档维护流程轮换。")
    return value


def ensure_secret(data_dir: str | Path) -> str:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "owner_secret"
    try:
        return _read_owner_secret(path)
    except FileNotFoundError:
        pass

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir,
                                         prefix=".owner-secret-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(secrets.token_hex(32))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return _read_owner_secret(path)
    except OSError:
        raise OwnerSecretError("无法初始化归档会话密钥。请检查数据目录及文件系统。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def token_matches(candidate: str | None, expected: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(str(candidate), str(expected))


def host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header
    if host.startswith("["):
        return False
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower() in ALLOWED_HOSTS


def origin_allowed(origin: str | None, scheme: str, host: str) -> bool:
    """Allow missing Origin, but reject any explicit cross-origin value."""
    if not origin:
        return True
    expected = f"{scheme}://{host}".rstrip("/")
    return origin.rstrip("/").lower() == expected.lower()
