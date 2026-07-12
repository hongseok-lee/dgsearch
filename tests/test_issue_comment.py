import asyncio

from scripts.process_issues import (
    consume_progress,
    format_comment,
    is_relevant,
    latest_source_for_issue,
    main,
    marker,
    progress_label,
    region_scope_label,
    sources_for_issue,
    unique_results,
)
from dgsearch.listings import is_tradable


def test_format_comment_uses_markdown_table(monkeypatch):
    monkeypatch.setattr("scripts.process_issues.MAX_REGIONS", "10")
    body = format_comment(
        "갤럭시 폴드 7",
        [{
            "title": "폴드7 | 블루",
            "price": "1250000.0",
            "region": {"name": "시흥동"},
            "href": "https://example.com/item",
        }],
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
    ]

    assert sources_for_issue(issue, comments) == [
        ("issue", 10, "첫 검색"),
        ("comment", 20, "아이폰 17 프로"),
    ]
    assert marker("comment", 20, "아이폰 17 프로") != marker("comment", 23, "아이폰 17 프로")
    assert latest_source_for_issue(issue, comments) == ("comment", 20, "아이폰 17 프로")


def test_progress_labels():
    assert progress_label(None, None, False) == "지역 목록 준비 중"
    assert progress_label(3, 10, False) == "3/10"
    assert progress_label(10, 10, True) == "완료 (10/10)"


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


def test_open_issue_is_searched_even_when_old_result_marker_exists(monkeypatch):
    issue = {
        "id": 10,
        "number": 7,
        "body": "갤럭시 폴드7",
        "author_association": "OWNER",
    }
    old_result = {
        "id": 20,
        "body": "<!-- dgsearch:old-result -->",
        "author_association": "NONE",
        "user": {"login": "github-actions[bot]"},
    }
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
        if path.endswith("/issues?state=open&sort=created&direction=asc&per_page=100"):
            return [issue]
        if path.endswith("/issues/7/comments?per_page=100"):
            return [old_result]
        calls.append((method, path, payload))
        if method == "POST" and path.endswith("/issues/7/comments"):
            return {"id": 99}
        return {}

    async def fake_crawl(keyword, issue_number, on_progress):
        await on_progress({"completed": 1, "total": 2, "articles": [item], "error": None})
        await on_progress({"completed": 2, "total": 2, "articles": [item], "error": None})
        return [item, item]

    monkeypatch.setattr("scripts.process_issues.TOKEN", "token")
    monkeypatch.setattr("scripts.process_issues.REPOSITORY", "owner/repo")
    monkeypatch.setattr("scripts.process_issues.api", fake_api)
    monkeypatch.setattr("scripts.process_issues.crawl_incrementally", fake_crawl)

    asyncio.run(main())

    assert calls[0][0:2] == ("POST", "/repos/owner/repo/issues/7/comments")
    updates = [call for call in calls if call[1] == "/repos/owner/repo/issues/comments/99"]
    assert len(updates) == 3
    assert "- 고유 매물: 1개" in updates[0][2]["body"]
    assert "- 진행: 1/2" in updates[0][2]["body"]
    assert "- 진행: 완료 (2/2)" in updates[-1][2]["body"]
    assert calls[-1] == ("PATCH", "/repos/owner/repo/issues/7", {"state": "closed"})
