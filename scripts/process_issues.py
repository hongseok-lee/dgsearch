from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN", "")
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
MAX_REGIONS = os.getenv("DGSEARCH_MAX_REGIONS", "300")
MAX_RESULTS = int(os.getenv("DGSEARCH_MAX_COMMENT_RESULTS", "50"))
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def api(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dgsearch-actions",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"GitHub API {error.code}: {detail}") from error


def keyword_from_issue(issue: dict) -> str:
    return " ".join((issue.get("body") or "").split())[:100]


def marker(source_kind: str, source_id: int, keyword: str) -> str:
    digest = hashlib.sha256(f"{source_kind}:{source_id}:{keyword}".encode()).hexdigest()[:16]
    return f"<!-- dgsearch:{digest} -->"


def legacy_issue_marker(issue_id: int, keyword: str) -> str:
    digest = hashlib.sha256(f"{issue_id}:{keyword}".encode()).hexdigest()[:16]
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


def crawl(keyword: str, issue_number: int) -> list[dict]:
    output = Path("output")
    output.mkdir(exist_ok=True)
    destination = output / f"issue-{issue_number}.jsonl"
    destination.unlink(missing_ok=True)
    subprocess.run(
        [
            "scrapy", "crawl", "daangn",
            "-a", f"query={keyword}",
            "-a", "provinces=서울특별시,경기도",
            "-a", f"max_regions={MAX_REGIONS}",
            "-O", str(destination),
        ],
        check=True,
    )
    if not destination.exists():
        return []
    return [json.loads(line) for line in destination.read_text().splitlines() if line.strip()]


def is_relevant(item: dict, keyword: str) -> bool:
    haystack = (item.get("title") or "").casefold()
    tokens = [token.casefold() for token in keyword.split() if token.strip()]
    return bool(tokens) and all(token in haystack for token in tokens)


def unique_results(items: list[dict], keyword: str) -> list[dict]:
    unique = {}
    for item in items:
        if not is_relevant(item, keyword):
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


def format_comment(keyword: str, items: list[dict], issue_marker: str) -> str:
    shown = items[:MAX_RESULTS]
    lines = [
        issue_marker,
        f"`{keyword}` 서울·경기 검색 결과입니다.",
        "",
        f"- 고유 매물: {len(items)}개",
        f"- 조회 지역 상한: {MAX_REGIONS}개",
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


def main():
    if not TOKEN or not REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
    issues = api("GET", f"/repos/{REPOSITORY}/issues?state=open&sort=created&direction=asc&per_page=100")
    for issue in issues:
        if "pull_request" in issue:
            continue
        comments = api("GET", f"/repos/{REPOSITORY}/issues/{issue['number']}/comments?per_page=100")
        comment_bodies = [comment.get("body") or "" for comment in comments]
        for source_kind, source_id, keyword in sources_for_issue(issue, comments):
            source_marker = marker(source_kind, source_id, keyword)
            accepted_markers = [source_marker]
            if source_kind == "issue":
                accepted_markers.append(legacy_issue_marker(source_id, keyword))
            if any(any(value in body for value in accepted_markers) for body in comment_bodies):
                continue

            items = unique_results(crawl(keyword, issue["number"]), keyword)
            body = format_comment(keyword, items, source_marker)
            api("POST", f"/repos/{REPOSITORY}/issues/{issue['number']}/comments", {"body": body})
            print(
                f"processed {source_kind} {source_id} on issue #{issue['number']}: "
                f"{len(items)} unique results"
            )
            return
    print("no unprocessed open issues")


if __name__ == "__main__":
    main()
