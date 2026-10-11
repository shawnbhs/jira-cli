#!/usr/bin/env python3
"""Fill the sprint / greenhopper fixtures that the main capture could not reach.

Read-only (GET only). Reuses the Sanitizer and scanner from ``capture_shapes``
so the output is sanitized identically. Exists because on this server the
Agile sprint list (``/rest/agile/1.0/board/{id}/sprint``) returns an EMPTY
``values`` array for every scrum board, while the GreenHopper velocity chart
does report sprints - so sprint ids have to be discovered through GreenHopper.

Usage (inside WSL, from the repo root):

    scripts/capture_sprint_shapes.py [--board N]

Credentials come from the same config file as ``capture_shapes.py`` and are
never printed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture_shapes as cs  # noqa: E402

FIXTURES = cs.FIXTURES


def main(argv: list[str]) -> int:
    env = cs.load_env(cs.CONFIG_PATH)
    base = env.get("JIRA_BASE_URL", "").rstrip("/")
    user = env.get("JIRA_EMAIL", "")
    secret = env.get("JIRA_API_TOKEN", "")
    project = env.get("JIRA_PROJECT", "")
    if not (base and user and secret):
        raise SystemExit("config.env missing required keys")
    host = cs.re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    real_email = user if "@" in user else ""
    real_user = user.split("@")[0]

    board = None
    if "--board" in argv:
        board = int(argv[argv.index("--board") + 1])

    san = cs.Sanitizer(host, real_user, real_email)
    cap = cs.Capture(base, user, secret, san)

    # Reuse the already-captured board list rather than spending a request.
    if board is None:
        p = FIXTURES / "scrum_board_lookup.json"
        if p.exists():
            vals = json.loads(p.read_text(encoding="utf-8")).get("values") or []
            board = vals[0]["id"] if vals else None
    if board is None:
        raise SystemExit("no scrum board id available")

    print("capturing (GET only):")
    vel = cap.get(
        "greenhopper_velocity",
        "/rest/greenhopper/1.0/rapid/charts/velocity.json",
        {"rapidViewId": board},
    )
    sprint_id = None
    if isinstance(vel, dict):
        sprints = vel.get("sprints") or []
        print(f"  velocity sprints reported: {len(sprints)}")
        if sprints:
            sprint_id = sprints[-1].get("id")

    if sprint_id is None:
        # GreenHopper also exposes the board's own sprint picker
        pick = cap.get(
            "greenhopper_sprintpicker",
            "/rest/greenhopper/1.0/sprint/picker",
            {"rapidViewId": board},
        )
        if isinstance(pick, dict):
            for bucket in ("suggestions", "allMatches"):
                items = pick.get(bucket) or []
                if items:
                    sprint_id = items[0].get("id")
                    break

    if sprint_id is not None:
        cap.get("sprint_detail", f"/rest/agile/1.0/sprint/{sprint_id}")
        cap.get(
            "sprint_issues",
            f"/rest/agile/1.0/sprint/{sprint_id}/issue",
            {"maxResults": 3},
        )
        cap.get(
            "greenhopper_sprintreport",
            "/rest/greenhopper/1.0/rapid/charts/sprintreport",
            {"rapidViewId": board, "sprintId": sprint_id},
        )
    else:
        print("  no sprint id discoverable on this board")

    print(f"requests made: {cap.count}")
    for label, data in cap.raw.items():
        p = cap.write(label, data)
        print(f"  wrote {p.name} ({p.stat().st_size} bytes)")

    meta_path = FIXTURES / "_capture_meta.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.exists()
        else {"endpoints": []}
    )
    meta["endpoints"].extend(
        {
            "label": m["label"],
            "path": san.string(m["path"]),
            "status": m["status"],
            "error": san.string(m["error"]),
        }
        for m in cap.meta
    )
    meta["requests"] = int(meta.get("requests", 0)) + cap.count
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    print("leak scan (all must be 0):", cs.scan(host, real_user, project, san))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
