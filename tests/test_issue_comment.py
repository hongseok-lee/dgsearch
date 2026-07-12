from scripts.process_issues import format_comment


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
