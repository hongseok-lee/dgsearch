# dgsearch

서울·경기 여러 지역에서 당근 중고거래 검색 결과를 수집하는 저속 Scrapy 크롤러입니다.

당근 웹 검색 페이지의 공개 응답을 사용하며 로그인이나 프록시 회전 같은 차단 우회를 시도하지 않습니다. `429 Too Many Requests`가 발생하면 요청 속도를 낮추고, 검색용 PoW가 만료된 경우 새 challenge를 받아 다시 계산합니다.

Scrapy의 robots 미들웨어는 검색 loader 경로를 차단하므로 이 프로젝트의 `ROBOTSTXT_OBEY`는 `False`입니다. 실행자는 당근의 현재 이용약관, robots 정책 및 관련 법규를 직접 확인하고 허용된 범위에서만 사용해야 합니다.

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
scrapy crawl daangn -a query='갤럭시 폴드 7'
```

결과는 기본적으로 `output/results.jsonl`에 저장됩니다. 같은 매물이 여러 검색 지역에서 노출될 수 있으므로 후처리 시 `id` 또는 `href` 기준 중복 제거가 필요합니다.

범위를 제한할 수 있습니다.

```bash
scrapy crawl daangn \
  -a query='갤럭시 폴드 7' \
  -a provinces='서울특별시' \
  -a max_regions=20
```

## GitHub Actions

Actions 탭의 **Search Daangn regions**에서 `Run workflow`를 선택하고 검색어를 입력합니다. 결과 JSONL과 실행 통계는 workflow artifact로 업로드됩니다.

또한 5분마다 open issue를 확인합니다. 처리되지 않은 가장 오래된 issue의 본문을 검색어로 사용하고, 결과를 해당 issue 댓글로 남깁니다. 동일 본문은 댓글의 숨은 해시 마커로 한 번만 처리되며 본문이 바뀌면 다시 검색됩니다. GitHub Actions 예약 실행의 최소 간격이 5분이므로 1분 주기는 사용할 수 없습니다.

Issue 본문에는 검색어만 입력하는 것을 권장합니다.

```text
갤럭시 폴드 7
```

전체 서울·경기 지역은 실행 시간이 길고 `429` 제한이 발생할 수 있습니다. 수동 실행은 처음에 `max_regions`를 20~100으로 설정해 확인하세요. 예약 issue 작업은 실행 시간과 요청량을 제한하기 위해 최대 300개 지역을 조회하고 한 번에 issue 하나만 처리합니다.

## 데이터 처리

- 저장 결과에는 원 응답의 판매자 객체가 포함될 수 있습니다.
- 결과 파일은 Git에서 제외되며 GitHub Actions artifact는 3일 후 삭제됩니다.
- 재배포 전 `user.webCrawlNotAllowed`와 개인정보 관련 필드를 검토하세요.
