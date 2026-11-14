#!/usr/bin/env python3
"""Priority-based Minecraft-style loading text selector for Akai MPC boot screens."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import random
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_MESSAGES_PATH = Path(__file__).resolve().with_name("boot_messages.json")


def load_boot_messages(path: Path | str = DEFAULT_MESSAGES_PATH) -> dict[str, Any]:
    """Load the 4-layer boot_messages.json schema from disk."""
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_special_trigger(
    triggers: Sequence[Mapping[str, Any]],
    now: _dt.datetime | None = None,
    rng: random.Random | None = None,
) -> tuple[str, str] | None:
    """Return `(trigger_id, message)` for the highest-priority matching special rule, or `None`."""
    cur = now or _dt.datetime.now()
    mm_dd = cur.strftime("%m-%d")
    hour = cur.hour
    picker = rng or random

    ordered = sorted(triggers, key=lambda t: int(t.get("priority", 0)), reverse=True)
    for trig in ordered:
        t_type = trig.get("type")
        msgs = trig.get("messages") or []
        if not msgs:
            continue
        matched = False
        if t_type == "date" and mm_dd in (trig.get("dates") or []):
            matched = True
        elif t_type == "time_range":
            start_h = int(trig.get("start_hour", 0))
            end_h = int(trig.get("end_hour", 0))
            if start_h <= end_h:
                matched = start_h <= hour < end_h
            else:  # Wraps midnight (e.g. 23 -> 04)
                matched = hour >= start_h or hour < end_h
        if matched:
            return str(trig.get("id", "special")), str(picker.choice(msgs))
    return None


def select_boot_messages(
    data: Mapping[str, Any],
    progress: float = 0.0,
    now: _dt.datetime | None = None,
    tip_category: str = "all",
    rng: random.Random | None = None,
) -> dict[str, str]:
    """Select active stage, tip, and splash banner following priority rules.

    Priority order:
      1. `special_triggers` (calendar dates -> late night 01:00-05:00) override `splashes`.
      2. `boot_stages` advance sequentially in proportion to `progress` [0.0 .. 1.0].
      3. `tips` draw from `tip_category` (`beginner_tips`, `sampling_tips`,
         `groove_and_mix_tips`, or `all`).
      4. `splashes` provide random one-line flavor when no special trigger is active.
    """
    picker = rng or random
    p = max(0.0, min(1.0, float(progress)))

    stages = data.get("boot_stages") or ["Booting MPC..."]
    if p >= 0.99:
        stage_text = str(stages[-1])
    else:
        idx = min(len(stages) - 1, int(p * len(stages)))
        stage_text = str(stages[idx])

    special = resolve_special_trigger(data.get("special_triggers") or [], now=now, rng=picker)
    if special is not None:
        trig_id, splash_text = special
        splash_source = f"special:{trig_id}"
    else:
        splashes = data.get("splashes") or ["16 pads, zero sleep!"]
        splash_text = str(picker.choice(splashes))
        splash_source = "splash"

    tips_map = data.get("tips") or {}
    if tip_category != "all" and isinstance(tips_map.get(tip_category), list) and tips_map[tip_category]:
        tip_pool = list(tips_map[tip_category])
        chosen_cat = tip_category
    else:
        tagged = [(k, msg) for k, lst in tips_map.items() for msg in (lst or [])]
        if tagged:
            chosen_cat, tip_text = picker.choice(tagged)
        else:
            chosen_cat, tip_text = "default", "Check levels before you drop the beat."
        tip_pool = None

    if tip_pool is not None:
        tip_text = str(picker.choice(tip_pool))

    return {
        "stage": stage_text,
        "splash": splash_text,
        "splash_source": splash_source,
        "tip": str(tip_text),
        "tip_category": chosen_cat,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Select boot messages from boot_messages.json")
    ap.add_argument("--json", default=str(DEFAULT_MESSAGES_PATH), help="Path to boot_messages.json")
    ap.add_argument("--progress", type=float, default=0.5, help="Boot progress [0.0 .. 1.0]")
    ap.add_argument("--date", help="Override date as MM-DD (e.g. 02-07 or 08-11)")
    ap.add_argument("--hour", type=int, help="Override local hour [0 .. 23]")
    ap.add_argument("--category", default="all", help="Tip pool category or 'all'")
    ap.add_argument("--seed", type=int, default=None, help="Optional RNG seed")
    args = ap.parse_args()

    now = _dt.datetime.now()
    if args.date:
        mm, dd = (int(x) for x in args.date.split("-", 1))
        now = now.replace(month=mm, day=dd)
    if args.hour is not None:
        now = now.replace(hour=args.hour)

    data = load_boot_messages(args.json)
    rng = random.Random(args.seed) if args.seed is not None else None
    selected = select_boot_messages(data, progress=args.progress, now=now, tip_category=args.category, rng=rng)
    print(json.dumps(selected, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
