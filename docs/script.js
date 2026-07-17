const CONFIG = {
  apiBaseUrl: "https://api.github.com",
  apiVersion: "2022-11-28",
  concurrentRequests: 4,
  maxPages: 50,
  logLimit: 80,
  zipCompressionLevel: 6,
};

const tabs = document.querySelectorAll("[data-tab]");
const panels = document.querySelectorAll("[data-panel]");
const exportForm = document.getElementById("export-form");
const repoInput = document.getElementById("repo-input");
const tokenInput = document.getElementById("token-input");
const commentsInput = document.getElementById("comments-input");
const rawInput = document.getElementById("raw-input");
const previewButton = document.getElementById("preview-button");
const downloadButton = document.getElementById("download-button");
const exportProgress = document.getElementById("export-progress");
const exportStatus = document.getElementById("export-status");
const statusDot = document.querySelector(".status-dot");
const exportSummary = document.getElementById("export-summary");
const summaryIssues = document.getElementById("summary-issues");
const summaryPrs = document.getElementById("summary-prs");
const summaryComments = document.getElementById("summary-comments");
const exportLog = document.getElementById("export-log");
const responsiveDetails = document.querySelectorAll("[data-responsive-details]");
const mobileMedia = window.matchMedia("(max-width: 760px)");

function syncResponsiveDetails() {
  responsiveDetails.forEach((details) => {
    details.open = !mobileMedia.matches;
  });
}

syncResponsiveDetails();
mobileMedia.addEventListener("change", syncResponsiveDetails);

tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tab;

    tabs.forEach((item) => {
      const active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-pressed", String(active));
    });

    panels.forEach((panel) => {
      panel.hidden = panel.dataset.panel !== target;
    });
  });
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copy);
    if (!target) {
      return;
    }

    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select text";
    }

    window.setTimeout(() => {
      button.textContent = original;
    }, 1400);
  });
});

function parseRepository(value) {
  const input = value.trim();
  if (!input) {
    throw new Error("Enter a GitHub repository URL or OWNER/REPO.");
  }

  const direct = input.replace(/\.git$/, "").match(/^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/);
  if (direct) {
    return { owner: direct[1], repo: direct[2] };
  }

  let url;
  try {
    url = new URL(input);
  } catch {
    throw new Error("Use a repository URL like https://github.com/OWNER/REPO or OWNER/REPO.");
  }

  if (!/^https?:$/.test(url.protocol) || url.hostname.toLowerCase() !== "github.com") {
    throw new Error("Only github.com repository URLs are supported.");
  }

  const parts = url.pathname.split("/").filter(Boolean);
  if (parts.length === 2) {
    const normalized = `${parts[0]}/${parts[1].replace(/\.git$/, "")}`;
    const match = normalized.match(/^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/);
    if (match) {
      return { owner: match[1], repo: match[2] };
    }
  }

  throw new Error("Use a repository URL like https://github.com/OWNER/REPO or OWNER/REPO.");
}

function repoSlug(repo) {
  return `${repo.owner}_${repo.repo}`.replace(/[^A-Za-z0-9._-]+/g, "_");
}

function setBusy(busy) {
  downloadButton.disabled = busy;
  previewButton.disabled = busy;
  repoInput.disabled = busy;
  tokenInput.disabled = busy;
  commentsInput.disabled = busy;
  rawInput.disabled = busy;
  exportProgress.hidden = !busy;
}

function setStatus(message, isError = false) {
  exportStatus.textContent = message;
  statusDot.classList.toggle("is-error", isError);
}

function setProgress(done, total) {
  exportProgress.value = total > 0 ? Math.round((done / total) * 100) : 0;
}

function addLog(message) {
  const item = document.createElement("li");
  item.textContent = message;
  exportLog.prepend(item);
  while (exportLog.children.length > CONFIG.logLimit) {
    exportLog.lastElementChild.remove();
  }
}

function resetUi() {
  exportLog.replaceChildren();
  exportSummary.hidden = false;
  summaryIssues.textContent = "—";
  summaryPrs.textContent = "—";
  summaryComments.textContent = "—";
  exportProgress.value = 0;
  setStatus("Ready to export.");
}

function requestHeaders(token) {
  const headers = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": CONFIG.apiVersion,
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

async function requestJson(path, token) {
  const response = await fetch(`${CONFIG.apiBaseUrl}${path}`, {
    headers: requestHeaders(token),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = data?.message || response.statusText;
    throw new Error(`GitHub API ${response.status}: ${message}`);
  }
  return data;
}

async function fetchPage(path, page, token) {
  const separator = path.includes("?") ? "&" : "?";
  return requestJson(`${path}${separator}per_page=100&page=${page}`, token);
}

async function fetchPaginated(path, token, label) {
  const rows = [];
  for (let page = 1; page <= CONFIG.maxPages; page += 1) {
    addLog(`${label}: page ${page}`);
    const data = await fetchPage(path, page, token);
    if (!Array.isArray(data)) {
      throw new Error(`${label} did not return a list.`);
    }
    rows.push(...data);
    if (data.length < 100) {
      return rows;
    }
  }
  throw new Error(`${label} exceeded ${CONFIG.maxPages * 100} rows. Use the Python CLI for very large repositories.`);
}

async function mapWithLimit(items, worker, onDone) {
  const results = new Array(items.length);
  let next = 0;
  let done = 0;
  let failed = false;

  async function run() {
    while (!failed && next < items.length) {
      const index = next;
      next += 1;
      try {
        results[index] = await worker(items[index], index);
      } catch (error) {
        failed = true;
        throw error;
      }
      if (failed) {
        return;
      }
      done += 1;
      onDone(done, items.length);
    }
  }

  const workers = Array.from({ length: Math.min(CONFIG.concurrentRequests, items.length) }, run);
  await Promise.all(workers);
  return results;
}

function sortComments(comments) {
  return [...comments].sort((left, right) => {
    const leftDate = Date.parse(left.created_at || left.createdAt || "") || Number.MAX_SAFE_INTEGER;
    const rightDate = Date.parse(right.created_at || right.createdAt || "") || Number.MAX_SAFE_INTEGER;
    return leftDate - rightDate;
  });
}

function authorLogin(comment) {
  return comment.user?.login || comment.author?.login || comment.author || "unknown";
}

function itemLine(label, value) {
  return `- ${label}: ${value || ""}`;
}

function commentsMarkdown(comments, included) {
  if (!included) {
    return "_Comments were not included in this browser export_";
  }
  if (!comments.length) {
    return "_No comments_";
  }
  return sortComments(comments)
    .map((comment) => {
      const body = comment.body?.trim() || "_No content_";
      return `### ${authorLogin(comment)} | ${comment.created_at || comment.createdAt || ""}\n\n${body}`;
    })
    .join("\n\n");
}

function issueMarkdown(issue, comments, included) {
  const body = issue.body?.trim() || "_No description_";
  return [
    `# Issue #${issue.number}: ${issue.title || ""}`,
    "",
    itemLine("URL", issue.html_url || issue.url),
    itemLine("State", (issue.state || "").toUpperCase()),
    itemLine("Created", issue.created_at),
    itemLine("Updated", issue.updated_at),
    "",
    "## Description",
    "",
    body,
    "",
    "## Comments",
    "",
    commentsMarkdown(comments, included),
    "",
  ].join("\n");
}

function prMarkdown(pr, issueComments, reviewComments, included) {
  const body = pr.body?.trim() || "_No description_";
  const comments = [...issueComments, ...reviewComments];
  return [
    `# PR #${pr.number}: ${pr.title || ""}`,
    "",
    itemLine("URL", pr.html_url || pr.url),
    itemLine("State", (pr.state || "").toUpperCase()),
    itemLine("Created", pr.created_at),
    itemLine("Updated", pr.updated_at),
    "",
    "## Description",
    "",
    body,
    "",
    "## Comments",
    "",
    commentsMarkdown(comments, included),
    "",
  ].join("\n");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function updateSummary(issues, prs, commentMaps) {
  const commentTotal = Object.values(commentMaps.issueComments).reduce((sum, rows) => sum + rows.length, 0)
    + Object.values(commentMaps.prIssueComments).reduce((sum, rows) => sum + rows.length, 0)
    + Object.values(commentMaps.prReviewComments).reduce((sum, rows) => sum + rows.length, 0);
  summaryIssues.textContent = String(issues.length);
  summaryPrs.textContent = String(prs.length);
  summaryComments.textContent = String(commentTotal);
  exportSummary.hidden = false;
}

async function fetchCommentMaps(repo, token, issues, prs) {
  const maps = {
    issueComments: {},
    prIssueComments: {},
    prReviewComments: {},
  };
  const tasks = [
    ...issues.map((issue) => ({ kind: "issue", number: issue.number })),
    ...prs.map((pr) => ({ kind: "pr-issue", number: pr.number })),
    ...prs.map((pr) => ({ kind: "pr-review", number: pr.number })),
  ];

  await mapWithLimit(
    tasks,
    async (task) => {
      if (task.kind === "issue") {
        maps.issueComments[task.number] = await fetchPaginated(
          `/repos/${repo.owner}/${repo.repo}/issues/${task.number}/comments`,
          token,
          `Issue #${task.number} comments`,
        );
      }
      if (task.kind === "pr-issue") {
        maps.prIssueComments[task.number] = await fetchPaginated(
          `/repos/${repo.owner}/${repo.repo}/issues/${task.number}/comments`,
          token,
          `PR #${task.number} comments`,
        );
      }
      if (task.kind === "pr-review") {
        maps.prReviewComments[task.number] = await fetchPaginated(
          `/repos/${repo.owner}/${repo.repo}/pulls/${task.number}/comments`,
          token,
          `PR #${task.number} review comments`,
        );
      }
    },
    (done, total) => {
      setStatus(`Fetched comments ${done}/${total}.`);
      setProgress(done, total);
    },
  );

  return maps;
}

async function fetchExportData(includeComments) {
  const repo = parseRepository(repoInput.value);
  const token = tokenInput.value.trim();
  addLog(`Repository: ${repo.owner}/${repo.repo}`);
  setStatus("Fetching repository metadata.");
  const metadata = await requestJson(`/repos/${repo.owner}/${repo.repo}`, token);

  setStatus("Fetching issues.");
  const allIssues = await fetchPaginated(`/repos/${repo.owner}/${repo.repo}/issues?state=all`, token, "Issues");
  const issues = allIssues.filter((issue) => !issue.pull_request);

  setStatus("Fetching pull requests.");
  const prs = await fetchPaginated(`/repos/${repo.owner}/${repo.repo}/pulls?state=all`, token, "Pull requests");

  const emptyMaps = {
    issueComments: Object.fromEntries(issues.map((issue) => [issue.number, []])),
    prIssueComments: Object.fromEntries(prs.map((pr) => [pr.number, []])),
    prReviewComments: Object.fromEntries(prs.map((pr) => [pr.number, []])),
  };
  const commentMaps = includeComments ? await fetchCommentMaps(repo, token, issues, prs) : emptyMaps;
  updateSummary(issues, prs, commentMaps);
  return { repo, metadata, allIssues, issues, prs, commentMaps, includeComments };
}

async function buildZip(data, includeRaw) {
  if (!window.JSZip) {
    throw new Error("ZIP library failed to load. Check your connection and reload the page.");
  }

  const zip = new window.JSZip();
  const slug = repoSlug(data.repo);
  const repoRoot = `export/${slug}`;
  const rawRoot = `export/raw/${slug}`;
  const summary = {
    repo: `${data.repo.owner}/${data.repo.repo}`,
    exported_at: new Date().toISOString(),
    issues: data.issues.length,
    pull_requests: data.prs.length,
    comments_included: data.includeComments,
    attachments: "Browser export keeps attachment URLs in Markdown. Use the Python CLI to mirror private assets.",
  };

  data.issues.forEach((issue) => {
    zip.file(
      `${repoRoot}/issues/ISSUE-${issue.number}.md`,
      issueMarkdown(issue, data.commentMaps.issueComments[issue.number] || [], data.includeComments),
    );
  });
  data.prs.forEach((pr) => {
    zip.file(
      `${repoRoot}/prs/PR-${pr.number}.md`,
      prMarkdown(
        pr,
        data.commentMaps.prIssueComments[pr.number] || [],
        data.commentMaps.prReviewComments[pr.number] || [],
        data.includeComments,
      ),
    );
  });
  zip.file(`${repoRoot}/summary.json`, JSON.stringify(summary, null, 2));
  zip.file(`${repoRoot}/README.md`, `# ${data.repo.owner}/${data.repo.repo} export\n\nExported at ${summary.exported_at}.\n`);

  if (includeRaw) {
    zip.file(`${rawRoot}/repo.json`, JSON.stringify(data.metadata, null, 2));
    zip.file(`${rawRoot}/issues.json`, JSON.stringify(data.allIssues, null, 2));
    zip.file(`${rawRoot}/prs.json`, JSON.stringify(data.prs, null, 2));
    data.issues.forEach((issue) => {
      zip.file(
        `${rawRoot}/issue_comments/ISSUE-${issue.number}.json`,
        JSON.stringify(data.commentMaps.issueComments[issue.number] || [], null, 2),
      );
    });
    data.prs.forEach((pr) => {
      zip.file(
        `${rawRoot}/pr_issue_comments/PR-${pr.number}.json`,
        JSON.stringify(data.commentMaps.prIssueComments[pr.number] || [], null, 2),
      );
      zip.file(
        `${rawRoot}/pr_review_comments/PR-${pr.number}.json`,
        JSON.stringify(data.commentMaps.prReviewComments[pr.number] || [], null, 2),
      );
    });
  }

  return zip.generateAsync({
    type: "blob",
    compression: "DEFLATE",
    compressionOptions: { level: CONFIG.zipCompressionLevel },
  });
}

async function runExport(download) {
  resetUi();
  setBusy(true);
  try {
    const data = await fetchExportData(commentsInput.checked);
    if (download) {
      setStatus("Building ZIP.");
      const blob = await buildZip(data, rawInput.checked);
      downloadBlob(blob, `${repoSlug(data.repo)}-github-export.zip`);
      setStatus("ZIP download started.");
      addLog("ZIP ready.");
    } else {
      setStatus("Preview ready.");
      addLog("Counts loaded. Use Download export ZIP to save files.");
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : "Export failed.";
    setStatus(message, true);
    addLog(message);
  } finally {
    setBusy(false);
  }
}

if (exportForm) {
  exportForm.addEventListener("submit", (event) => {
    event.preventDefault();
    runExport(true);
  });
  previewButton.addEventListener("click", () => {
    runExport(false);
  });
}
