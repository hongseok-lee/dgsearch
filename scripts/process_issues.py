from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import aiohttp

from dgsearch.listings import is_tradable


API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "")
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
MAX_REGIONS = os.getenv("DGSEARCH_MAX_REGIONS", "0")
MAX_RESULTS = int(os.getenv("DGSEARCH_MAX_COMMENT_RESULTS", "50"))
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


async def api(session: aiohttp.ClientSession, method: str, path: str, payload=None):
    async with session.request(method, f"{API}{path}", json=payload) as response:
        if response.status >= 400:
            detail = await response.text()
            raise RuntimeError(f"GitHub API {response.status}: {detail}")
        if response.status == 204:
            return None
        return await response.json()


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


async def crawl_incrementally(keyword: str, issue_number: int, on_progress) -> list[dict]:
    output = Path("output")
    output.mkdir(exist_ok=True)
    destination = output / f"issue-{issue_number}.jsonl"
    progress = output / f"issue-{issue_number}-progress.jsonl"
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
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=10)
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


def format_comment(
    keyword: str,
    items: list[dict],
    issue_marker: str,
    *,
    completed: int | None = None,
    total: int | None = None,
    failed: int = 0,
    finished: bool = False,
) -> str:
    shown = items[:MAX_RESULTS]
    lines = [
        issue_marker,
        f"`{keyword}` 서울·경기 검색 결과입니다.",
        "",
        f"- 고유 매물: {len(items)}개",
        f"- 조회 지역: {region_scope_label()}",
        f"- 진행: {progress_label(completed, total, finished)}",
        f"- 실패 지역: {failed}개",
        f"- 댓글 표시: 최근 갱신 {len(shown)}개",
        "",
        "| 가격 | 지역 | 매물 |",
        "|---:|---|---|",
    ]
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
    lines.extend([
        "",
        "> 검색 결과는 지역별 노출과 요청 제한의 영향을 받으며 전체 매물을 보장하지 않습니다. 판매자 정보는 댓글에 포함하지 않습니다.",
    ])
    return "\n".join(lines)


def progress_label(completed, total, finished):
    if completed is None:
        return "지역 목록 준비 중"
    if finished:
        return f"완료 ({completed}/{total or completed})"
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


async def main():
    if not TOKEN or not REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=github_headers(), timeout=timeout) as session:
        issues = await api(
            session,
            "GET",
            f"/repos/{REPOSITORY}/issues?state=open&sort=created&direction=asc&per_page=100",
        )
        for issue in issues:
            if "pull_request" in issue:
                continue
            comments = await api(
                session,
                "GET",
                f"/repos/{REPOSITORY}/issues/{issue['number']}/comments?per_page=100",
            )
            source = latest_source_for_issue(issue, comments)
            if source is None:
                continue
            await process_issue(session, issue, source)
            return
    print("no unprocessed open issues")


async def process_issue(session, issue, source):
    source_kind, source_id, keyword = source
    source_marker = marker(source_kind, source_id, keyword)
    comment_id = await create_result_comment(
        session,
        issue["number"],
        format_comment(keyword, [], source_marker),
    )
    found = {}
    state = {"completed": 0, "total": None, "failed": 0}

    async def on_progress(event):
        state["completed"] = event["completed"]
        state["total"] = event["total"]
        state["failed"] += int(bool(event.get("error")))
        for item in unique_results(event.get("articles", []), keyword):
            key = item.get("id") or item.get("href")
            if key:
                found.setdefault(key, item)
        await update_result_comment(
            session,
            comment_id,
            format_comment(
                keyword,
                sorted_results(found.values()),
                source_marker,
                completed=state["completed"],
                total=state["total"],
                failed=state["failed"],
            ),
        )

    items = unique_results(
        await crawl_incrementally(keyword, issue["number"], on_progress),
        keyword,
    )
    await update_result_comment(
        session,
        comment_id,
        format_comment(
            keyword,
            items,
            source_marker,
            completed=state["completed"],
            total=state["total"],
            failed=state["failed"],
            finished=True,
        ),
    )
    await close_issue(session, issue["number"])
    print(
        f"processed open issue #{issue['number']} from {source_kind} {source_id}: "
        f"{len(items)} unique results"
    )


def sorted_results(items):
    return sorted(
        items,
        key=lambda item: item.get("boostedAt") or item.get("createdAt") or "",
        reverse=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
