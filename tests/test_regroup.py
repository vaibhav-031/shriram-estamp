"""The regroup one-off's planning logic (pure – no Drive)."""
from scripts.regroup_folders import HUMAN, INTERNAL, SYSTEM_NAME, build_plan


def test_build_plan_moves_internals_and_renames_humans():
    current = {k: {"name": k, "parents": ["root"]} for k in INTERNAL}
    current.update({k: {"name": f"old {k}", "parents": ["root"]} for k in HUMAN})
    plan = build_plan(current, system_id=None)
    moves = [p for p in plan if p[0] == "move"]
    renames = [p for p in plan if p[0] == "rename"]
    assert len(moves) == len(INTERNAL) and all(dst == SYSTEM_NAME for _, _, dst in moves)
    assert {dst for _, _, dst in renames} == set(HUMAN.values())


def test_build_plan_is_idempotent_once_grouped_and_numbered():
    current = {k: {"name": k, "parents": ["sys"]} for k in INTERNAL}          # already under System
    current.update({k: {"name": new, "parents": ["root"]} for k, new in HUMAN.items()})  # already numbered
    assert build_plan(current, system_id="sys") == []
