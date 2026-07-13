from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from dgsearch.listings import is_tradable


API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "")
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
MAX_REGIONS = os.getenv("DGSEARCH_MAX_REGIONS", "0")
MAX_RESULTS = int(os.getenv("DGSEARCH_MAX_COMMENT_RESULTS", "50"))
PROGRESS_UPDATE_INTERVAL = float(os.getenv("DGSEARCH_PROGRESS_UPDATE_INTERVAL_SECONDS", "30"))
MAX_REQUESTS_PER_RUN = int(os.getenv("DGSEARCH_MAX_REQUESTS_PER_RUN", "3"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("DGSEARCH_REQUEST_TIMEOUT_SECONDS", "9000"))
WORKER_BUDGET_SECONDS = float(os.getenv("DGSEARCH_WORKER_BUDGET_SECONDS", "18000"))
WORKER_CLEANUP_RESERVE_SECONDS = float(os.getenv("DGSEARCH_WORKER_CLEANUP_RESERVE_SECONDS", "900"))
ACK_GRACE_SECONDS = float(os.getenv("DGSEARCH_ACK_GRACE_SECONDS", "60"))
API_RETRY_ATTEMPTS = int(os.getenv("DGSEARCH_API_RETRY_ATTEMPTS", "4"))
API_RETRY_MAX_SECONDS = float(os.getenv("DGSEARCH_API_RETRY_MAX_SECONDS", "120"))
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
BOT_LOGINS = {"github-actions", "github-actions[bot]"}
TERMINAL_STATES = {"completed", "failed", "superseded"}
SUMMARY_PATH = Path("output/run-summary.jsonl")
RETRYABLE_API_STATUSES = {403, 429, 500, 502, 503, 504}


class RecoverableAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingRequest:
    issue: dict
    comments: list[dict]
    source: tuple[str, int, str]
    status_comment: dict | None
    identity: str


def api_retry_delay(headers, attempt: int, *, now=time.time) -> float:
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 0), API_RETRY_MAX_SECONDS)
        except ValueError:
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            reset_delay = float(headers.get("X-RateLimit-Reset", "0")) - now()
            return min(max(reset_delay, 0), API_RETRY_MAX_SECONDS)
        except ValueError:
            pass
    return min(2**attempt, API_RETRY_MAX_SECONDS)


async def api(session: aiohttp.ClientSession, method: str, path: str, payload=None):
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            async with session.request(method, f"{API}{path}", json=payload) as response:
                if response.status < 400:
                    if response.status == 204:
                        return None
                    return await response.json()
                detail = await response.text()
                if response.status not in RETRYABLE_API_STATUSES:
                    raise RuntimeError(f"GitHub API {response.status}: {detail}")
                if attempt + 1 == API_RETRY_ATTEMPTS:
                    raise RecoverableAPIError(
                        f"GitHub API {response.status} after "
                        f"{API_RETRY_ATTEMPTS} attempts: {detail}"
                    )
                await asyncio.sleep(api_retry_delay(response.headers, attempt))
        except (aiohttp.ClientError, TimeoutError) as error:
            if attempt + 1 == API_RETRY_ATTEMPTS:
                raise RecoverableAPIError(
                    f"GitHub API transport failed after {API_RETRY_ATTEMPTS} "
                    f"attempts: {type(error).__name__}: {error}"
                ) from error
            await asyncio.sleep(min(2**attempt, API_RETRY_MAX_SECONDS))
    raise AssertionError("unreachable")


def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dgsearch-actions",
    }


def keyword_from_issue(issue: dict) -> str:
    return " ".join((issue.get("body") or "").split())[:100]


def marker(source_kind: str, source_id: int, keyword: str) -> str:
    digest = hashlib.sha256(f"{source_kind}:{source_id}:{keyword}".encode()).hexdigest()[:16]
    return f"<!-- dgsearch:{digest} -->"


def sources_for_issue(issue: dict, comments: list[dict]):
    sources = []
    if issue.get("author_association") in TRUSTED_ASSOCIATIONS:
        keyword = keyword_from_issue(issue)
        if keyword:
            sources.append(("issue", issue["id"], keyword))

    for comment in comments:
        body = comment.get("body") or ""
        author = (comment.get("user") or {}).get("login", "")
        if (
            comment.get("author_association") not in TRUSTED_ASSOCIATIONS
            or author == "github-actions[bot]"
            or "<!-- dgsearch:" in body
        ):
            continue
        keyword = " ".join(body.split())[:100]
        if keyword:
            sources.append(("comment", comment["id"], keyword))
    return sources


def latest_source_for_issue(issue: dict, comments: list[dict]):
    sources = sources_for_issue(issue, comments)
    return sources[-1] if sources else None


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def github_run_url() -> str | None:
    run_id = os.getenv("GITHUB_RUN_ID")
    if not run_id or not REPOSITORY:
        return None
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    return f"{server}/{REPOSITORY}/actions/runs/{run_id}"


def hidden_markers(body: str) -> list[str]:
    return [
        line.strip()
        for line in (body or "").splitlines()
        if line.strip().startswith("<!-- dgsearch:") and line.strip().endswith("-->")
    ]


def is_bot_comment(comment: dict) -> bool:
    return (comment.get("user") or {}).get("login", "") in BOT_LOGINS


def ack_marker_for_source(comment: dict, source_kind: str, source_id: int) -> str | None:
    for candidate in hidden_markers(comment.get("body") or ""):
        if source_kind == "comment" and candidate == f"<!-- dgsearch:ack:comment:{source_id} -->":
            return candidate
        if source_kind == "issue" and candidate.startswith(f"<!-- dgsearch:ack:issue:{source_id}:"):
            return candidate
    return None


def issue_ack_event_at(ack_marker: str, source_id: int) -> datetime | None:
    prefix = f"<!-- dgsearch:ack:issue:{source_id}:"
    if not ack_marker.startswith(prefix):
        return None
    request_key = ack_marker.removeprefix(prefix).removesuffix(" -->")
    _, separator, timestamp = request_key.partition(":")
    return parse_github_timestamp(timestamp) if separator else None


def latest_comment_created_at(comments: list[dict]) -> datetime | None:
    timestamps = [
        parsed
        for comment in comments
        if (parsed := parse_github_timestamp(comment.get("created_at"))) is not None
    ]
    return max(timestamps, default=None)


def find_status_comment(
    comments: list[dict],
    source_kind: str,
    source_id: int,
    source_marker: str,
) -> dict | None:
    candidates = []
    terminal_candidates = []
    for index, comment in enumerate(comments):
        if not is_bot_comment(comment):
            continue
        markers = hidden_markers(comment.get("body") or "")
        if source_marker not in markers and not ack_marker_for_source(
            comment, source_kind, source_id
        ):
            continue
        ordered = (comment.get("created_at") or "", index, comment)
        candidates.append(ordered)
        if source_marker in markers and status_state(comment) in TERMINAL_STATES:
            terminal_candidates.append(ordered)
    latest = max(candidates, default=(None, None, None))[-1]
    if latest is None or not terminal_candidates:
        return latest
    latest_ack = ack_marker_for_source(latest, source_kind, source_id)
    latest_terminal = max(terminal_candidates)[-1]
    if latest_ack and (source_kind == "comment" or ":opened:" in latest_ack):
        return latest_terminal
    if latest_ack and source_kind == "issue" and ":reopened:" in latest_ack:
        event_at = issue_ack_event_at(latest_ack, source_id)
        terminal_at = latest_comment_created_at(
            [candidate[-1] for candidate in terminal_candidates]
        )
        if event_at and terminal_at and terminal_at >= event_at:
            return latest_terminal
    return latest


def explicit_status_state(comment: dict | None) -> str | None:
    if comment is None:
        return None
    for candidate in hidden_markers(comment.get("body") or ""):
        prefix = "<!-- dgsearch:state:"
        if candidate.startswith(prefix):
            return candidate.removeprefix(prefix).removesuffix(" -->")
    return None


def status_state(comment: dict | None) -> str | None:
    if comment is None:
        return None
    explicit_state = explicit_status_state(comment)
    if explicit_state:
        return explicit_state

    body = comment.get("body") or ""
    if "- 진행: 완료 (" in body or "- 상태: ✅ 완료" in body:
        return "completed"
    if "- 상태: ❌ 실패" in body:
        return "failed"
    if ack_marker_for_source(comment, "comment", -1) or "<!-- dgsearch:ack:" in body:
        return "queued"
    return "running"


def source_created_at(
    issue: dict,
    comments: list[dict],
    source_kind: str,
    source_id: int,
) -> str | None:
    if source_kind == "issue":
        return issue.get("updated_at") or issue.get("created_at")
    for comment in comments:
        if comment.get("id") == source_id:
            return comment.get("created_at")
    return None


def parse_github_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def legacy_source_ready(
    issue: dict,
    comments: list[dict],
    source_kind: str,
    source_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    created_at = source_created_at(issue, comments, source_kind, source_id)
    created = parse_github_timestamp(created_at)
    if created is None:
        return True
    current = now or datetime.now(UTC)
    return (current - created).total_seconds() >= ACK_GRACE_SECONDS


def request_identity(
    source_kind: str,
    source_id: int,
    source_marker: str,
    status_comment: dict | None,
) -> str:
    if status_comment:
        ack_marker = ack_marker_for_source(status_comment, source_kind, source_id)
        if ack_marker:
            return ack_marker
    return source_marker


def status_markers(status_comment: dict | None, source_marker: str) -> list[str]:
    markers = []
    if status_comment:
        markers.extend(
            candidate
            for candidate in hidden_markers(status_comment.get("body") or "")
            if candidate.startswith("<!-- dgsearch:ack:")
        )
    if source_marker not in markers:
        markers.append(source_marker)
    return markers


def pending_requests_for_issue(issue: dict, comments: list[dict]) -> list[PendingRequest]:
    requests = []
    latest_source = latest_source_for_issue(issue, comments)
    for source in sources_for_issue(issue, comments):
        source_kind, source_id, keyword = source
        source_marker = marker(source_kind, source_id, keyword)
        status_comment = find_status_comment(comments, source_kind, source_id, source_marker)
        state = status_state(status_comment)
        has_ack = bool(
            status_comment and ack_marker_for_source(status_comment, source_kind, source_id)
        )
        legacy_open_request = (
            issue.get("state", "open") == "open"
            and source == latest_source
            and status_comment is None
            and legacy_source_ready(issue, comments, source_kind, source_id)
        )
        recoverable_status = (
            status_comment is not None
            and state not in TERMINAL_STATES
            and (
                issue.get("state", "open") == "open"
                or has_ack
                or explicit_status_state(status_comment) is not None
            )
        )
        if not has_ack and not legacy_open_request and not recoverable_status:
            continue
        if state in TERMINAL_STATES:
            continue
        requests.append(
            PendingRequest(
                issue=issue,
                comments=comments,
                source=source,
                status_comment=status_comment,
                identity=request_identity(source_kind, source_id, source_marker, status_comment),
            )
        )
    return requests


def load_trigger_context() -> dict:
    context = {
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "repository": REPOSITORY or None,
        "event_name": os.getenv("GITHUB_EVENT_NAME"),
        "sha": os.getenv("GITHUB_SHA"),
        "trigger_issue_number": None,
        "trigger_issue_id": None,
        "trigger_comment_id": None,
        "trigger_action": None,
    }
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path:
        return context
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return context
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    context.update(
        trigger_issue_number=issue.get("number"),
        trigger_issue_id=issue.get("id"),
        trigger_comment_id=comment.get("id"),
        trigger_action=event.get("action"),
    )
    return context


class RunMetrics:
    def __init__(self, path: Path = SUMMARY_PATH, clock=time.monotonic):
        self.path = path
        self.clock = clock
        self.started_at = clock()
        self.context = load_trigger_context()

    def emit(self, record_type: str, **fields) -> None:
        record = {
            "schema_version": 1,
            "record_type": record_type,
            "timestamp": utc_timestamp(),
            "elapsed_seconds": round(self.clock() - self.started_at, 3),
            **self.context,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as error:
            print(f"warning: could not write run metrics: {error}", file=sys.stderr)


async def stop_subprocess(process, *, terminate_timeout: float = 10) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=terminate_timeout)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def crawl_incrementally(
    keyword: str,
    issue_number: int,
    on_progress,
    *,
    output_key: str | None = None,
) -> list[dict]:
    output = Path("output")
    output.mkdir(exist_ok=True)
    stem = f"issue-{issue_number}"
    if output_key:
        stem = f"{stem}-{output_key}"
    destination = output / f"{stem}.jsonl"
    progress = output / f"{stem}-progress.jsonl"
    destination.unlink(missing_ok=True)
    progress.write_text("", encoding="utf-8")
    command = (
        "scrapy",
        "crawl",
        "daangn",
        "-a",
        f"query={keyword}",
        "-a",
        "provinces=서울특별시,경기도",
        "-a",
        f"max_regions={MAX_REGIONS}",
        "-a",
        f"progress_file={progress}",
        "-O",
        str(destination),
    )
    process = await asyncio.create_subprocess_exec(*command)
    position = 0
    try:
        while process.returncode is None:
            position = await consume_progress(progress, position, on_progress)
            try:
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except TimeoutError:
                pass
        position = await consume_progress(progress, position, on_progress)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, "scrapy crawl daangn")
    except BaseException:
        try:
            await stop_subprocess(process)
        except Exception as cleanup_error:
            print(
                f"warning: could not stop Scrapy subprocess: {cleanup_error}",
                file=sys.stderr,
            )
        raise
    if not destination.exists():
        return []
    text = await asyncio.to_thread(destination.read_text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def consume_progress(path: Path, position: int, on_progress) -> int:
    events, position = await asyncio.to_thread(read_progress, path, position)
    for event in events:
        await on_progress(event)
    return position


def read_progress(path: Path, position: int):
    events = []
    with path.open(encoding="utf-8") as stream:
        stream.seek(position)
        for line in stream:
            if line.strip():
                events.append(json.loads(line))
        return events, stream.tell()


def is_relevant(item: dict, keyword: str) -> bool:
    haystack = (item.get("title") or "").casefold()
    tokens = [token.casefold() for token in keyword.split() if token.strip()]
    return bool(tokens) and all(token in haystack for token in tokens)


def unique_results(items: list[dict], keyword: str) -> list[dict]:
    unique = {}
    for item in items:
        if not is_tradable(item) or not is_relevant(item, keyword):
            continue
        key = item.get("id") or item.get("href")
        if key and key not in unique:
            unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: item.get("boostedAt") or item.get("createdAt") or "",
        reverse=True,
    )


def cell(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def region_scope_label() -> str:
    return "전체" if int(MAX_REGIONS) == 0 else f"최대 {MAX_REGIONS}개"


def eta_label(seconds: float | None) -> str:
    if seconds is None:
        return "계산 중"
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"약 {minutes}분"
    hours, remainder = divmod(minutes, 60)
    return f"약 {hours}시간 {remainder}분" if remainder else f"약 {hours}시간"


def status_label(status: str) -> str:
    return {
        "running": "🔄 검색 중",
        "completed": "✅ 완료",
        "failed": "❌ 실패",
        "interrupted": "⏳ 자동 재시도 대기",
    }[status]


def format_comment(
    keyword: str,
    items: list[dict],
    request_markers: str | list[str],
    *,
    completed: int | None = None,
    total: int | None = None,
    failed: int = 0,
    status: str = "running",
    updated_at: str | None = None,
    eta_seconds: float | None = None,
    run_url: str | None = None,
    error_type: str | None = None,
) -> str:
    shown = items[:MAX_RESULTS]
    markers = [request_markers] if isinstance(request_markers, str) else request_markers
    lines = [
        *markers,
        f"<!-- dgsearch:state:{status} -->",
        f"`{keyword}` 서울·경기 검색 결과입니다.",
        "",
        f"- 상태: {status_label(status)}",
        f"- 고유 매물: {len(items)}개",
        f"- 조회 지역: {region_scope_label()}",
        f"- 진행: {progress_label(completed, total, status)}",
        f"- 실패 지역: {failed}개",
        f"- 댓글 표시: 최근 갱신 {len(shown)}개",
        f"- 마지막 갱신: {updated_at or utc_timestamp()}",
        "",
    ]
    if status == "running":
        lines.insert(-1, f"- 예상 남은 시간: {eta_label(eta_seconds)}")
    if run_url:
        lines.insert(-1, f"- 실행: [GitHub Actions]({run_url})")
    if status == "failed":
        reason = f" ({error_type})" if error_type else ""
        lines.extend(
            [
                f"> 검색 작업이 중단되었습니다{reason}. 이슈는 열린 상태로 유지됩니다.",
                "> 새 댓글로 다시 요청하면 재시도합니다.",
                "",
            ]
        )
    if status == "interrupted":
        lines.extend(
            [
                "> GitHub API 일시 오류로 중단되었습니다. 다음 worker가 자동 재시도합니다.",
                "",
            ]
        )
    lines.extend(["| 가격 | 지역 | 매물 |", "|---:|---|---|"])
    for item in shown:
        price = item.get("price")
        try:
            price_text = f"{int(float(price)):,}원"
        except (TypeError, ValueError):
            price_text = "가격 미정"
        region = (item.get("region") or item.get("regionId") or {}).get("name", "")
        title = cell(item.get("title"))
        url = item.get("href", "")
        lines.append(f"| {price_text} | {cell(region)} | [{title}]({url}) |")
    lines.extend(
        [
            "",
            "> 검색 결과는 지역별 노출과 요청 제한의 영향을 받으며 전체 매물을 보장하지 않습니다. 판매자 정보는 댓글에 포함하지 않습니다.",
        ]
    )
    return "\n".join(lines)


def progress_label(completed, total, status):
    if completed is None:
        return "지역 목록 준비 중"
    if status == "completed":
        return f"완료 ({completed}/{total or completed})"
    if status in {"failed", "interrupted"}:
        return f"중단 ({completed}/{total or '?'})"
    return f"{completed}/{total or '?'}"


async def create_result_comment(session, issue_number: int, body: str) -> int:
    result = await api(
        session,
        "POST",
        f"/repos/{REPOSITORY}/issues/{issue_number}/comments",
        {"body": body},
    )
    return result["id"]


async def update_result_comment(session, comment_id: int, body: str):
    await api(
        session,
        "PATCH",
        f"/repos/{REPOSITORY}/issues/comments/{comment_id}",
        {"body": body},
    )


async def close_issue(session, issue_number: int):
    await api(
        session,
        "PATCH",
        f"/repos/{REPOSITORY}/issues/{issue_number}",
        {"state": "closed"},
    )


async def reopen_issue(session, issue_number: int):
    await api(
        session,
        "PATCH",
        f"/repos/{REPOSITORY}/issues/{issue_number}",
        {"state": "open"},
    )


async def paginate(session, path: str) -> list[dict]:
    items = []
    page = 1
    while True:
        page_path = path if page == 1 else f"{path}&page={page}"
        batch = await api(session, "GET", page_path)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


async def list_repository_issues(session) -> list[dict]:
    return await paginate(
        session,
        f"/repos/{REPOSITORY}/issues?state=all&sort=created&direction=asc&per_page=100",
    )


async def list_issue_comments(session, issue_number: int) -> list[dict]:
    return await paginate(
        session,
        f"/repos/{REPOSITORY}/issues/{issue_number}/comments?per_page=100",
    )


async def discover_pending_requests(session) -> list[PendingRequest]:
    pending = []
    for issue in await list_repository_issues(session):
        if "pull_request" in issue:
            continue
        comments = await list_issue_comments(session, issue["number"])
        pending.extend(pending_requests_for_issue(issue, comments))
    return pending


class ProgressReporter:
    def __init__(
        self,
        session,
        comment_id: int,
        keyword: str,
        markers: list[str],
        issue_number: int,
        metrics: RunMetrics,
        *,
        interval: float = PROGRESS_UPDATE_INTERVAL,
        clock=time.monotonic,
    ):
        self.session = session
        self.comment_id = comment_id
        self.keyword = keyword
        self.markers = markers
        self.issue_number = issue_number
        self.metrics = metrics
        self.interval = interval
        self.clock = clock
        self.started_at = clock()
        self.last_progress_update_at = None
        self.completed = 0
        self.total = None
        self.failed = 0
        self.raw_progress_events = 0
        self.progress_comment_updates = 0
        self.terminal_comment_updates = 0
        self.found = {}

    def apply(self, event: dict) -> None:
        self.raw_progress_events += 1
        self.completed = event["completed"]
        self.total = event["total"]
        self.failed += int(bool(event.get("error")))
        for item in unique_results(event.get("articles", []), self.keyword):
            key = item.get("id") or item.get("href")
            if key:
                self.found.setdefault(key, item)

    def eta_seconds(self, now: float | None = None) -> float | None:
        if not self.completed or not self.total or self.completed >= self.total:
            return None
        elapsed = (now if now is not None else self.clock()) - self.started_at
        return elapsed / self.completed * (self.total - self.completed)

    async def on_progress(self, event: dict) -> None:
        self.apply(event)
        now = self.clock()
        if (
            self.last_progress_update_at is not None
            and now - self.last_progress_update_at < self.interval
        ):
            return
        await self.publish("running", now=now)

    async def publish(
        self,
        status: str,
        *,
        now: float | None = None,
        items: list[dict] | None = None,
        error_type: str | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        shown_items = sorted_results(self.found.values()) if items is None else items
        await update_result_comment(
            self.session,
            self.comment_id,
            format_comment(
                self.keyword,
                shown_items,
                self.markers,
                completed=self.completed,
                total=self.total,
                failed=self.failed,
                status=status,
                eta_seconds=self.eta_seconds(now),
                run_url=github_run_url(),
                error_type=error_type,
            ),
        )
        if status == "running":
            self.last_progress_update_at = now
            self.progress_comment_updates += 1
        else:
            self.terminal_comment_updates += 1
        elapsed = max(now - self.started_at, 0)
        self.metrics.emit(
            "progress_published",
            selected_issue_number=self.issue_number,
            comment_id=self.comment_id,
            status=status,
            completed_regions=self.completed,
            total_regions=self.total,
            failed_regions=self.failed,
            unique_results=len(shown_items),
            raw_progress_events=self.raw_progress_events,
            progress_comment_updates=self.progress_comment_updates,
            terminal_comment_updates=self.terminal_comment_updates,
            coalesced_events=max(self.raw_progress_events - self.progress_comment_updates, 0),
            regions_per_minute=(round(self.completed / elapsed * 60, 3) if elapsed > 0 else None),
        )


async def prepare_status_comment(
    session,
    issue: dict,
    source: tuple[str, int, str],
    existing: dict | None,
) -> tuple[int, list[str]]:
    source_kind, source_id, keyword = source
    source_marker = marker(source_kind, source_id, keyword)
    markers = status_markers(existing, source_marker)
    body = format_comment(
        keyword,
        [],
        markers,
        status="running",
        run_url=github_run_url(),
    )
    if existing:
        await update_result_comment(session, existing["id"], body)
        return existing["id"], markers
    return await create_result_comment(session, issue["number"], body), markers


def failed_comments_superseded_by(
    issue: dict,
    comments: list[dict],
    current_source: tuple[str, int, str],
) -> list[dict]:
    current_kind, current_id, _ = current_source
    if current_kind != "comment":
        return []
    retry_created_at = parse_github_timestamp(
        source_created_at(issue, comments, current_kind, current_id)
    )
    if retry_created_at is None:
        return []

    superseded = []
    seen = set()
    for source_kind, source_id, keyword in sources_for_issue(issue, comments):
        if (source_kind, source_id) == (current_kind, current_id):
            continue
        source_marker = marker(source_kind, source_id, keyword)
        status_comment = find_status_comment(comments, source_kind, source_id, source_marker)
        if status_state(status_comment) != "failed":
            continue
        failed_at = parse_github_timestamp(
            status_comment.get("updated_at") or status_comment.get("created_at")
        )
        if failed_at is None or retry_created_at <= failed_at:
            continue
        if status_comment["id"] not in seen:
            superseded.append(status_comment)
            seen.add(status_comment["id"])
    return superseded


async def supersede_resolved_failures(
    session,
    issue: dict,
    current_source: tuple[str, int, str],
) -> int:
    comments = await list_issue_comments(session, issue["number"])
    superseded = failed_comments_superseded_by(issue, comments, current_source)
    for comment in superseded:
        body = (comment.get("body") or "").replace(
            "<!-- dgsearch:state:failed -->",
            "<!-- dgsearch:state:superseded -->",
        )
        body = body.replace("- 상태: ❌ 실패", "- 상태: ↪️ 이후 요청으로 대체됨")
        body += (
            f"\n\n> issue #{issue['number']}의 이후 댓글 요청이 성공해 이 실패 상태를 해소했습니다."
        )
        await update_result_comment(session, comment["id"], body)
    return len(superseded)


def has_failed_requests(issue: dict, comments: list[dict]) -> bool:
    for source_kind, source_id, keyword in sources_for_issue(issue, comments):
        source_marker = marker(source_kind, source_id, keyword)
        status_comment = find_status_comment(comments, source_kind, source_id, source_marker)
        if status_state(status_comment) == "failed":
            return True
    return False


async def should_close_issue(session, issue_number: int) -> bool:
    comments = await list_issue_comments(session, issue_number)
    issue = await api(session, "GET", f"/repos/{REPOSITORY}/issues/{issue_number}")
    return not pending_requests_for_issue(issue, comments) and not has_failed_requests(
        issue, comments
    )


async def process_issue(
    session,
    request: PendingRequest,
    metrics: RunMetrics,
    *,
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    reporter_clock=time.monotonic,
) -> bool:
    issue = request.issue
    source_kind, source_id, keyword = request.source
    keyword_hash = hashlib.sha256(keyword.encode()).hexdigest()[:16]
    metrics.emit(
        "request_started",
        selected_issue_number=issue["number"],
        selected_issue_id=issue.get("id"),
        source_kind=source_kind,
        source_id=source_id,
        request_identity=request.identity,
        keyword_sha256_16=keyword_hash,
        trigger_selected_mismatch=(
            metrics.context.get("trigger_issue_number") is not None
            and metrics.context["trigger_issue_number"] != issue["number"]
        ),
    )

    reporter = None
    try:
        async with asyncio.timeout(request_timeout_seconds):
            if issue.get("state") == "closed":
                await reopen_issue(session, issue["number"])
            comment_id, markers = await prepare_status_comment(
                session, issue, request.source, request.status_comment
            )
            reporter = ProgressReporter(
                session,
                comment_id,
                keyword,
                markers,
                issue["number"],
                metrics,
                clock=reporter_clock,
            )
            crawled = await crawl_incrementally(
                keyword,
                issue["number"],
                reporter.on_progress,
                output_key=f"{source_kind}-{source_id}",
            )
            items = unique_results(crawled, keyword)
            await reporter.publish("completed", items=items)
            await supersede_resolved_failures(session, issue, request.source)
            if await should_close_issue(session, issue["number"]):
                await close_issue(session, issue["number"])
            metrics.emit(
                "request_finished",
                outcome="success",
                selected_issue_number=issue["number"],
                source_kind=source_kind,
                source_id=source_id,
                completed_regions=reporter.completed,
                total_regions=reporter.total,
                failed_regions=reporter.failed,
                unique_results=len(items),
                raw_progress_events=reporter.raw_progress_events,
                progress_comment_updates=reporter.progress_comment_updates,
                terminal_comment_updates=reporter.terminal_comment_updates,
            )
            print(
                f"processed request on issue #{issue['number']} from {source_kind} "
                f"{source_id}: {len(items)} unique results"
            )
        return True
    except asyncio.CancelledError:
        metrics.emit(
            "request_finished",
            outcome="cancelled",
            selected_issue_number=issue["number"],
            source_kind=source_kind,
            source_id=source_id,
        )
        raise
    except Exception as error:
        recoverable = isinstance(error, RecoverableAPIError)
        if reporter is not None:
            try:
                await reporter.publish(
                    "interrupted" if recoverable else "failed",
                    error_type=type(error).__name__,
                )
            except Exception as comment_error:
                print(
                    f"warning: could not publish failure status: {comment_error}",
                    file=sys.stderr,
                )
        metrics.emit(
            "request_finished",
            outcome="retryable_failure" if recoverable else "failure",
            selected_issue_number=issue["number"],
            source_kind=source_kind,
            source_id=source_id,
            error_type=type(error).__name__,
            error_message=str(error)[:500],
            completed_regions=reporter.completed if reporter else 0,
            total_regions=reporter.total if reporter else None,
            failed_regions=reporter.failed if reporter else 0,
            unique_results=len(reporter.found) if reporter else 0,
            raw_progress_events=reporter.raw_progress_events if reporter else 0,
            progress_comment_updates=(reporter.progress_comment_updates if reporter else 0),
            terminal_comment_updates=(reporter.terminal_comment_updates if reporter else 0),
        )
        print(
            f"request failed on issue #{issue['number']}: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return False


async def drain_pending_requests(
    session,
    metrics: RunMetrics,
    *,
    clock=time.monotonic,
) -> tuple[int, int, bool]:
    started_at = clock()
    attempted = set()
    processed = 0
    failures = 0
    deferred = False

    while processed < MAX_REQUESTS_PER_RUN:
        candidates = [
            candidate
            for candidate in await discover_pending_requests(session)
            if candidate.identity not in attempted
        ]
        if not candidates:
            break
        request = candidates[0]
        attempted.add(request.identity)
        remaining = WORKER_BUDGET_SECONDS - (clock() - started_at)
        required = REQUEST_TIMEOUT_SECONDS + WORKER_CLEANUP_RESERVE_SECONDS
        if remaining < required:
            deferred = True
            break
        success = await process_issue(
            session,
            request,
            metrics,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        processed += 1
        failures += int(not success)

    if processed >= MAX_REQUESTS_PER_RUN:
        deferred = bool(
            [
                candidate
                for candidate in await discover_pending_requests(session)
                if candidate.identity not in attempted
            ]
        )
    return processed, failures, deferred


async def main():
    if not TOKEN or not REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
    metrics = RunMetrics()
    metrics.emit("worker_started")
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(headers=github_headers(), timeout=timeout) as session:
            processed, failures, deferred = await drain_pending_requests(session, metrics)
    except asyncio.CancelledError:
        metrics.emit("worker_finished", outcome="cancelled")
        raise
    except Exception as error:
        metrics.emit(
            "worker_finished",
            outcome="failure",
            error_type=type(error).__name__,
            error_message=str(error)[:500],
        )
        raise

    outcome = "failure" if failures else "partial" if deferred else "success"
    metrics.emit(
        "worker_finished",
        outcome=outcome,
        processed_requests=processed,
        failed_requests=failures,
        deferred_requests=deferred,
    )
    if not processed:
        print("no queued issue requests")
    if deferred:
        print("queued requests remain for the next worker run")
    if failures:
        raise RuntimeError(f"{failures} request(s) failed")


def sorted_results(items):
    return sorted(
        items,
        key=lambda item: item.get("boostedAt") or item.get("createdAt") or "",
        reverse=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
