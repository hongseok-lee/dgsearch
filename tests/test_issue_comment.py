from scripts.process_issues import (
    format_comment,
    is_relevant,
    legacy_issue_marker,
    marker,
    publish_results_and_close,
    sources_for_issue,
    unique_results,
)


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


def test_relevance_requires_every_keyword_token():
    fold5 = {
        "id": "5",
        "title": "삼성 갤럭시 Z 폴드5 512GB",
        "content": "갤럭시 폴드 7도 검색해 보세요",
    }
    fold7 = {"id": "7", "title": "삼성 갤럭시 Z 폴드7 256GB", "content": ""}

    assert not is_relevant(fold5, "갤럭시 폴드 7")
    assert is_relevant(fold7, "갤럭시 폴드 7")
    assert unique_results([fold5, fold7], "갤럭시 폴드 7") == [fold7]


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
    assert legacy_issue_marker(10, "첫 검색") == "<!-- dgsearch:8d6eff7f2e0cfaae -->"


def test_publish_results_comments_before_closing(monkeypatch):
    calls = []
    monkeypatch.setattr("scripts.process_issues.REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        "scripts.process_issues.api",
        lambda method, path, payload=None: calls.append((method, path, payload)),
    )

    publish_results_and_close(7, "result table")

    assert calls == [
        ("POST", "/repos/owner/repo/issues/7/comments", {"body": "result table"}),
        ("PATCH", "/repos/owner/repo/issues/7", {"state": "closed"}),
    ]
