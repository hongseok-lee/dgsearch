import json

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
