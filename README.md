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

API가 `status: Ongoing`으로 표시한 현재 거래 가능 매물만 저장하고 댓글에 표시합니다. 예약·판매완료 상태와 상태 정보가 없는 항목은 제외합니다.

범위를 제한할 수 있습니다.

```bash
scrapy crawl daangn \
  -a query='갤럭시 폴드 7' \
  -a provinces='서울특별시' \
  -a max_regions=20
```

## GitHub Actions

Actions 탭의 **Search Daangn regions**에서 `Run workflow`를 선택하고 검색어를 입력합니다. 결과 JSONL과 실행 통계는 workflow artifact로 업로드됩니다.

저장소 소유자·멤버·협업자가 issue를 열거나 다시 열면 즉시 검색한 뒤 결과를 댓글로 남기고 닫습니다. 검색어는 신뢰된 사용자의 최신 댓글을 우선 사용하며, 댓글이 없으면 issue 본문을 사용합니다. 예약 cron은 사용하지 않습니다.

저장소 소유자·멤버·협업자가 open issue에 새 댓글을 달면 댓글 본문을 검색어로 즉시 처리합니다. GitHub Actions 봇이 작성한 결과 댓글과 외부 사용자의 댓글은 검색어로 사용하지 않습니다.

검색 결과 댓글 등록이 성공하면 처리가 끝난 issue를 자동으로 닫습니다. 크롤링이나 댓글 등록이 실패한 경우에는 재시도할 수 있도록 open 상태를 유지합니다. 닫힌 issue에서 새 검색을 요청하려면 issue를 다시 연 뒤 댓글을 작성해야 합니다.

Issue 본문에는 검색어만 입력하는 것을 권장합니다.

```text
갤럭시 폴드 7
```

전체 서울·경기 지역은 실행 시간이 길고 `429` 제한이 발생할 수 있습니다. 수동 실행은 처음에 `max_regions`를 20~100으로 설정해 확인하세요. issue 이벤트 작업은 실행 시간과 요청량을 제한하기 위해 최대 300개 지역을 조회하고 한 번에 issue 하나만 처리합니다.

수집기는 도메인 동시성 2에서 시작해 최대 8까지 동적으로 조절합니다. 최근 40개 응답의 실패율이 15% 이상이면 동시성을 절반으로 낮추고 지연을 늘리며, 실패율이 2.5% 이하로 안정되면 한 단계씩 복구합니다. `429` 응답은 즉시 동시성을 절반으로 낮추고 `Retry-After` 또는 최소 60초의 도메인 지연을 적용합니다. Scrapy AutoThrottle도 함께 사용해 응답 지연시간에 맞춰 요청 간격을 조절합니다.

GitHub-hosted runner의 job 실행 상한에 맞춰 workflow timeout은 최대 360분(6시간)입니다. 그보다 긴 수집은 한 번의 job으로 실행할 수 없으므로 지역 범위를 나눠 여러 issue로 처리하거나 self-hosted runner를 사용해야 합니다.

## 데이터 처리

- 저장 결과에는 원 응답의 판매자 객체가 포함될 수 있습니다.
- 결과 파일은 Git에서 제외되며 GitHub Actions artifact는 3일 후 삭제됩니다.
- 재배포 전 `user.webCrawlNotAllowed`와 개인정보 관련 필드를 검토하세요.
