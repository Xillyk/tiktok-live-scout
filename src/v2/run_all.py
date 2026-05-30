"""v2 multi-target launcher — runs every target from config_v2.yaml in a
single asyncio process. Built for cloud deploys (e.g. Render workers are
billed per service, so collapsing N targets into one process is cheaper).

Locally the .command launcher is still preferred because per-target log
files are easier to tail.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from . import db
from .scout import scout_target

log = logging.getLogger("v2.run_all")

CONFIG_PATH = Path("config_v2.yaml")


def load_targets() -> list[str]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"missing {CONFIG_PATH}")
    cfg: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    raw = cfg.get("targets") or []
    targets = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    if not targets:
        raise RuntimeError(f"no targets in {CONFIG_PATH}")
    return targets


async def _amain() -> None:
    targets = load_targets()
    log.info("launching %d scout(s): %s", len(targets), ", ".join(targets))
    await asyncio.gather(*(scout_target(t, None) for t in targets))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    db.init()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        db.close()


if __name__ == "__main__":
    main()
