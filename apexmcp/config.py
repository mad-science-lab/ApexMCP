"""Settings from environment variables. Writes are off unless explicitly enabled."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    pass


# Label prefixes, as regex. Loose on purpose so a typo like "usernane:" still reads as a label.
_USER_LABELS = (r"user\w*", r"login\w*")
_PASS_LABELS = (r"pass\w*",)


def _label(line: str, labels: tuple[str, ...]) -> bool:
    return re.match(rf"^\s*(?:{'|'.join(labels)})\s*[:=]", line, re.IGNORECASE) is not None


def _unlabel(line: str, labels: tuple[str, ...]) -> str:
    m = re.match(rf"^\s*(?:{'|'.join(labels)})\s*[:=]\s*(.*)$", line, re.IGNORECASE)
    return (m.group(1) if m else line).strip()


def _flag(env: Mapping[str, str], key: str) -> bool:
    return env.get(key, "").strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    host: str
    username: str = "admin"
    password: str = field(default="", repr=False)  # never show in logs or tracebacks
    allow_write: bool = False          # outlets and feed cycles
    allow_program_write: bool = False  # outlet programs (needs allow_write too)
    protected: frozenset[str] = frozenset()  # lower-case dids or names, never forced ON/OFF
    backup_dir: Path = Path("backups")
    timeout_seconds: int = 10

    def is_protected(self, did: str, name: str) -> bool:
        return did.lower() in self.protected or name.lower() in self.protected


def load(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    host = env.get("APEX_HOST", "").strip()
    if not host:
        raise ConfigError("APEX_HOST is required, e.g. APEX_HOST=http://apex.local")

    username = env.get("APEX_USERNAME", "admin").strip() or "admin"
    password = env.get("APEX_PASSWORD", "")
    password_file = env.get("APEX_PASSWORD_FILE", "").strip()
    if not password and password_file:
        try:
            lines = [
                ln.strip()
                for ln in Path(password_file).read_text(encoding="utf-8-sig").splitlines()
                if ln.strip()
            ]
        except OSError as exc:
            raise ConfigError(f"cannot read APEX_PASSWORD_FILE: {exc}") from exc
        # One line is the password. Two lines are username then password, and either
        # line may carry a label such as "username: x" or "password=y".
        if len(lines) == 1:
            password = _unlabel(lines[0], _PASS_LABELS)
        elif len(lines) == 2:
            by_label = {
                "user": [_unlabel(ln, _USER_LABELS) for ln in lines if _label(ln, _USER_LABELS)],
                "pass": [_unlabel(ln, _PASS_LABELS) for ln in lines if _label(ln, _PASS_LABELS)],
            }
            if by_label["user"] and by_label["pass"]:
                username, password = by_label["user"][0], by_label["pass"][0]
            else:
                username, password = _unlabel(lines[0], _USER_LABELS), _unlabel(lines[1], _PASS_LABELS)
        elif len(lines) > 2:
            raise ConfigError("APEX_PASSWORD_FILE should hold the password, or username then password")

    allow_write = _flag(env, "APEX_ALLOW_WRITE")
    allow_program_write = _flag(env, "APEX_ALLOW_PROGRAM_WRITE")
    if allow_program_write and not allow_write:
        raise ConfigError("APEX_ALLOW_PROGRAM_WRITE requires APEX_ALLOW_WRITE=true")
    if allow_write and not password:
        raise ConfigError("APEX_ALLOW_WRITE needs APEX_PASSWORD or APEX_PASSWORD_FILE")

    protected = frozenset(
        p.strip().lower() for p in env.get("APEX_PROTECTED_OUTLETS", "").split(",") if p.strip()
    )
    try:
        timeout = int(env.get("APEX_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise ConfigError("APEX_TIMEOUT_SECONDS must be an integer") from exc

    return Settings(
        host=host,
        username=username,
        password=password,
        allow_write=allow_write,
        allow_program_write=allow_program_write,
        protected=protected,
        backup_dir=Path(env.get("APEX_BACKUP_DIR", "backups")),
        timeout_seconds=timeout,
    )
