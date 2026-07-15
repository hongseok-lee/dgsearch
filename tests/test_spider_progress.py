import json

from scrapy.http import Request, TextResponse

from dgsearch.spiders.daangn import DaangnSpider


def test_spider_appends_one_progress_event_per_region(tmp_path):
    path = tmp_path / "progress.jsonl"
    spider = DaangnSpider(progress_file=str(path))
    spider.total_regions = 2
    region = {
        "id": 1,
        "name": "부암동",
        "name1": "서울특별시",
        "name2": "종로구",
    }

    spider.report_region(region, [{"id": "one"}])
    spider.report_region(region, [], "429 exhausted")

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["completed"] for event in events] == [1, 2]
    assert all(event["total"] == 2 for event in events)
    assert events[0]["articles"] == [{"id": "one"}]
    assert events[1]["error"] == "429 exhausted"


def test_spider_skips_checkpointed_regions_and_reports_cumulative_progress():
    spider = DaangnSpider(skip_region_ids="1")
    spider.pending_seed_requests = 1
    request = Request("https://example.com")
    response = TextResponse(
        request=request,
        url=request.url,
        body=json.dumps(
            {
                "locations": [
                    {"id": 1, "depth": 3, "name": "부암동", "name1": "서울특별시", "name2": "종로구"},
                    {"id": 2, "depth": 3, "name": "청운동", "name1": "서울특별시", "name2": "종로구"},
                ]
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        encoding="utf-8",
    )

    requests = list(spider.parse_region_seed(response, "서울특별시", "종로구"))

    assert spider.total_regions == 2
    assert spider.completed_regions == 1
    assert len(requests) == 1
    assert requests[0].cb_kwargs["region"]["id"] == 2
