# 🛡️ Secure Code Auditor CLI

AI-powered security auditor for GitHub Pull Requests.  
Uses an **Actor → Critic** multi-agent pipeline to detect OWASP Top 10 vulnerabilities with minimal false positives.

---

## Features

- 🔍 **Real GitHub PR analysis** — fetches the actual diff, not the full codebase
- 🧠 **Dual-agent pipeline** — Actor scans, Critic validates and filters false positives
- 🔀 **Claude or Ollama** — toggle between cloud (Anthropic) and local (Ollama) models
- 💬 **Auto-posts** a formatted Markdown report as a GitHub PR comment
- 🔐 **Privacy-first** — only the diff is sent, no full files, no persistent storage

---

## Installation

```bash
# Clone the repo
git clone https://github.com/your-org/secure-auditor
cd secure-auditor

# Install dependencies
npm install

# Make the CLI available globally (optional)
npm link
```

---

## Setup

Run the interactive config wizard once:

```bash
node src/index.js config
# or if linked globally:
secure-auditor config
```

This will ask for:
- **GitHub Personal Access Token** — needs `repo` + `pull_requests:write` scopes
- **AI provider** — `claude` (Anthropic API) or `ollama` (local)
- **Anthropic API key** — if using Claude
- **Ollama URL & model** — if using Ollama

Config is stored in `~/.secure-auditor/config.json` (permissions: 600).

---

## Usage

### Audit a Pull Request (Claude)

```bash
node src/index.js audit --repo acme-corp/backend-api --pr 142
```

### Audit with Ollama (local model)

```bash
node src/index.js audit --repo acme-corp/backend-api --pr 142 --model ollama
```

### Save report to file

```bash
node src/index.js audit --repo acme-corp/backend-api --pr 142 --output report.md
```

### Audit without posting GitHub comment

```bash
node src/index.js audit --repo acme-corp/backend-api --pr 142 --no-comment
```

---

## All Options

```
Options:
  -r, --repo <owner/repo>     GitHub repository (required)
  -p, --pr <number>           Pull Request number (required)
  -m, --model <provider>      AI provider: claude | ollama (default: claude)
  --ollama-model <name>       Ollama model name (default: codellama)
  --no-comment                Skip posting comment to GitHub PR
  --output <file>             Save report to file (e.g. report.md)
```

---

## Architecture

```
CLI (index.js)
  └─ audit command
       ├─ 1. fetchPRData()     ← GitHub API → raw diff
       ├─ 2. runActor()        ← LLM: scan diff → JSON findings
       ├─ 3. runCritic()       ← LLM: validate findings → filtered report
       ├─ 4. generateReport()  ← JSON → Markdown
       └─ 5. postComment()     ← GitHub API → PR comment
```

### AI Providers

| Provider | Model | Speed | Privacy | Cost |
|----------|-------|-------|---------|------|
| Claude (Anthropic) | claude-sonnet-4-6 | Fast | API (no retention) | ~$0.01–0.05/PR |
| Ollama | codellama / llama3 | Slower | 100% local | Free |

---

## Security Notes

- GitHub token stored with chmod 600 in `~/.secure-auditor/config.json`
- Only the **diff** is sent to the AI — never full file contents
- Sensitive patterns (API keys, passwords) are scrubbed before sending
- For enterprise: use Ollama for fully on-premise operation

---

## Example Output

```
  ╔═══════════════════════════════════════════════╗
  ║   🛡️  SECURE CODE AUDITOR  v1.0               ║
  ║   AI-Powered Security Review for GitHub PRs   ║
  ╚═══════════════════════════════════════════════╝

  ┌──────────────────────────────────────────────────┐
  │  Auditing PR #142 in acme-corp/backend-api        │
  └──────────────────────────────────────────────────┘
  ✔ Fetched PR: "feat: add user profile endpoint"
  ℹ Files to analyze: 3 / 5
  ℹ AI provider: CLAUDE

  1/3  Running AI Auditor (Actor)...
  ✔ Actor complete — 2 potential finding(s)

  2/3  Running AI Validator (Critic)...
  ✔ Critic complete — 1 confirmed, 1 rejected as false positive

  ┌──────────────────────────────────────────────────┐
  │  Security Report                                  │
  └──────────────────────────────────────────────────┘

  Overall Risk:  HIGH 

   HIGH      ✅ CONFIRMED
  SQL Injection via unsanitized user input
  A03 | CWE-89 | Confidence: 92%
  📍 src/api/users.py  lines 47-52

  3/3  Generating Markdown report...
  ✔ Comment posted to PR #142
```
