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

저장소 소유자·멤버·협업자가 issue를 열거나 다시 열면 검색 요청을 큐에 넣고, 처리 후 결과를 댓글로 남기고 닫습니다. 검색어는 신뢰된 사용자의 요청별 댓글 또는 issue 본문을 사용합니다. 정기 검색 cron은 없으며, 30분 간격 schedule은 중단된 큐를 복구하는 용도로만 실행됩니다.

요청 이벤트를 받으면 검색 worker가 대기 중이더라도 트리거된 issue에 접수 ACK를 먼저 남깁니다. worker는 이 ACK 댓글 하나를 `대기 → 검색 중 → 완료/실패` 상태와 결과로 계속 갱신하며, 실제 처리 run 링크와 마지막 갱신 시각·예상 잔여 시간을 표시합니다. 같은 요청 실행을 재시도해도 댓글은 중복 생성하지 않습니다.

저장소 소유자·멤버·협업자가 open issue에 새 댓글을 달면 댓글 본문을 검색어로 즉시 처리합니다. GitHub Actions 봇이 작성한 결과 댓글과 외부 사용자의 댓글은 검색어로 사용하지 않습니다.

worker는 ACK가 남은 요청을 오래된 순서로 한 run에서 최대 3건 처리합니다. Actions concurrency queue는 요청 run을 순서대로 보존하고, 취소로 요청이 남더라도 30분 간격 복구 sweep이 다시 큐를 확인합니다. 처리 중 들어온 새 댓글과 이미 닫힌 issue에 남은 queued ACK도 다음 worker가 이어받습니다.

검색 결과 댓글 등록이 성공하고 같은 issue에 대기·실패 요청이 없으면 issue를 자동으로 닫습니다. 크롤링이 실패한 경우에는 실패 상태를 표시하고 재시도할 수 있도록 open 상태를 유지하며, 실패 표시 뒤에 작성한 댓글 요청이 성공하면 이전 실패를 해소된 상태로 전환합니다. GitHub API 일시 오류는 제한적으로 재시도하고, 계속 실패하면 다음 worker가 이어받습니다. 같은 검색어를 다시 실행하려면 issue를 다시 열고, 다른 검색어는 새 issue를 만드는 것을 권장합니다. open issue에 새 댓글을 남기면 댓글마다 별도 요청으로 접수됩니다.

Issue 본문에는 검색어만 입력하는 것을 권장합니다.

```text
갤럭시 폴드 7
```

issue 생성·reopen·댓글 이벤트는 `max_regions=0`으로 실행해 발견된 서울·경기 전체 지역을 조회합니다. 수동 실행도 기본값은 전체이며, 빠른 확인이 필요할 때만 `max_regions`를 20~100으로 지정해 범위를 줄일 수 있습니다. 전체 검색은 실행 시간이 길고 `429` 제한이 발생할 수 있습니다.

전체 검색이 4시간을 넘기기 전에 완료 지역과 누적 결과를 이슈 댓글에 압축 체크포인트로 저장하고 새 workflow run을 호출합니다. 다음 run은 완료된 지역을 건너뛰고 남은 지역부터 이어서 검색하며, 모든 지역이 완료된 뒤에만 이슈를 닫습니다.

수집기는 도메인 동시성 2에서 시작해 최대 16까지 동적으로 조절합니다. 최근 40개 응답의 실패율이 15% 이상이면 동시성을 절반으로 낮추고 지연을 늘리며, 실패율이 2.5% 이하로 안정되면 한 단계씩 복구합니다. `429` 응답은 즉시 동시성을 절반으로 낮추고 `Retry-After` 또는 최소 60초의 도메인 지연을 적용합니다. Scrapy AutoThrottle도 함께 사용해 응답 지연시간에 맞춰 요청 간격을 조절합니다.

Issue worker는 `asyncio`로 Scrapy subprocess와 progress 파일을 감시하고, `aiohttp`로 GitHub 결과 댓글을 비동기 갱신합니다. 모든 지역 진행 이벤트는 로컬에 누적하되 GitHub 댓글은 첫 진행·30초 간격·최종 상태에만 갱신해 API 쓰기 폭주를 막습니다. 요청별 결과 파일과 `output/run-summary.jsonl` 메트릭은 artifact로 업로드됩니다.

요청 하나는 최대 150분, worker 전체는 최대 300분으로 제한하며 workflow는 상태·artifact 마무리 시간을 포함해 330분에 종료합니다. 남은 요청은 다음 event run이나 복구 sweep이 처리합니다. 그보다 긴 단일 수집은 지역 범위를 나눠 여러 issue로 처리하거나 self-hosted runner를 사용해야 합니다.

## 데이터 처리

- 저장 결과에는 원 응답의 판매자 객체가 포함될 수 있습니다.
- 결과 파일은 Git에서 제외되며 GitHub Actions artifact는 3일 후 삭제됩니다.
- 재배포 전 `user.webCrawlNotAllowed`와 개인정보 관련 필드를 검토하세요.
