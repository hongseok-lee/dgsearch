import asyncio
import json
from datetime import UTC, datetime

import pytest
import aiohttp

from scripts.process_issues import (
    PendingRequest,
    ProgressReporter,
    RecoverableAPIError,
    RunMetrics,
    api,
    consume_progress,
    crawl_incrementally,
    drain_pending_requests,
    failed_comments_superseded_by,
    find_status_comment,
    format_comment,
    has_failed_requests,
    is_relevant,
    legacy_source_ready,
    latest_source_for_issue,
    main,
    marker,
    pending_requests_for_issue,
    process_issue,
    progress_label,
    region_scope_label,
    status_state,
    stop_subprocess,
    supersede_resolved_failures,
    sources_for_issue,
    unique_results,
)
from dgsearch.listings import is_tradable


def test_format_comment_uses_markdown_table(monkeypatch):
    monkeypatch.setattr("scripts.process_issues.MAX_REGIONS", "10")
    body = format_comment(
        "갤럭시 폴드 7",
        [
            {
                "title": "폴드7 | 블루",
                "price": "1250000.0",
                "region": {"name": "시흥동"},
                "href": "https://example.com/item",
            }
        ],
        "<!-- dgsearch:test -->",
    )

    assert "| 가격 | 지역 | 매물 |" in body
    assert "| 1,250,000원 | 시흥동 | [폴드7 \\| 블루](https://example.com/item) |" in body
    assert "seller" not in body.lower()


def test_zero_region_limit_is_rendered_as_all(monkeypatch):
    monkeypatch.setattr("scripts.process_issues.MAX_REGIONS", "0")
    assert region_scope_label() == "전체"

    body = format_comment("자전거", [], "<!-- dgsearch:test -->")
    assert "- 조회 지역: 전체" in body
    assert "조회 지역 상한: 0개" not in body


def test_relevance_requires_every_keyword_token():
    fold5 = {
        "id": "5",
        "title": "삼성 갤럭시 Z 폴드5 512GB",
        "content": "갤럭시 폴드 7도 검색해 보세요",
        "status": "Ongoing",
    }
    fold7 = {
        "id": "7",
        "title": "삼성 갤럭시 Z 폴드7 256GB",
        "content": "",
        "status": "Ongoing",
    }

    assert not is_relevant(fold5, "갤럭시 폴드 7")
    assert is_relevant(fold7, "갤럭시 폴드 7")
    assert unique_results([fold5, fold7], "갤럭시 폴드 7") == [fold7]


def test_only_ongoing_listings_are_tradable():
    assert is_tradable({"status": "Ongoing"})
    assert not is_tradable({"status": "Reserved"})
    assert not is_tradable({"status": "SoldOut"})
    assert not is_tradable({})

    sold = {"id": "sold", "title": "갤럭시 폴드7", "status": "SoldOut"}
    ongoing = {"id": "live", "title": "갤럭시 폴드7", "status": "Ongoing"}
    assert unique_results([sold, ongoing], "갤럭시 폴드7") == [ongoing]


def test_trusted_user_comment_becomes_search_source():
    issue = {"id": 10, "body": "첫 검색", "author_association": "OWNER"}
    comments = [
        {
            "id": 20,
            "body": "아이폰 17 프로",
            "author_association": "OWNER",
            "user": {"login": "repo-owner"},
        },
        {
            "id": 21,
            "body": "<!-- dgsearch:result -->",
            "author_association": "NONE",
            "user": {"login": "github-actions[bot]"},
        },
        {
            "id": 22,
            "body": "외부 사용자 검색",
            "author_association": "NONE",
            "user": {"login": "visitor"},
        },
        {
            "id": 23,
            "body": "<!-- dgsearch:ack:comment:23 -->\n✅ 검색 요청을 접수했습니다.",
            "author_association": "OWNER",
            "user": {"login": "repo-owner"},
        },
    ]

    assert sources_for_issue(issue, comments) == [
        ("issue", 10, "첫 검색"),
        ("comment", 20, "아이폰 17 프로"),
    ]
    assert marker("comment", 20, "아이폰 17 프로") != marker("comment", 23, "아이폰 17 프로")
    assert latest_source_for_issue(issue, comments) == ("comment", 20, "아이폰 17 프로")


def test_progress_labels():
    assert progress_label(None, None, "running") == "지역 목록 준비 중"
    assert progress_label(3, 10, "running") == "3/10"
    assert progress_label(10, 10, "completed") == "완료 (10/10)"
    assert progress_label(3, 10, "failed") == "중단 (3/10)"


def test_consume_progress_reads_only_new_lines(tmp_path):
    path = tmp_path / "progress.jsonl"
    path.write_text('{"completed": 1}\n', encoding="utf-8")
    events = []

    async def collect(event):
        events.append(event)

    position = asyncio.run(consume_progress(path, 0, collect))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"completed": 2}\n')

    asyncio.run(consume_progress(path, position, collect))

    assert events == [{"completed": 1}, {"completed": 2}]


def test_crawl_incrementally_passes_subprocess_arguments_separately(monkeypatch, tmp_path):
    calls = []

    class FinishedProcess:
        returncode = 0

    async def fake_create_subprocess_exec(*args):
        calls.append(args)
        return FinishedProcess()

    async def ignore_progress(_event):
        pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.process_issues.MAX_REGIONS", "0")
    monkeypatch.setattr(
        "scripts.process_issues.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(
        crawl_incrementally("갤럭시 폴드 7", 7, ignore_progress, output_key="comment-22")
    )

    assert result == []
    assert calls == [
        (
            "scrapy",
            "crawl",
            "daangn",
            "-a",
            "query=갤럭시 폴드 7",
            "-a",
            "provinces=서울특별시,경기도",
            "-a",
            "max_regions=0",
            "-a",
            "progress_file=output/issue-7-comment-22-progress.jsonl",
            "-O",
            "output/issue-7-comment-22.jsonl",
        )
    ]


def test_stop_subprocess_escalates_from_terminate_to_kill():
    class StubbornProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            if self.killed:
                return self.returncode
            await asyncio.Future()

    process = StubbornProcess()

    asyncio.run(stop_subprocess(process, terminate_timeout=0.001))

    assert process.terminated
    assert process.killed
    assert process.returncode == -9


def test_open_issue_is_searched_even_when_old_result_marker_exists(monkeypatch, tmp_path):
    issue = {
        "id": 10,
        "number": 7,
        "body": "갤럭시 폴드7",
        "author_association": "OWNER",
        "state": "open",
    }
    old_result = {
        "id": 20,
        "body": "<!-- dgsearch:old-result -->",
        "author_association": "NONE",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:00Z",
    }
    comments = [old_result]
    calls = []
    item = {
        "id": "live",
        "title": "갤럭시 폴드7",
        "status": "Ongoing",
        "price": "1000000",
        "region": {"name": "부암동"},
        "href": "https://example.com/live",
    }

    async def fake_api(session, method, path, payload=None):
        if path.endswith("/issues?state=all&sort=created&direction=asc&per_page=100"):
            return [issue]
        if path.endswith("/issues/7/comments?per_page=100"):
            return comments
        if method == "GET" and path.endswith("/issues/7"):
            return issue
        calls.append((method, path, payload))
        if method == "POST" and path.endswith("/issues/7/comments"):
            comments.append(
                {
                    "id": 99,
                    "body": payload["body"],
                    "user": {"login": "github-actions[bot]"},
                    "created_at": "2026-07-13T11:00:00Z",
                }
            )
            return {"id": 99}
        if method == "PATCH" and path.endswith("/issues/comments/99"):
            comments[-1]["body"] = payload["body"]
        if method == "PATCH" and path.endswith("/issues/7"):
            issue["state"] = payload["state"]
        return {}

    async def fake_crawl(keyword, issue_number, on_progress, **kwargs):
        await on_progress({"completed": 1, "total": 2, "articles": [item], "error": None})
        await on_progress({"completed": 2, "total": 2, "articles": [item], "error": None})
        return [item, item]

    monkeypatch.setattr("scripts.process_issues.TOKEN", "token")
    monkeypatch.setattr("scripts.process_issues.REPOSITORY", "owner/repo")
    monkeypatch.setattr("scripts.process_issues.api", fake_api)
    monkeypatch.setattr("scripts.process_issues.crawl_incrementally", fake_crawl)
    monkeypatch.chdir(tmp_path)

    asyncio.run(main())

    assert calls[0][0:2] == ("POST", "/repos/owner/repo/issues/7/comments")
    updates = [call for call in calls if call[1] == "/repos/owner/repo/issues/comments/99"]
    assert len(updates) == 2
    assert "- 고유 매물: 1개" in updates[0][2]["body"]
    assert "- 진행: 1/2" in updates[0][2]["body"]
    assert "<!-- dgsearch:state:completed -->" in updates[-1][2]["body"]
    assert "- 진행: 완료 (2/2)" in updates[-1][2]["body"]
    assert calls[-1] == ("PATCH", "/repos/owner/repo/issues/7", {"state": "closed"})


def test_later_reopen_ack_makes_completed_issue_source_pending_again():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    source_marker = marker("issue", 10, "제습기")
    completed = {
        "id": 20,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:00Z",
    }
    reopened_ack = {
        "id": 21,
        "body": (
            "<!-- dgsearch:ack:issue:10:reopened:2026-07-13T11:00:00Z -->\n"
            "<!-- dgsearch:state:queued -->\n✅ 검색 요청을 접수했습니다."
        ),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T11:00:01Z",
    }

    selected = find_status_comment([completed, reopened_ack], "issue", 10, source_marker)
    pending = pending_requests_for_issue(issue, [completed, reopened_ack])

    assert selected == reopened_ack
    assert status_state(selected) == "queued"
    assert len(pending) == 1
    assert pending[0].identity.startswith("<!-- dgsearch:ack:issue:10:reopened:")


def test_late_reopen_ack_does_not_resurrect_result_completed_after_event():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    source_marker = marker("issue", 10, "제습기")
    completed_after_reopen = {
        "id": 20,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:01:00Z",
        "updated_at": "2026-07-13T10:01:05Z",
    }
    late_ack = {
        "id": 21,
        "body": (
            "<!-- dgsearch:ack:issue:10:reopened:2026-07-13T10:00:00Z -->\n"
            "<!-- dgsearch:state:queued -->"
        ),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:01:06Z",
    }

    selected = find_status_comment([completed_after_reopen, late_ack], "issue", 10, source_marker)

    assert selected == completed_after_reopen
    assert pending_requests_for_issue(issue, [completed_after_reopen, late_ack]) == []


def test_reopen_ack_survives_old_comment_finishing_after_reopen():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    source_marker = marker("issue", 10, "제습기")
    old_comment_finished_late = {
        "id": 20,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T09:00:00Z",
        "updated_at": "2026-07-13T10:01:05Z",
    }
    reopen_ack = {
        "id": 21,
        "body": (
            "<!-- dgsearch:ack:issue:10:reopened:2026-07-13T10:00:00Z -->\n"
            "<!-- dgsearch:state:queued -->"
        ),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:01Z",
    }

    selected = find_status_comment(
        [old_comment_finished_late, reopen_ack], "issue", 10, source_marker
    )
    pending = pending_requests_for_issue(issue, [old_comment_finished_late, reopen_ack])

    assert selected == reopen_ack
    assert len(pending) == 1
    assert pending[0].identity.startswith("<!-- dgsearch:ack:issue:10:reopened:")


def test_late_comment_ack_does_not_resurrect_completed_request():
    issue = {
        "id": 10,
        "number": 7,
        "body": "첫 검색",
        "author_association": "OWNER",
        "state": "open",
    }
    request_comment = {
        "id": 30,
        "body": "두 번째 검색",
        "author_association": "OWNER",
        "user": {"login": "repo-owner"},
        "created_at": "2026-07-13T10:00:00Z",
    }
    source_marker = marker("comment", 30, "두 번째 검색")
    completed = {
        "id": 31,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:01Z",
    }
    late_ack = {
        "id": 32,
        "body": ("<!-- dgsearch:ack:comment:30 -->\n<!-- dgsearch:state:queued -->"),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:02Z",
    }

    selected = find_status_comment(
        [request_comment, completed, late_ack], "comment", 30, source_marker
    )

    assert selected == completed
    assert pending_requests_for_issue(issue, [request_comment, completed, late_ack]) == []


def test_late_opened_ack_does_not_resurrect_completed_issue_request():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    source_marker = marker("issue", 10, "제습기")
    completed = {
        "id": 31,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:01Z",
    }
    late_ack = {
        "id": 32,
        "body": (
            "<!-- dgsearch:ack:issue:10:opened:2026-07-13T10:00:00Z -->\n"
            "<!-- dgsearch:state:queued -->"
        ),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:02Z",
    }

    selected = find_status_comment([completed, late_ack], "issue", 10, source_marker)

    assert selected == completed
    assert pending_requests_for_issue(issue, [completed, late_ack]) == []


def test_closed_marker_only_legacy_result_is_not_recovered():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "closed",
    }
    legacy_result = {
        "id": 20,
        "body": f"{marker('issue', 10, '제습기')}\n과거 결과",
        "user": {"login": "github-actions[bot]"},
    }

    assert status_state(legacy_result) == "running"
    assert pending_requests_for_issue(issue, [legacy_result]) == []


def test_new_source_waits_for_ack_grace_period():
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
        "created_at": created_at,
    }

    assert not legacy_source_ready(issue, [], "issue", 10)
    assert pending_requests_for_issue(issue, []) == []


def test_reopened_old_issue_uses_recent_update_for_ack_grace():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    assert not legacy_source_ready(issue, [], "issue", 10)
    assert pending_requests_for_issue(issue, []) == []


def test_completed_request_is_not_queued_again():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    source_marker = marker("issue", 10, "제습기")
    completed = {
        "id": 20,
        "body": f"{source_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
    }

    assert pending_requests_for_issue(issue, [completed]) == []


def test_failed_request_blocks_automatic_issue_close():
    issue = {
        "id": 10,
        "number": 7,
        "body": "첫 검색",
        "author_association": "OWNER",
        "state": "open",
    }
    failed = {
        "id": 20,
        "body": (f"{marker('issue', 10, '첫 검색')}\n<!-- dgsearch:state:failed -->"),
        "user": {"login": "github-actions[bot]"},
    }
    later_comment = {
        "id": 30,
        "body": "두 번째 검색",
        "author_association": "OWNER",
        "user": {"login": "repo-owner"},
    }
    completed = {
        "id": 31,
        "body": (f"{marker('comment', 30, '두 번째 검색')}\n<!-- dgsearch:state:completed -->"),
        "user": {"login": "github-actions[bot]"},
    }
    comments = [failed, later_comment, completed]

    assert pending_requests_for_issue(issue, comments) == []
    assert has_failed_requests(issue, comments)


def test_comment_created_after_failure_supersedes_failed_request(monkeypatch):
    issue = {
        "id": 10,
        "number": 7,
        "body": "첫 검색",
        "author_association": "OWNER",
        "state": "open",
    }
    failed = {
        "id": 20,
        "body": (f"{marker('issue', 10, '첫 검색')}\n<!-- dgsearch:state:failed -->"),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:00Z",
        "updated_at": "2026-07-13T10:05:00Z",
    }
    retry = {
        "id": 30,
        "body": "첫 검색",
        "author_association": "OWNER",
        "user": {"login": "repo-owner"},
        "created_at": "2026-07-13T10:06:00Z",
    }
    completed = {
        "id": 31,
        "body": (f"{marker('comment', 30, '첫 검색')}\n<!-- dgsearch:state:completed -->"),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:07:00Z",
    }

    superseded = failed_comments_superseded_by(
        issue, [failed, retry, completed], ("comment", 30, "첫 검색")
    )

    assert superseded == [failed]

    async def fake_list_comments(session, issue_number):
        return [failed, retry, completed]

    async def fake_update(session, comment_id, body):
        assert comment_id == 20
        failed["body"] = body

    monkeypatch.setattr("scripts.process_issues.list_issue_comments", fake_list_comments)
    monkeypatch.setattr("scripts.process_issues.update_result_comment", fake_update)

    count = asyncio.run(supersede_resolved_failures(object(), issue, ("comment", 30, "첫 검색")))

    assert count == 1
    assert status_state(failed) == "superseded"
    assert not has_failed_requests(issue, [failed, retry, completed])


def test_interrupted_request_remains_recoverable():
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    interrupted = {
        "id": 20,
        "body": (
            "<!-- dgsearch:ack:issue:10:opened:2026-07-13T10:00:00Z -->\n"
            f"{marker('issue', 10, '제습기')}\n"
            "<!-- dgsearch:state:interrupted -->"
        ),
        "user": {"login": "github-actions[bot]"},
    }

    pending = pending_requests_for_issue(issue, [interrupted])

    assert status_state(interrupted) == "interrupted"
    assert len(pending) == 1


def test_new_comment_request_remains_pending_after_issue_request_completes():
    issue = {
        "id": 10,
        "number": 7,
        "body": "첫 검색",
        "author_association": "OWNER",
        "state": "open",
    }
    issue_marker = marker("issue", 10, "첫 검색")
    completed = {
        "id": 20,
        "body": f"{issue_marker}\n<!-- dgsearch:state:completed -->",
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:00:00Z",
    }
    request_comment = {
        "id": 30,
        "body": "두 번째 검색",
        "author_association": "OWNER",
        "user": {"login": "repo-owner"},
        "created_at": "2026-07-13T10:01:00Z",
    }
    ack = {
        "id": 31,
        "body": (
            "<!-- dgsearch:ack:comment:30 -->\n"
            "<!-- dgsearch:state:queued -->\n✅ 검색 요청을 접수했습니다."
        ),
        "user": {"login": "github-actions[bot]"},
        "created_at": "2026-07-13T10:01:05Z",
    }

    pending = pending_requests_for_issue(issue, [completed, request_comment, ack])

    assert [request.source for request in pending] == [("comment", 30, "두 번째 검색")]


def test_progress_reporter_coalesces_comment_updates(monkeypatch):
    calls = []

    async def fake_update(session, comment_id, body):
        calls.append((comment_id, body))

    class Metrics:
        def __init__(self):
            self.records = []

        def emit(self, record_type, **fields):
            self.records.append((record_type, fields))

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    item = {
        "id": "live",
        "title": "제습기",
        "status": "Ongoing",
        "href": "https://example.com/live",
        "region": {"name": "시흥동"},
    }
    clock = Clock()
    metrics = Metrics()
    monkeypatch.setattr("scripts.process_issues.update_result_comment", fake_update)
    reporter = ProgressReporter(
        object(),
        99,
        "제습기",
        ["<!-- dgsearch:test -->"],
        7,
        metrics,
        interval=30,
        clock=clock,
    )

    async def run_events():
        await reporter.on_progress({"completed": 1, "total": 4, "articles": [item], "error": None})
        clock.now = 5
        await reporter.on_progress({"completed": 2, "total": 4, "articles": [], "error": "timeout"})
        clock.now = 29.9
        await reporter.on_progress({"completed": 3, "total": 4, "articles": [], "error": None})
        clock.now = 30
        await reporter.on_progress({"completed": 4, "total": 4, "articles": [], "error": None})
        await reporter.publish("completed", items=[item])

    asyncio.run(run_events())

    assert len(calls) == 3
    assert "- 진행: 1/4" in calls[0][1]
    assert "- 진행: 4/4" in calls[1][1]
    assert "- 실패 지역: 1개" in calls[1][1]
    assert "- 진행: 완료 (4/4)" in calls[2][1]
    assert reporter.raw_progress_events == 4
    assert reporter.progress_comment_updates == 2
    assert reporter.terminal_comment_updates == 1


def test_process_failure_reuses_ack_and_marks_same_comment_failed(monkeypatch):
    ack_marker = "<!-- dgsearch:ack:issue:10:opened:2026-07-13T10:00:00Z -->"
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    ack = {
        "id": 21,
        "body": f"{ack_marker}\n<!-- dgsearch:state:queued -->",
        "user": {"login": "github-actions[bot]"},
    }
    request = PendingRequest(
        issue=issue,
        comments=[ack],
        source=("issue", 10, "제습기"),
        status_comment=ack,
        identity=ack_marker,
    )
    calls = []

    async def fake_api(session, method, path, payload=None):
        calls.append((method, path, payload))
        return {}

    async def failing_crawl(keyword, issue_number, on_progress, **kwargs):
        await on_progress({"completed": 1, "total": 2, "articles": [], "error": None})
        raise RuntimeError("boom")

    class Metrics:
        context = {"trigger_issue_number": 7}

        def __init__(self):
            self.records = []

        def emit(self, record_type, **fields):
            self.records.append((record_type, fields))

    monkeypatch.setattr("scripts.process_issues.REPOSITORY", "owner/repo")
    monkeypatch.setattr("scripts.process_issues.api", fake_api)
    monkeypatch.setattr("scripts.process_issues.crawl_incrementally", failing_crawl)
    metrics = Metrics()

    result = asyncio.run(process_issue(object(), request, metrics))

    assert result is False
    assert not [call for call in calls if call[0] == "POST"]
    updates = [call for call in calls if "/issues/comments/21" in call[1]]
    assert len(updates) == 3
    assert ack_marker in updates[-1][2]["body"]
    assert "<!-- dgsearch:state:failed -->" in updates[-1][2]["body"]
    assert "- 상태: ❌ 실패" in updates[-1][2]["body"]
    assert not [call for call in calls if call[1].endswith("/issues/7")]
    assert metrics.records[-1][1]["outcome"] == "failure"


def test_request_timeout_covers_status_preparation(monkeypatch):
    issue = {
        "id": 10,
        "number": 7,
        "body": "제습기",
        "author_association": "OWNER",
        "state": "open",
    }
    request = PendingRequest(
        issue=issue,
        comments=[],
        source=("issue", 10, "제습기"),
        status_comment=None,
        identity="request-10",
    )

    async def slow_prepare(*args, **kwargs):
        await asyncio.sleep(3600)

    class Metrics:
        context = {"trigger_issue_number": 7}

        def __init__(self):
            self.records = []

        def emit(self, record_type, **fields):
            self.records.append((record_type, fields))

    monkeypatch.setattr("scripts.process_issues.prepare_status_comment", slow_prepare)
    metrics = Metrics()

    result = asyncio.run(process_issue(object(), request, metrics, request_timeout_seconds=0.001))

    assert result is False
    assert metrics.records[-1][1]["error_type"] == "TimeoutError"


def test_drain_processes_multiple_requests_and_continues_after_failure(monkeypatch):
    def pending(number):
        return PendingRequest(
            issue={"id": number, "number": number, "state": "open"},
            comments=[],
            source=("issue", number, f"검색 {number}"),
            status_comment=None,
            identity=f"request-{number}",
        )

    queued = [pending(1), pending(2), pending(3)]
    processed_order = []

    async def fake_discover(session):
        return queued

    async def fake_process(session, request, metrics, **kwargs):
        processed_order.append(request.issue["number"])
        return request.issue["number"] != 1

    monkeypatch.setattr("scripts.process_issues.discover_pending_requests", fake_discover)
    monkeypatch.setattr("scripts.process_issues.process_issue", fake_process)
    monkeypatch.setattr("scripts.process_issues.MAX_REQUESTS_PER_RUN", 4)
    monkeypatch.setattr("scripts.process_issues.WORKER_BUDGET_SECONDS", 1000)
    monkeypatch.setattr("scripts.process_issues.REQUEST_TIMEOUT_SECONDS", 100)
    monkeypatch.setattr("scripts.process_issues.WORKER_CLEANUP_RESERVE_SECONDS", 1)

    result = asyncio.run(drain_pending_requests(object(), object(), clock=lambda: 0))

    assert result == (3, 1, False)
    assert processed_order == [1, 2, 3]


def test_drain_defers_when_full_request_budget_does_not_remain(monkeypatch):
    request = PendingRequest(
        issue={"id": 1, "number": 1, "state": "open"},
        comments=[],
        source=("issue", 1, "검색"),
        status_comment=None,
        identity="request-1",
    )
    process_calls = []

    async def fake_discover(session):
        return [request]

    async def fake_process(session, request, metrics, **kwargs):
        process_calls.append(request)
        return True

    monkeypatch.setattr("scripts.process_issues.discover_pending_requests", fake_discover)
    monkeypatch.setattr("scripts.process_issues.process_issue", fake_process)
    monkeypatch.setattr("scripts.process_issues.WORKER_BUDGET_SECONDS", 100)
    monkeypatch.setattr("scripts.process_issues.REQUEST_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr("scripts.process_issues.WORKER_CLEANUP_RESERVE_SECONDS", 20)

    result = asyncio.run(drain_pending_requests(object(), object(), clock=lambda: 0))

    assert result == (0, 0, True)
    assert process_calls == []


def test_run_metrics_writes_parseable_unicode_jsonl(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    clock_values = iter([10.0, 11.5])
    metrics = RunMetrics(tmp_path / "summary.jsonl", clock=lambda: next(clock_values))

    metrics.emit("worker_started", note="제습기")

    [record] = [
        json.loads(line)
        for line in (tmp_path / "summary.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert record["schema_version"] == 1
    assert record["record_type"] == "worker_started"
    assert record["elapsed_seconds"] == 1.5
    assert record["note"] == "제습기"
    assert record["timestamp"].endswith("Z")


def test_api_retries_transient_failure_then_succeeds(monkeypatch):
    class Response:
        def __init__(self, status, payload=None):
            self.status = status
            self.payload = payload
            self.headers = {"Retry-After": "0"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def text(self):
            return "temporarily unavailable"

        async def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.responses = [Response(503), Response(200, {"ok": True})]

        def request(self, method, url, json=None):
            return self.responses.pop(0)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("scripts.process_issues.asyncio.sleep", fake_sleep)

    assert asyncio.run(api(Session(), "GET", "/test")) == {"ok": True}
    assert sleeps == [0]


def test_api_exhaustion_is_recoverable(monkeypatch):
    class Response:
        status = 503
        headers = {"Retry-After": "0"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def text(self):
            return "temporarily unavailable"

    class Session:
        def request(self, method, url, json=None):
            return Response()

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr("scripts.process_issues.API_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.process_issues.asyncio.sleep", fake_sleep)

    with pytest.raises(RecoverableAPIError):
        asyncio.run(api(Session(), "GET", "/test"))


def test_api_transport_exhaustion_is_recoverable(monkeypatch):
    class Session:
        def request(self, method, url, json=None):
            raise aiohttp.ServerDisconnectedError("reset")

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr("scripts.process_issues.API_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr("scripts.process_issues.asyncio.sleep", fake_sleep)

    with pytest.raises(RecoverableAPIError) as captured:
        asyncio.run(api(Session(), "GET", "/test"))

    assert isinstance(captured.value.__cause__, aiohttp.ServerDisconnectedError)
