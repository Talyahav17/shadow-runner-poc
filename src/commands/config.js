import inquirer from 'inquirer';
import fs from 'fs';
import path from 'path';
import os from 'os';
import chalk from 'chalk';
import { successLog, infoLog } from '../ui/banner.js';

export const CONFIG_PATH = path.join(os.homedir(), '.secure-auditor', 'config.json');

export function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    }
  } catch {}
  return {};
}

export function saveConfig(config) {
  const dir = path.dirname(CONFIG_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2), { mode: 0o600 });
}

export async function configCommand() {
  console.log('');
  console.log(chalk.bold.white('  ⚙️  Configuration Setup'));
  console.log(chalk.dim('  Keys are stored in ~/.secure-auditor/config.json (chmod 600)\n'));

  const existing = loadConfig();

  const answers = await inquirer.prompt([
    {
      type: 'input',
      name: 'githubToken',
      message: 'GitHub Personal Access Token (repo + pull_requests scope):',
      default: existing.githubToken || '',
      validate: v => v.length > 10 || 'Please enter a valid token',
    },
    {
      type: 'list',
      name: 'defaultModel',
      message: 'Default AI provider:',
      choices: ['claude', 'ollama'],
      default: existing.defaultModel || 'claude',
    },
    {
      type: 'input',
      name: 'anthropicKey',
      message: 'Anthropic API Key (for Claude):',
      default: existing.anthropicKey || '',
      when: ans => ans.defaultModel === 'claude',
    },
    {
      type: 'input',
      name: 'ollamaUrl',
      message: 'Ollama base URL:',
      default: existing.ollamaUrl || 'http://localhost:11434',
      when: ans => ans.defaultModel === 'ollama',
    },
    {
      type: 'input',
      name: 'ollamaModel',
      message: 'Ollama model name:',
      default: existing.ollamaModel || 'codellama',
      when: ans => ans.defaultModel === 'ollama',
    },
  ]);

  const config = { ...existing, ...answers };
  saveConfig(config);

  console.log('');
  successLog('Configuration saved!');
  infoLog(`Config file: ${CONFIG_PATH}`);
}
