import chalk from 'chalk';

export function banner() {
  console.log('');
  console.log(chalk.bold.cyan('  ╔═══════════════════════════════════════════════╗'));
  console.log(chalk.bold.cyan('  ║') + chalk.bold.white('   🛡️  SECURE CODE AUDITOR  v1.0               ') + chalk.bold.cyan('║'));
  console.log(chalk.bold.cyan('  ║') + chalk.dim('   AI-Powered Security Review for GitHub PRs   ') + chalk.bold.cyan('║'));
  console.log(chalk.bold.cyan('  ║') + chalk.dim('   Actor → Critic Multi-Agent Pipeline         ') + chalk.bold.cyan('║'));
  console.log(chalk.bold.cyan('  ╚═══════════════════════════════════════════════╝'));
  console.log('');
}

export function sectionHeader(title) {
  const line = '─'.repeat(50);
  console.log('');
  console.log(chalk.cyan(`  ┌${line}┐`));
  console.log(chalk.cyan('  │') + chalk.bold.white(`  ${title.padEnd(49)}`) + chalk.cyan('│'));
  console.log(chalk.cyan(`  └${line}┘`));
}

export function stepLog(step, total, message) {
  const badge = chalk.bgCyan.black(` ${step}/${total} `);
  console.log(`  ${badge} ${chalk.white(message)}`);
}

export function successLog(msg) {
  console.log(`  ${chalk.green('✔')} ${chalk.green(msg)}`);
}

export function warnLog(msg) {
  console.log(`  ${chalk.yellow('⚠')} ${chalk.yellow(msg)}`);
}

export function errorLog(msg) {
  console.log(`  ${chalk.red('✖')} ${chalk.red(msg)}`);
}

export function infoLog(msg) {
  console.log(`  ${chalk.blue('ℹ')} ${chalk.dim(msg)}`);
}

export function severityBadge(severity) {
  const map = {
    CRITICAL: chalk.bgRed.white(' CRITICAL '),
    HIGH:     chalk.bgYellow.black(' HIGH     '),
    MEDIUM:   chalk.bgBlue.white(' MEDIUM   '),
    LOW:      chalk.bgGreen.black(' LOW      '),
    INFO:     chalk.bgGray.white(' INFO     '),
  };
  return map[severity] || chalk.bgGray.white(` ${severity} `);
}

export function verdictBadge(verdict) {
  const map = {
    CONFIRMED:  chalk.green('✅ CONFIRMED'),
    ADJUSTED:   chalk.blue('🔧 ADJUSTED'),
    DOWNGRADED: chalk.yellow('⬇ DOWNGRADED'),
    MITIGATED:  chalk.cyan('🛡 MITIGATED'),
    REJECTED:   chalk.red('❌ REJECTED'),
  };
  return map[verdict] || verdict;
}
