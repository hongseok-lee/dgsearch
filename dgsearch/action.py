from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def load_event(path: str | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def as_bool(value) -> bool:
    return str(value).casefold() in {"1", "true", "yes", "on"}


def should_process_comment(event: dict) -> bool:
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    user = comment.get("user") or {}
    return (
        "pull_request" not in issue
        and comment.get("author_association") in TRUSTED_ASSOCIATIONS
        and user.get("login") != "github-actions[bot]"
    )


def build_command(event_name: str, event: dict) -> tuple[list[str] | None, dict[str, str]]:
    inputs = event.get("inputs") or {}
    environment = os.environ.copy()

    if event_name == "issue_comment" and not should_process_comment(event):
        return None, environment

    process_issue = event_name in {"schedule", "issue_comment"} or as_bool(
        inputs.get("process_open_issue")
    )
    if process_issue:
        max_regions = inputs.get("max_regions", "300") if event_name == "workflow_dispatch" else "300"
        environment.update(
            {
                "DGSEARCH_MAX_REGIONS": str(max_regions or "300"),
                "DGSEARCH_MAX_COMMENT_RESULTS": "50",
            }
        )
        return [sys.executable, "scripts/process_issues.py"], environment

    if event_name != "workflow_dispatch":
        return None, environment

    return (
        [
            sys.executable,
            "-m",
            "scrapy",
            "crawl",
            "daangn",
            "-a",
            f"query={inputs.get('query', '갤럭시 폴드 7')}",
            "-a",
            f"provinces={inputs.get('provinces', '서울특별시,경기도')}",
            "-a",
            f"max_regions={inputs.get('max_regions', '100')}",
        ],
        environment,
    )


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    event = load_event(os.getenv("GITHUB_EVENT_PATH"))
    command, environment = build_command(event_name, event)
    if command is None:
        print(f"No work for GitHub event: {event_name}")
        return 0
    subprocess.run(command, env=environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

