const assert = require("node:assert/strict");
const test = require("node:test");

const acknowledge = require("../.github/scripts/acknowledge.cjs");

function issueContext(overrides = {}) {
  return {
    eventName: "issues",
    payload: {
      action: "opened",
      issue: {
        id: 10,
        number: 7,
        updated_at: "2026-07-13T18:00:00Z",
      },
    },
    repo: { owner: "owner", repo: "dgsearch" },
    runId: 1234,
    serverUrl: "https://github.com",
    ...overrides,
  };
}

function fakeGithub(comments = []) {
  const created = [];
  const listComments = () => {};
  return {
    created,
    github: {
      paginate: async (method, params) => {
        assert.equal(method, listComments);
        assert.equal(params.per_page, 100);
        return comments;
      },
      rest: {
        issues: {
          listComments,
          createComment: async (params) => {
            created.push(params);
            return { data: { id: 99 } };
          },
        },
      },
    },
  };
}

const core = { info() {} };

test("creates one ACK on the triggering issue", async () => {
  const { github, created } = fakeGithub();
  const result = await acknowledge({ github, context: issueContext(), core });

  assert.equal(result.created, true);
  assert.equal(created.length, 1);
  assert.equal(created[0].issue_number, 7);
  assert.equal(
    created[0].body,
    [
      "<!-- dgsearch:ack:issue:10:opened:2026-07-13T18:00:00Z -->",
      "✅ 검색 요청을 접수했습니다.",
      "",
      "[Actions 실행 상태 보기](https://github.com/owner/dgsearch/actions/runs/1234)",
    ].join("\n"),
  );
});

test("uses the comment id as the request key", () => {
  const context = issueContext({
    eventName: "issue_comment",
    payload: {
      action: "created",
      issue: { id: 10, number: 7 },
      comment: { id: 22 },
    },
  });

  assert.equal(acknowledge.requestKey(context), "comment:22");
});

test("does not duplicate an existing bot ACK after pagination", async () => {
  const marker = acknowledge.markerFor(issueContext());
  const comments = Array.from({ length: 100 }, (_, index) => ({
    id: index + 1,
    body: "ordinary comment",
    user: { login: "someone" },
  }));
  comments.push({
    id: 101,
    body: `${marker}\n✅ 검색 요청을 접수했습니다.`,
    user: { login: "github-actions[bot]" },
  });
  const { github, created } = fakeGithub(comments);

  const result = await acknowledge({ github, context: issueContext(), core });

  assert.deepEqual(result, { created: false, commentId: 101, marker });
  assert.equal(created.length, 0);
});

test("creates a fresh key after a later reopen", () => {
  const first = issueContext();
  const reopened = issueContext({
    payload: {
      action: "reopened",
      issue: {
        id: 10,
        number: 7,
        updated_at: "2026-07-14T09:00:00Z",
      },
    },
  });

  assert.notEqual(acknowledge.requestKey(first), acknowledge.requestKey(reopened));
});
