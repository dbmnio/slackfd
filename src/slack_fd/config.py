"""Load bot/app tokens from env or config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from slack_fd.paths import default_data_dir


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str | None
    app_token: str | None
    data_dir: Path

    @property
    def slack_ready(self) -> bool:
        return bool(self.bot_token and self.app_token)


def load_config(data_dir: Path | None = None) -> Config:
    root = (data_dir or default_data_dir()).resolve()
    file_vals = _read_toml(root / "config.toml")

    bot = os.environ.get("SLACK_BOT_TOKEN") or file_vals.get("bot_token")
    app = os.environ.get("SLACK_APP_TOKEN") or file_vals.get("app_token")
    return Config(
        bot_token=_clean(bot),
        app_token=_clean(app),
        data_dir=root,
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_toml(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key in ("bot_token", "app_token"):
        val = data.get(key)
        if isinstance(val, str):
            out[key] = val
    return out
