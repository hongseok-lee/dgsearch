function requestKey(context) {
  if (context.eventName === "issue_comment") {
    return `comment:${context.payload.comment.id}`;
  }

  const issue = context.payload.issue;
  return `issue:${issue.id}:${context.payload.action}:${issue.updated_at}`;
}

function markerFor(context) {
  return `<!-- dgsearch:ack:${requestKey(context)} -->`;
}

function firstLine(body) {
  return (body || "").split(/\r?\n/, 1)[0].trim();
}

async function acknowledge({ github, context, core }) {
  const issueNumber = context.payload.issue.number;
  const marker = markerFor(context);
  const comments = await github.paginate(github.rest.issues.listComments, {
    ...context.repo,
    issue_number: issueNumber,
    per_page: 100,
  });
  const existing = comments.find(
    (comment) =>
      comment.user?.login === "github-actions[bot]" && firstLine(comment.body) === marker,
  );

  if (existing) {
    core.info(`ACK already exists as comment ${existing.id}`);
    return { created: false, commentId: existing.id, marker };
  }

  const serverUrl = context.serverUrl || process.env.GITHUB_SERVER_URL || "https://github.com";
  const runUrl = `${serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`;
  const response = await github.rest.issues.createComment({
    ...context.repo,
    issue_number: issueNumber,
    body: [
      marker,
      "✅ 검색 요청을 접수했습니다.",
      "",
      `[Actions 실행 상태 보기](${runUrl})`,
    ].join("\n"),
  });

  core.info(`Created ACK comment ${response.data.id}`);
  return { created: true, commentId: response.data.id, marker };
}

module.exports = acknowledge;
module.exports.requestKey = requestKey;
module.exports.markerFor = markerFor;
