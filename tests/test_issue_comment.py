from scripts.process_issues import format_comment, is_relevant, unique_results


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
