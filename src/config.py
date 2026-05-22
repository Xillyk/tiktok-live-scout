"""Load config.yaml + .env into a single Config object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8766


@dataclass
class Config:
    targets: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 60
    headless: bool = True
    user_data_dir: Path = Path("./data/auth")
    log_file: Path = Path("./logs/scout.log")
    debug_dump_dir: Path = Path("./data/debug")
    discord_webhook_url: str | None = None
    # None means "auto-discover via `docker compose port postgres 5432`".
    # Set DATABASE_URL in .env to override with a fixed DSN.
    database_url: str | None = None
    web: WebConfig = field(default_factory=WebConfig)


def load(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw = yaml.safe_load(Path(path).read_text())

    web_raw = raw.get("web") or {}
    return Config(
        targets=[t.lstrip("@") for t in raw.get("targets", [])],
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        headless=bool(raw.get("headless", True)),
        user_data_dir=Path(raw.get("user_data_dir", "./data/auth")),
        log_file=Path(raw.get("log_file", "./logs/scout.log")),
        debug_dump_dir=Path(raw.get("debug_dump_dir", "./data/debug")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        database_url=os.getenv("DATABASE_URL") or None,
        web=WebConfig(
            host=web_raw.get("host", "127.0.0.1"),
            port=int(web_raw.get("port", 8765)),
        ),
    )
