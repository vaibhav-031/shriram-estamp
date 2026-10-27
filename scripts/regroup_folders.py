"""One-off: tidy the Drive layout to the handoff's Phase F shape, without touching the service.

Moves the machine-internal folders (Inventory, Backup, Upload Archive, Orders Processed/Rejected) under a
single "⚙️ System — do not touch" parent, and numbers the human-facing folders so ops read them in order.

Safe by construction: folders are referenced by ID in config.yaml, and moving/renaming a folder does NOT
change its ID – the service keeps finding every folder. This script only moves and renames; it never deletes.

    uv run python scripts/regroup_folders.py            # preview (dry run – prints the plan, changes nothing)
    uv run python scripts/regroup_folders.py --apply    # do it, then:  python -m estamp check-setup

Idempotent: re-running after --apply finds nothing left to do.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estamp import constants as K

SYSTEM_NAME = "⚙️ System — do not touch"

# machine-internal folders → moved under the System parent
INTERNAL = [K.INVENTORY_ROOT, K.BACKUP_ROOT, K.UPLOAD_ARCHIVE, K.ORDERS_PROCESSED, K.ORDERS_REJECTED]

# human-facing folders → renamed with a workflow number (IDs stay the same, so config needs no change)
HUMAN = {
    K.UPLOAD_INBOX:    "1 · 📥 Upload Inbox",
    K.ORDERS_INCOMING: "2 · 📥 Orders Incoming",
    K.NEEDS_REVIEW:    "3 · ⚠️ Needs Review",
    K.OUTPUT_ROOT:     "4 · 📤 Output",
}


def build_plan(current: dict[str, dict], system_id: str | None) -> list[tuple[str, str, str]]:
    """Pure: decide what to move/rename. `current` maps each folder key to its {name, parents}; `system_id`
    is the System folder's id (None in a dry run, when it may not exist yet). Returns (op, from, to) tuples."""
    plan: list[tuple[str, str, str]] = []
    for key in INTERNAL:
        f = current[key]
        if system_id and system_id in f.get("parents", []):
            continue                                   # already under System – nothing to do
        plan.append(("move", f["name"], SYSTEM_NAME))
    for key, new_name in HUMAN.items():
        f = current[key]
        if f["name"] == new_name:
            continue                                   # already numbered
        plan.append(("rename", f["name"], new_name))
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="regroup_folders")
    ap.add_argument("--apply", action="store_true", help="actually move/rename (default is a dry-run preview)")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)

    from estamp.settings import load_settings
    from estamp.google_clients import Google

    s = load_settings(args.config)
    g = Google(s)

    root = g.get_file(s.folder(K.UPLOAD_INBOX))["parents"][0]   # the eStamp root = inbox's parent
    # --apply creates System if missing; dry-run only looks it up (read-only) so the preview is accurate.
    system_id = g.folder_path(root, [SYSTEM_NAME]) if args.apply else _find_child(g, root, SYSTEM_NAME)
    current = {key: g.get_file(s.folder(key)) for key in INTERNAL + list(HUMAN)}
    plan = build_plan(current, system_id)

    if not plan:
        print("Nothing to do – the layout is already grouped and numbered.")
        return 0

    print(("APPLYING" if args.apply else "DRY RUN – pass --apply to execute") + f" ({len(plan)} change(s)):")
    for op, src, dst in plan:
        print(f"  {op:<6} {src!r}  →  {dst!r}")
        if not args.apply:
            continue
        if op == "move":
            g.move_rename(current[_key_of(current, src)]["id"], system_id)
        else:
            f = current[_key_of(current, src)]
            g.move_rename(f["id"], f["parents"][0], dst)   # rename = move to same parent with a new name

    if args.apply:
        print("\nDone. Now run:  python -m estamp check-setup   (confirms every folder id still resolves).")
    return 0


def _key_of(current: dict[str, dict], name: str) -> str:
    return next(k for k, f in current.items() if f["name"] == name)


def _find_child(g, parent: str, name: str) -> str | None:
    """Read-only: the id of the sub-folder named `name` under `parent`, or None. Never creates anything."""
    return next((f["id"] for f in g.list_children(parent, folders=True) if f["name"] == name), None)


if __name__ == "__main__":
    sys.exit(main())
