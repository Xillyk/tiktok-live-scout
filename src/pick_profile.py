"""macOS osascript profile chooser used by tiktok-live-scout.command.

Behaviour:
  * 0 profiles configured  -> exit code 1
  * 1 profile  configured  -> print its name, exit 0 (no dialog shown)
  * >1 profiles configured -> show a native chooser; print the selected
                              name and exit 0, or exit 1 if cancelled.

The chooser shows "<name>  (N targets)" but only the name is printed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from .config import load


def _osascript(profiles: list[tuple[str, int]]) -> str | None:
    """Run a native macOS list-chooser. Returns the selected profile name
    or None on cancel."""
    items_applescript = ", ".join(
        f'"{name}  ({count} target{"s" if count != 1 else ""})"'
        for name, count in profiles
    )
    # NB: don't name the list `items` — it's an AppleScript reserved word.
    script = (
        "set profileList to {" + items_applescript + "}\n"
        'set chosen to choose from list profileList with '
        '  prompt "Select a TikTok account to scout with:" '
        '  default items {item 1 of profileList} '
        '  with title "TikTok Live Scout"\n'
        "if chosen is false then\n"
        '  return ""\n'
        "else\n"
        "  return item 1 of chosen\n"
        "end if"
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    label = result.stdout.strip()
    if not label:
        return None
    # The label is "<name>  (N targets)" — return just the name.
    return label.split("  (", 1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Pick a scout profile")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load(args.config)
    if not cfg.profiles:
        print("no profiles configured", file=sys.stderr)
        sys.exit(1)

    if len(cfg.profiles) == 1:
        print(cfg.profiles[0].name)
        return

    chosen = _osascript([(p.name, len(p.targets)) for p in cfg.profiles])
    if not chosen:
        sys.exit(1)
    print(chosen)


if __name__ == "__main__":
    main()
