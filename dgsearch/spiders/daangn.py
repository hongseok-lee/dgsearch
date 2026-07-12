from __future__ import annotations

import hashlib
from urllib.parse import urlencode

import scrapy


SEOUL = [
    "종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구",
    "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구",
    "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구",
]

GYEONGGI = [
    "수원시", "고양시", "용인시", "성남시", "부천시", "화성시", "안산시", "남양주시",
    "안양시", "평택시", "시흥시", "파주시", "의정부시", "김포시", "광주시", "광명시",
    "군포시", "하남시", "오산시", "양주시", "이천시", "구리시", "안성시", "포천시",
    "의왕시", "여주시", "동두천시", "과천시", "가평군", "양평군", "연천군",
]


def solve_pow(challenge: str, difficulty: int) -> int:
    prefix = "0" * difficulty
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).hexdigest()
        if digest.startswith(prefix):
            return nonce
        nonce += 1


class DaangnSpider(scrapy.Spider):
    name = "daangn"
    allowed_domains = ["www.daangn.com"]
    handle_httpstatus_list = [403, 429]

    def __init__(self, query="갤럭시 폴드 7", provinces="서울특별시,경기도", max_regions="0", **kwargs):
        super().__init__(**kwargs)
        self.query = query
        self.provinces = {value.strip() for value in provinces.split(",") if value.strip()}
        self.max_regions = int(max_regions)
        self.regions: dict[int, dict] = {}
        self.pending_seed_requests = 0

    async def start(self):
        seeds = []
        if "서울특별시" in self.provinces:
            seeds.extend(("서울특별시", name) for name in SEOUL)
        if "경기도" in self.provinces:
            seeds.extend(("경기도", name) for name in GYEONGGI)
        self.pending_seed_requests = len(seeds)

        for province, seed in seeds:
            params = urlencode({"keyword": f"{province} {seed}"})
            yield scrapy.Request(
                f"https://www.daangn.com/kr/api/v1/regions/keyword?{params}",
                callback=self.parse_region_seed,
                cb_kwargs={"province": province, "seed": seed},
            )

    def parse_region_seed(self, response, province, seed):
        if response.status == 200:
            for location in response.json().get("locations", []):
                name2 = location.get("name2") or ""
                if location.get("depth") == 3 and location.get("name1") == province and seed in name2:
                    self.regions[location["id"]] = location
        else:
            self.logger.error("region seed failed: %s %s status=%s", province, seed, response.status)

        self.pending_seed_requests -= 1
        if self.pending_seed_requests == 0:
            regions = list(self.regions.values())
            if self.max_regions > 0:
                regions = regions[: self.max_regions]
            self.logger.info("discovered %d regions; scheduling %d", len(self.regions), len(regions))
            for region in regions:
                yield self.loader_request(region)

    def loader_request(self, region, attempt=0):
        params = urlencode({"in": f"{region['name']}-{region['id']}", "search": self.query})
        page_url = f"https://www.daangn.com/kr/buy-sell/s/?{params}"
        loader_url = f"{page_url}&_data={urlencode({'x': 'routes/kr.buy-sell.s'})[2:]}"
        return scrapy.Request(
            loader_url,
            callback=self.parse_loader,
            cb_kwargs={"region": region, "attempt": attempt},
            dont_filter=True,
        )

    def parse_loader(self, response, region, attempt):
        if response.status in {403, 429}:
            if attempt < 8:
                yield self.loader_request(region, attempt + 1)
            return

        data = response.json()
        if str(data.get("region", {}).get("id")) != str(region["id"]):
            self.logger.error("region mismatch wanted=%s got=%s", region["id"], data.get("region"))
            return

        pow_data = data["pow"]
        params = urlencode({
            "region_id": region["id"],
            "search": self.query,
            "uri": pow_data["uri"],
            "nonce": solve_pow(pow_data["challenge"], pow_data["difficulty"]),
            "expires_at": pow_data["expiresAt"],
        })
        yield scrapy.Request(
            f"https://www.daangn.com/kr/api/v1/fleamarket/search?{params}",
            callback=self.parse_results,
            cb_kwargs={"region": region, "attempt": attempt},
            dont_filter=True,
            meta={"dont_cache": True},
        )

    def parse_results(self, response, region, attempt):
        if response.status in {403, 429}:
            if attempt < 8:
                yield self.loader_request(region, attempt + 1)
            return

        for article in response.json().get("fleamarketArticles", []):
            article["matchedSearchRegion"] = {
                "id": region["id"],
                "name": region["name"],
                "name1": region["name1"],
                "name2": region["name2"],
            }
            yield article
