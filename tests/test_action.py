import sys

from dgsearch.action import build_command, should_process_comment


def test_manual_direct_crawl_is_built_as_python_command():
    command, environment = build_command(
        "workflow_dispatch",
        {
            "inputs": {
                "process_open_issue": False,
                "query": "아이폰 17",
                "provinces": "서울특별시",
                "max_regions": "5",
            }
        },
    )

    assert command[:4] == [sys.executable, "-m", "scrapy", "crawl"]
    assert "query=아이폰 17" in command
    assert "provinces=서울특별시" in command
    assert "max_regions=5" in command
    assert environment


def test_schedule_runs_python_issue_worker(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    command, environment = build_command("schedule", {})

    assert command == [sys.executable, "scripts/process_issues.py"]
    assert environment["DGSEARCH_MAX_REGIONS"] == "300"


def test_manual_issue_worker_uses_requested_test_limit():
    command, environment = build_command(
        "workflow_dispatch",
        {"inputs": {"process_open_issue": "true", "max_regions": "1"}},
    )

    assert command == [sys.executable, "scripts/process_issues.py"]
    assert environment["DGSEARCH_MAX_REGIONS"] == "1"


def test_untrusted_or_bot_comments_do_not_run():
    untrusted = {
        "issue": {},
        "comment": {"author_association": "NONE", "user": {"login": "visitor"}},
    }
    bot = {
        "issue": {},
        "comment": {
            "author_association": "OWNER",
            "user": {"login": "github-actions[bot]"},
        },
    }

    assert not should_process_comment(untrusted)
    assert not should_process_comment(bot)
    assert build_command("issue_comment", untrusted)[0] is None
