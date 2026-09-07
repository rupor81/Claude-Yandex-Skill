#!/usr/bin/env python3
"""Check the project's own records against each other.

This exists because the records broke three times in epic 1, always the same
way: a judgement made correctly and then filed by hand.  Eight stories sat at
``review`` because a manual transition was never made.  ``deferred-work.md``
grew a ``## Resolved`` heading in the middle of itself, so seven open items read
as closed -- and then, during the retrospective that found that, it turned out
to have two such headings and to have lost an entry to a repair script.

None of those were failures of judgement.  They were failures of transcription,
and transcription is the part a script does better than a person.

Run it with no arguments; it prints what is wrong and exits non-zero.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "_bmad-output" / "implementation-artifacts"
SPRINT_STATUS = ARTIFACTS / "sprint-status.yaml"
DEFERRED = ARTIFACTS / "deferred-work.md"

STORY_STATUSES = {"backlog", "ready-for-dev", "in-progress", "review", "done"}
ACTION_STATUSES = {"open", "in-progress", "done"}


def _front_matter_status(spec: Path) -> str | None:
    """The ``status:`` a spec declares about itself."""
    text = spec.read_text()
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    match = re.search(r"^status:\s*'?\"?([\w-]+)", text[3:end], re.MULTILINE)
    return match.group(1) if match else None


def check_story_statuses(status: dict) -> list[str]:
    """Every spec's own status, against the one sprint-status records for it."""
    problems = []
    development = status.get("development_status") or {}
    for spec in sorted(ARTIFACTS.glob("spec-*.md")):
        key = spec.stem.removeprefix("spec-")
        declared = _front_matter_status(spec)
        if declared is None:
            problems.append(f"{spec.name}: no `status:` in its front matter")
            continue
        if declared not in STORY_STATUSES:
            problems.append(f"{spec.name}: status {declared!r} is not a story status")
        if key not in development:
            problems.append(f"{spec.name}: no entry {key!r} in sprint-status.yaml")
        elif development[key] != declared:
            problems.append(
                f"{key}: the spec says {declared!r}, sprint-status says "
                f"{development[key]!r}"
            )
    return problems


def check_epic_rollup(status: dict) -> list[str]:
    """An epic is done exactly when its stories are. Nothing else is a rollup."""
    problems = []
    development = status.get("development_status") or {}
    epics = sorted(
        k.removeprefix("epic-") for k in development if re.fullmatch(r"epic-\d+", k)
    )
    for epic in epics:
        stories = {
            k: v
            for k, v in development.items()
            for m in [re.fullmatch(rf"{epic}-\d+-[\w-]+", k)]
            if m
        }
        if not stories:
            continue
        all_done = all(v == "done" for v in stories.values())
        recorded = development[f"epic-{epic}"]
        if all_done and recorded != "done":
            problems.append(
                f"epic-{epic}: every story is done but the epic says {recorded!r}"
            )
        if not all_done and recorded == "done":
            pending = sorted(k for k, v in stories.items() if v != "done")
            problems.append(
                f"epic-{epic}: recorded done, but {len(pending)} "
                + ("story is" if len(pending) == 1 else "stories are")
                + " not: "
                + ", ".join(pending)
            )
    return problems


def check_action_items(status: dict) -> list[str]:
    """Action items carry an id, a status, and a document that argues for them."""
    problems = []
    seen: set[str] = set()
    for item in status.get("action_items") or []:
        ident = item.get("id")
        if not ident:
            problems.append("an action item has no id")
            continue
        if ident in seen:
            problems.append(f"{ident}: duplicate action item id")
        seen.add(ident)
        if item.get("status") not in ACTION_STATUSES:
            problems.append(f"{ident}: status {item.get('status')!r} is not valid")
        source = (item.get("source") or "").replace("{project-root}/", "")
        if source and not (ROOT / source).exists():
            problems.append(f"{ident}: source {source} does not exist")
    return problems


def check_deferred_work() -> list[str]:
    """The register's shape -- the exact thing that broke twice.

    One ``## Resolved`` heading, and every open entry above it naming a spec
    that exists. An entry filed under the heading by accident is invisible, and
    invisible is how a known limitation becomes a surprise.
    """
    problems = []
    text = DEFERRED.read_text()
    headings = re.findall(r"^## Resolved\s*$", text, re.MULTILINE)
    if len(headings) != 1:
        problems.append(
            f"deferred-work.md: {len(headings)} `## Resolved` headings, expected 1"
            " -- entries after a stray one read as closed when they are not"
        )
    above = text.split("\n## Resolved", 1)[0]
    for spec in re.findall(r"^- source_spec: `([^`]+)`", above, re.MULTILINE):
        if not (ROOT / spec).exists():
            problems.append(f"deferred-work.md: source_spec {spec} does not exist")
    if not re.search(r"^- source_spec:", above, re.MULTILINE):
        problems.append("deferred-work.md: no open entries above `## Resolved`")
    return problems


def main() -> int:
    try:
        import yaml
    except ImportError:
        print(
            "PyYAML is needed: uv run --group dev python scripts/check_bookkeeping.py"
        )
        return 2

    status = yaml.safe_load(SPRINT_STATUS.read_text())
    problems = (
        check_story_statuses(status)
        + check_epic_rollup(status)
        + check_action_items(status)
        + check_deferred_work()
    )
    for problem in problems:
        print(f"  {problem}")
    if problems:
        noun = "problem" if len(problems) == 1 else "problems"
        print(f"\n{len(problems)} bookkeeping {noun}.")
        return 1
    print("Records agree with each other.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
