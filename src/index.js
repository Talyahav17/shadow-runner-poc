#!/usr/bin/env node
import { program } from 'commander';
import chalk from 'chalk';
import { auditPR } from './commands/audit.js';
import { configCommand } from './commands/config.js';
import { banner } from './ui/banner.js';

banner();

program
  .name('secure-auditor')
  .description('🛡️  AI-powered security auditor for GitHub Pull Requests')
  .version('1.0.0');

program
  .command('audit')
  .description('Audit a GitHub Pull Request for security vulnerabilities')
  .requiredOption('-r, --repo <owner/repo>', 'GitHub repository (e.g. acme/backend-api)')
  .requiredOption('-p, --pr <number>', 'Pull Request number', parseInt)
  .option('-m, --model <provider>', 'AI provider: claude | ollama', 'claude')
  .option('-o, --ollama-model <name>', 'Ollama model name (default: codellama)', 'codellama')
  .option('--no-comment', 'Skip posting comment to GitHub PR')
  .option('--output <file>', 'Save report to file (e.g. report.md)')
  .action(auditPR);

program
  .command('config')
  .description('Set up API keys and preferences')
  .action(configCommand);

program.parse();
