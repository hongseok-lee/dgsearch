# Bug: ACKed GitHub requests could process the wrong issue or be lost

**Date Reported**: 2026-07-13
**Date Fixed**: 2026-07-13
**Reporter**: Live Actions audit
**Assignee**: Codex
**Severity**: HIGH
**Status**: FIXED

## Problem Description

The workflow acknowledged the triggering issue immediately, but the search worker ignored that
request identity, selected only the oldest open issue, processed one issue, and exited. GitHub's
single concurrency group can replace an older pending run, so an ACKed request could remain open
without any worker left to process it. Failures also left a stale progress comment with no visible
terminal state, and each region caused a GitHub comment PATCH.

## Evidence

- Run `29269439016`, triggered by issue #12, updated issue #2 instead.
- Run `29270901311` ACKed issue #14 and then remained pending behind that worker.
- The live crawl discovered 1,858 regions and updated its comment once per region.

## Root Cause

`scripts/process_issues.py` listed open issues, processed the first eligible issue, and returned.
It had no durable queued/completed state per ACK, no multi-request drain, no visible failure update,
and no progress publication interval.

## Solution

- Treat ACK markers as durable request identities and reuse the ACK as the request status comment.
- Preserve pending workflow runs with `queue: max` and discover queued or explicitly interrupted
  requests across paginated open and closed issues.
- Drain up to three requests inside a five-hour soft budget, with a per-request timeout.
- Coalesce progress comment writes to the first event, every 30 seconds, and terminal state.
- Emit append-only `output/run-summary.jsonl` records and request-specific result files.
- Run a twice-hourly recovery sweep for requests left queued by cancellation or interrupted jobs.
- Retry transient GitHub API failures, escalate stuck Scrapy children from terminate to kill, and
  keep failed requests open instead of allowing a later success to hide them.

## Verification

- Python regression tests cover completed/reopened identity, ACK reuse, failure visibility,
  multi-request drain, coalescing, and metrics JSONL.
- Node tests cover ACK creation and idempotency.
- Ruff, actionlint, and diff checks are required before publication.

## Rollback

Revert this change as one commit. It adds no database migration or external persistent service; the
hidden comment markers remain harmless if an older worker is restored.
