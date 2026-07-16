import { Octokit } from '@octokit/rest';

const SKIP_EXTENSIONS = [
  '.md', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.svg',
  '.ico', '.pdf', '.lock', '.yaml', '.yml', '.toml',
  '.cfg', '.ini', '.env.example', '.gitignore'
];

const CODE_EXTENSIONS = [
  '.py', '.js', '.ts', '.java', '.go', '.rb', '.php',
  '.cs', '.cpp', '.c', '.h', '.rs', '.kt', '.swift',
  '.sh', '.bash', '.sql', '.jsx', '.tsx', '.vue', '.mjs', '.cjs'
];

const MAX_DIFF_CHARS  = 24000;
const MAX_FILE_PATCH  = 4000;

function ext(filename) {
  const parts = filename.split('.');
  return parts.length > 1 ? '.' + parts[parts.length - 1].toLowerCase() : '';
}

function isCodeFile(filename) {
  const e = ext(filename);
  if (SKIP_EXTENSIONS.includes(e)) return false;
  if (CODE_EXTENSIONS.includes(e)) return true;
  // No extension → might be a script
  return !filename.includes('.');
}

function isGenerated(filename) {
  return /\/(dist|build|node_modules|vendor|\.next|__pycache__)\//.test(filename);
}

function truncate(text, max) {
  if (!text || text.length <= max) return text;
  return text.slice(0, max) + '\n... [PATCH TRUNCATED]';
}

export async function fetchPRData(token, owner, repo, prNumber) {
  const octokit = new Octokit({ auth: token });

  // Fetch PR metadata
  const { data: pr } = await octokit.pulls.get({ owner, repo, pull_number: prNumber });

  // Fetch file list with patches
  const { data: rawFiles } = await octokit.pulls.listFiles({
    owner, repo, pull_number: prNumber, per_page: 100
  });

  let totalChars = 0;
  const files = [];

  for (const file of rawFiles) {
    if (!isCodeFile(file.filename)) continue;
    if (file.status === 'removed') continue;
    if (isGenerated(file.filename)) continue;

    const patch = truncate(file.patch, MAX_FILE_PATCH);
    const chars = (patch || '').length;

    if (totalChars + chars > MAX_DIFF_CHARS) {
      files.push({
        filename: `[LIMIT REACHED — ${rawFiles.length - files.length} more files not analyzed]`,
        patch: null,
        status: 'skipped'
      });
      break;
    }

    files.push({
      filename: file.filename,
      status: file.status,
      additions: file.additions,
      deletions: file.deletions,
      patch: patch || '(binary or empty)'
    });

    totalChars += chars;
  }

  return {
    pr: {
      number: pr.number,
      title: pr.title,
      author: pr.user.login,
      repo: `${owner}/${repo}`,
      branch: pr.head.ref,
      base: pr.base.ref,
      url: pr.html_url,
      sha: pr.head.sha.slice(0, 7),
    },
    files,
    stats: {
      totalFiles: rawFiles.length,
      analyzedFiles: files.filter(f => f.patch && f.status !== 'skipped').length,
      totalChars,
    }
  };
}

export async function postComment(token, owner, repo, prNumber, body) {
  const octokit = new Octokit({ auth: token });
  await octokit.issues.createComment({
    owner, repo, issue_number: prNumber, body
  });
}
