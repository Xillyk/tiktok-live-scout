"""Load config.yaml + .env into a Config object.

Multi-profile model: a TikTok account = a profile. Each profile has its
own persisted Chromium user_data_dir (cookies) and its own list of
targets to watch. Exactly one profile is active per scout run.
"""
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
class Profile:
    name: str
    user_data_dir: Path
    targets: list[str] = field(default_factory=list)


@dataclass
class Config:
    profiles: list[Profile] = field(default_factory=list)
    poll_interval_seconds: int = 60
    headless: bool = True
    log_file: Path = Path("./logs/scout.log")
    debug_dump_dir: Path = Path("./data/debug")
    discord_webhook_url: str | None = None
    database_url: str | None = None
    web: WebConfig = field(default_factory=WebConfig)

    def profile(self, name: str) -> Profile:
        for p in self.profiles:
            if p.name == name:
                return p
        raise KeyError(f"unknown profile {name!r} (have: {[p.name for p in self.profiles]})")


def load(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw = yaml.safe_load(Path(path).read_text())

    profiles: list[Profile] = []
    for p in raw.get("profiles") or []:
        profiles.append(
            Profile(
                name=str(p["name"]),
                user_data_dir=Path(p.get("user_data_dir") or f"./data/auth_{p['name']}"),
                targets=[str(t).lstrip("@") for t in p.get("targets") or []],
            )
        )

    # Back-compat: top-level `targets:` becomes a profile named "default".
    if not profiles and raw.get("targets"):
        profiles.append(
            Profile(
                name="default",
                user_data_dir=Path(raw.get("user_data_dir") or "./data/auth"),
                targets=[str(t).lstrip("@") for t in raw["targets"]],
            )
        )

    web_raw = raw.get("web") or {}
    return Config(
        profiles=profiles,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        headless=bool(raw.get("headless", True)),
        log_file=Path(raw.get("log_file", "./logs/scout.log")),
        debug_dump_dir=Path(raw.get("debug_dump_dir", "./data/debug")),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        database_url=os.getenv("DATABASE_URL") or None,
        web=WebConfig(
            host=web_raw.get("host", "127.0.0.1"),
            port=int(web_raw.get("port", 8766)),
        ),
    )
