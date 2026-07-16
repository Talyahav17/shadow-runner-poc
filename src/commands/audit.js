import chalk from 'chalk';
import ora from 'ora';
import fs from 'fs';
import { loadConfig } from './config.js';
import { fetchPRData, postComment } from '../github/client.js';
import { runActor } from '../agents/actor.js';
import { runCritic } from '../agents/critic.js';
import { generateReport } from '../ui/report.js';
import {
  sectionHeader, stepLog, successLog, warnLog, errorLog, infoLog,
  severityBadge, verdictBadge
} from '../ui/banner.js';

const RISK_COLOR = {
  CRITICAL: chalk.bgRed.white,
  HIGH:     chalk.bgYellow.black,
  MEDIUM:   chalk.bgBlue.white,
  LOW:      chalk.bgGreen.black,
  CLEAN:    chalk.bgGreen.white,
};

export async function auditPR(options) {
  // ── 0. Load config ─────────────────────────────────────────────────────
  const config = loadConfig();

  const githubToken = config.githubToken;
  if (!githubToken) {
    errorLog('GitHub token not found. Run: secure-auditor config');
    process.exit(1);
  }

  const provider = options.model || config.defaultModel || 'claude';
  if (provider === 'claude' && !config.anthropicKey) {
    errorLog('Anthropic API key not found. Run: secure-auditor config');
    process.exit(1);
  }
  if (provider === 'ollama') {
    infoLog(`Using Ollama model: ${options.ollamaModel || config.ollamaModel || 'codellama'}`);
  }

  const aiConfig = {
    provider,
    anthropicKey: config.anthropicKey,
    ollamaUrl:    config.ollamaUrl    || 'http://localhost:11434',
    ollamaModel:  options.ollamaModel || config.ollamaModel || 'codellama',
  };

  const [owner, repo] = options.repo.split('/');
  const prNumber = options.pr;

  // ── 1. Fetch PR data ───────────────────────────────────────────────────
  sectionHeader(`Auditing PR #${prNumber} in ${options.repo}`);
  let spinner = ora({ text: 'Fetching PR data from GitHub...', prefixText: '  ' }).start();

  let prData;
  try {
    prData = await fetchPRData(githubToken, owner, repo, prNumber);
    spinner.succeed(chalk.green(`Fetched PR: "${prData.pr.title}"`));
  } catch (err) {
    spinner.fail(chalk.red(`Failed to fetch PR: ${err.message}`));
    process.exit(1);
  }

  console.log('');
  infoLog(`Author: @${prData.pr.author}`);
  infoLog(`Branch: ${prData.pr.branch} → ${prData.pr.base}`);
  infoLog(`Files to analyze: ${prData.stats.analyzedFiles} / ${prData.stats.totalFiles}`);
  infoLog(`AI provider: ${chalk.cyan(provider.toUpperCase())}`);
  console.log('');

  if (prData.stats.analyzedFiles === 0) {
    warnLog('No code files found in this PR to analyze.');
    process.exit(0);
  }

  // ── 2. Actor: Security Audit ───────────────────────────────────────────
  stepLog(1, 3, `Running AI Auditor (Actor) — scanning ${prData.stats.analyzedFiles} files...`);
  spinner = ora({ text: 'Analyzing diff for vulnerabilities...', prefixText: '  ' }).start();

  let actorReport;
  try {
    actorReport = await runActor(aiConfig, prData);
    const count = actorReport.findings?.length || 0;
    spinner.succeed(chalk.green(`Actor complete — ${count} potential finding(s) identified`));
  } catch (err) {
    spinner.fail(chalk.red(`Actor failed: ${err.message}`));
    process.exit(1);
  }

  // ── 3. Critic: Validation ──────────────────────────────────────────────
  stepLog(2, 3, 'Running AI Validator (Critic) — peer reviewing Actor findings...');
  spinner = ora({ text: 'Validating findings, filtering false positives...', prefixText: '  ' }).start();

  let criticReport;
  try {
    criticReport = await runCritic(aiConfig, prData, actorReport);
    const { findings_confirmed, findings_rejected } = criticReport.validation_metadata;
    spinner.succeed(
      chalk.green(`Critic complete — ${findings_confirmed} confirmed, ${findings_rejected} rejected as false positives`)
    );
  } catch (err) {
    spinner.fail(chalk.red(`Critic failed: ${err.message}`));
    process.exit(1);
  }

  // ── 4. Print results to terminal ───────────────────────────────────────
  sectionHeader('Security Report');

  const riskLevel = criticReport.validation_metadata.final_risk_level;
  const riskFn    = RISK_COLOR[riskLevel] || chalk.bgGray.white;
  console.log('');
  console.log(`  Overall Risk: ${riskFn(` ${riskLevel} `)}`);
  console.log('');

  const confirmed = criticReport.validated_findings.filter(vf => vf.verdict !== 'REJECTED');
  const rejected  = criticReport.validated_findings.filter(vf => vf.verdict === 'REJECTED');

  if (confirmed.length === 0) {
    successLog('No security vulnerabilities found! ✨');
  } else {
    for (const vf of confirmed) {
      const f = vf.finding;
      const conf = Math.round((vf.validator_confidence || 0) * 100);
      console.log('');
      console.log(`  ${severityBadge(vf.final_severity)} ${verdictBadge(vf.verdict)}`);
      console.log(`  ${chalk.bold.white(f.title)}`);
      console.log(`  ${chalk.dim(f.owasp_category)} ${chalk.dim('|')} ${chalk.dim(f.cwe_id)} ${chalk.dim('|')} Confidence: ${chalk.cyan(conf + '%')}`);
      console.log(`  📍 ${chalk.underline(f.file)} ${chalk.dim('lines ' + f.line_range)}`);
      console.log(`  ${chalk.dim(f.explanation?.slice(0, 120) + '...' || '')}`);
    }
  }

  if (rejected.length > 0) {
    console.log('');
    warnLog(`${rejected.length} finding(s) rejected as false positives by the Critic`);
  }

  if (actorReport.clean_areas?.length > 0) {
    console.log('');
    successLog(`Clean areas: ${actorReport.clean_areas.slice(0,2).join('; ')}`);
  }

  // ── 5. Generate Markdown report ────────────────────────────────────────
  stepLog(3, 3, 'Generating Markdown report...');
  const markdownReport = generateReport(prData, actorReport, criticReport);

  // Save to file if requested
  if (options.output) {
    fs.writeFileSync(options.output, markdownReport);
    successLog(`Report saved to: ${options.output}`);
  }

  // Post to GitHub if not disabled
  if (options.comment !== false) {
    spinner = ora({ text: 'Posting report as GitHub PR comment...', prefixText: '  ' }).start();
    try {
      await postComment(githubToken, owner, repo, prNumber, markdownReport);
      spinner.succeed(chalk.green(`Comment posted to PR #${prNumber}`));
    } catch (err) {
      spinner.fail(chalk.red(`Failed to post comment: ${err.message}`));
    }
  }

  console.log('');
  console.log(chalk.cyan('  ────────────────────────────────────────────────'));
  console.log(`  ${chalk.bold.white('Done!')} PR #${prNumber} audit complete.`);
  console.log(`  View PR: ${chalk.underline.blue(prData.pr.url)}`);
  console.log('');
}
