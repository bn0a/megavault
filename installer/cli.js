#!/usr/bin/env node
'use strict';

// Installer for the skill and its four subagents. Node built-ins only.
// Copies the package's skills/<SKILL_DIR>/ (SKILL.md, scripts/, references/) into
// <claudeDir>/skills/<SKILL_DIR>/ and agents/*.md into <claudeDir>/agents/. No network access.
// The same skills/ and agents/ directories are what the Claude Code plugin loads.
//
// Exit codes: 0 ok, 1 refused or usage error, 2 unexpected error.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

// The skill directory name (skills/<SKILL_DIR>/ here and under <claudeDir>/skills/).
const SKILL_DIR = 'megavault';

const PKG_ROOT = path.resolve(__dirname, '..');
const SKILL_SRC = path.join(PKG_ROOT, 'skills', SKILL_DIR);
const PKG = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, 'package.json'), 'utf8'));
const BIN = Object.keys(PKG.bin || {})[0] || PKG.name;
// How a user runs this installer. The package is not on the npm registry, so `npx <name>` would
// run whatever registry package holds that name. Users run it from GitHub (`npx github:<owner>/<repo>`,
// derived from package.json `repository`) or from a clone (`node installer/cli.js`).
const REPO_SLUG = String((PKG.repository && PKG.repository.url) || '')
  .replace(/^git\+/, '').replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '');
const RUN = /^[\w.-]+\/[\w.-]+$/.test(REPO_SLUG) ? `npx github:${REPO_SLUG}` : 'node installer/cli.js';

// What goes into the skill directory: every top-level entry of skills/<SKILL_DIR>/ in the
// package (SKILL.md, scripts/, references/). Only the files inside them are owned by this
// package: install --force and uninstall touch those files and nothing else.
function skillEntries() {
  return fs.readdirSync(SKILL_SRC).filter((n) => !skipped(n)).sort();
}
const MIN_PYTHON = [3, 10];

const HELP = `${BIN} ${PKG.version}

Install the skill and its four subagents into a Claude Code config directory.

Usage:
  ${RUN} [install] [options]   copy the skill and agents (default command)
  ${RUN} uninstall [options]   remove the files install copies; files you added are kept
  ${RUN} --help | --version
From a clone of the repository, 'node installer/cli.js' takes the same arguments.

Options:
  --claude-dir <path>   config directory to use (default: $CLAUDE_CONFIG_DIR, else ~/.claude)
  --force               install: replace an existing install
  --dry-run             print the plan, change nothing

Installs:
  <claude-dir>/skills/${SKILL_DIR}/  (SKILL.md, scripts/, references/)
  <claude-dir>/agents/research-*.md (four files)

Exit codes: 0 ok, 1 refused or usage error, 2 error.
`;

class UsageError extends Error {}

function parseArgs(argv) {
  const opts = { command: null, claudeDir: null, force: false, dryRun: false, help: false, version: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-h' || a === '--help') opts.help = true;
    else if (a === '-v' || a === '--version') opts.version = true;
    else if (a === '--force') opts.force = true;
    else if (a === '--dry-run') opts.dryRun = true;
    else if (a === '--claude-dir') {
      const v = argv[++i];
      if (!v || v.startsWith('--')) throw new UsageError('--claude-dir needs a path');
      opts.claudeDir = v;
    } else if (a.startsWith('--claude-dir=')) {
      const v = a.slice('--claude-dir='.length);
      if (!v) throw new UsageError('--claude-dir needs a path');
      opts.claudeDir = v;
    } else if (a.startsWith('-')) {
      throw new UsageError(`unknown option: ${a}`);
    } else if (opts.command === null && (a === 'install' || a === 'uninstall')) {
      opts.command = a;
    } else {
      throw new UsageError(`unknown command: ${a}`);
    }
  }
  if (opts.command === null) opts.command = 'install';
  if (opts.force && opts.command !== 'install') throw new UsageError('--force only applies to install');
  return opts;
}

function expandHome(p) {
  if (p === '~') return os.homedir();
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function resolveClaudeDir(flag) {
  if (flag) return path.resolve(expandHome(flag));
  const env = process.env.CLAUDE_CONFIG_DIR;
  if (env && env.trim()) return path.resolve(expandHome(env.trim()));
  return path.join(os.homedir(), '.claude');
}

function packageAgents() {
  const dir = path.join(PKG_ROOT, 'agents');
  return fs.readdirSync(dir).filter((f) => f.endsWith('.md')).sort();
}

function skillName() {
  // The slash command comes from the `name:` field in SKILL.md, not the directory name.
  const text = fs.readFileSync(path.join(SKILL_SRC, 'SKILL.md'), 'utf8');
  const m = /^---\r?\n[\s\S]*?^name:\s*(\S+)\s*$/m.exec(text);
  return m ? m[1] : SKILL_DIR;
}

function plan(claudeDir) {
  const skillDir = path.join(claudeDir, 'skills', SKILL_DIR);
  const agentsDir = path.join(claudeDir, 'agents');
  const agents = packageAgents();
  const targets = [
    ...skillEntries().map((e) => ({ src: path.join(SKILL_SRC, e), dst: path.join(skillDir, e) })),
    ...agents.map((a) => ({ src: path.join(PKG_ROOT, 'agents', a), dst: path.join(agentsDir, a) })),
  ];
  return { skillDir, agentsDir, agents, targets };
}

function exists(p) {
  try {
    fs.lstatSync(p);
    return true;
  } catch {
    return false;
  }
}

function skipped(name) {
  return name === '__pycache__' || name.endsWith('.pyc');
}

// Recursive copy that skips Python bytecode. Returns the list of files written (relative to base).
function copyTree(src, dst, base, out) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dst, { recursive: true });
    for (const name of fs.readdirSync(src).sort()) {
      if (skipped(name)) continue;
      copyTree(path.join(src, name), path.join(dst, name), base, out);
    }
  } else if (st.isFile()) {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
    out.push(path.relative(base, dst));
  }
  return out;
}

// Files a copy would write, without writing anything.
function listTree(src, dstBase, base, out) {
  const st = fs.statSync(src);
  if (st.isDirectory()) {
    for (const name of fs.readdirSync(src).sort()) {
      if (skipped(name)) continue;
      listTree(path.join(src, name), path.join(dstBase, name), base, out);
    }
  } else if (st.isFile()) {
    out.push(path.relative(base, dstBase));
  }
  return out;
}

function show(list) {
  for (const f of list) console.log('  ' + f.split(path.sep).join('/'));
}

// Absolute paths of every file this package installs (skill files and agent files).
function ownedFiles(p) {
  const rel = [];
  for (const e of skillEntries()) listTree(path.join(SKILL_SRC, e), path.join(p.skillDir, e), p.skillDir, rel);
  return [...rel.map((r) => path.join(p.skillDir, r)), ...p.agents.map((a) => path.join(p.agentsDir, a))];
}

// Every file under dir (symlinks count as files and are never followed).
function walkFiles(dir, out = []) {
  if (!exists(dir)) return out;
  for (const name of fs.readdirSync(dir).sort()) {
    const full = path.join(dir, name);
    if (fs.lstatSync(full).isDirectory()) walkFiles(full, out);
    else out.push(full);
  }
  return out;
}

// Bytecode Python writes next to the package's scripts; derived from owned files, so owned.
function isBytecode(file) {
  return file.split(path.sep).includes('__pycache__') || file.endsWith('.pyc');
}

// Files under the skill directory that this package does not ship (yours, or left from an
// older version). They are never deleted, only listed.
function extraFiles(p) {
  const owned = new Set(ownedFiles(p));
  return walkFiles(p.skillDir).filter((f) => !owned.has(f) && !isBytecode(f));
}

// Remove __pycache__ folders and then empty folders, bottom-up, inside dir (dir included).
function prune(dir) {
  if (!exists(dir) || !fs.lstatSync(dir).isDirectory()) return;
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    if (!fs.lstatSync(full).isDirectory()) continue;
    if (name === '__pycache__') fs.rmSync(full, { recursive: true, force: true });
    else prune(full);
  }
  if (!fs.readdirSync(dir).length) fs.rmdirSync(dir);
}

function install(opts) {
  const claudeDir = resolveClaudeDir(opts.claudeDir);
  const p = plan(claudeDir);

  const required = ['SKILL.md', 'scripts', 'references'].map((e) => ({ src: path.join(SKILL_SRC, e) }));
  for (const t of [...required, ...p.targets]) {
    if (!exists(t.src)) {
      console.error(`package is incomplete: missing ${path.relative(PKG_ROOT, t.src)}`);
      return 2;
    }
  }

  const existing = ownedFiles(p).filter(exists);
  if (existing.length && !opts.force) {
    console.error(`${opts.dryRun ? '[dry run] would refuse' : 'refusing'} to overwrite an existing install:`);
    for (const e of existing) console.error('  ' + e);
    console.error('re-run with --force to replace these files');
    return 1;
  }

  if (opts.dryRun) {
    console.log(`[dry run] claude dir: ${claudeDir}`);
    if (existing.length) {
      console.log('[dry run] would replace (--force):');
      for (const e of existing) console.log('  ' + e);
    }
    const skillFiles = [];
    for (const e of skillEntries()) listTree(path.join(SKILL_SRC, e), path.join(p.skillDir, e), p.skillDir, skillFiles);
    console.log(`[dry run] would install skill  -> ${p.skillDir}`);
    show(skillFiles);
    console.log(`[dry run] would install agents -> ${p.agentsDir}`);
    show(p.agents);
    console.log('[dry run] nothing written');
    return 0;
  }

  fs.mkdirSync(p.skillDir, { recursive: true });
  fs.mkdirSync(p.agentsDir, { recursive: true });
  const skillFiles = [];
  // File by file: --force overwrites the package's own files and leaves anything else alone.
  for (const e of skillEntries()) copyTree(path.join(SKILL_SRC, e), path.join(p.skillDir, e), p.skillDir, skillFiles);
  for (const a of p.agents) fs.copyFileSync(path.join(PKG_ROOT, 'agents', a), path.join(p.agentsDir, a));

  console.log(`installed skill  -> ${p.skillDir}`);
  show(skillFiles);
  console.log(`installed agents -> ${p.agentsDir}`);
  show(p.agents);
  const extras = extraFiles(p);
  if (extras.length) {
    console.log(`kept ${extras.length} file(s) under ${p.skillDir} that this package does not ship:`);
    show(extras);
  }

  checkPython();

  const cmd = '/' + skillName();
  console.log('');
  console.log('next steps:');
  console.log('  1. restart Claude Code so it picks up the skill and the agents');
  console.log('  2. set RESEARCH_VAULT_ROOT to the vault you publish into (default: ./vault)');
  console.log(`  3. in Claude Code: ${cmd} plan <question>`);
  return 0;
}

function pythonVersion(cmd) {
  let r;
  try {
    // cwd: the package directory, which ships no program. On Windows a bare name can
    // be looked up in the working directory first; never run a planted python.exe.
    r = spawnSync(cmd, ['--version'], { encoding: 'utf8', timeout: 10000, windowsHide: true, cwd: PKG_ROOT });
  } catch {
    return null;
  }
  if (r.error || r.status !== 0) return null;
  const m = /Python\s+(\d+)\.(\d+)(?:\.(\d+))?/.exec(`${r.stdout || ''} ${r.stderr || ''}`);
  return m ? { cmd, major: +m[1], minor: +m[2], text: m[0] } : null;
}

function checkPython() {
  const found = ['python3', 'python'].map(pythonVersion).filter(Boolean);
  const ok = found.find((v) => v.major > MIN_PYTHON[0] || (v.major === MIN_PYTHON[0] && v.minor >= MIN_PYTHON[1]));
  const need = `${MIN_PYTHON[0]}.${MIN_PYTHON[1]}`;
  if (ok) {
    console.log(`python: ${ok.text} (${ok.cmd})`);
  } else if (found.length) {
    console.warn(`warning: the skill needs Python ${need} or newer; found ${found.map((v) => `${v.text} (${v.cmd})`).join(', ')}`);
  } else {
    console.warn(`warning: no python or python3 on PATH; the skill needs Python ${need} or newer`);
  }
}

function uninstall(opts) {
  const claudeDir = resolveClaudeDir(opts.claudeDir);
  const p = plan(claudeDir);
  const present = ownedFiles(p).filter(exists);
  const tag = opts.dryRun ? '[dry run] would remove' : 'removed';

  if (!present.length) {
    console.log(`nothing to remove under ${claudeDir}`);
    return 0;
  }
  // Only the files this package ships (plus Python bytecode and folders left empty).
  const extras = extraFiles(p);
  for (const file of present) {
    if (!opts.dryRun) fs.rmSync(file, { force: true });
    console.log(`${tag} ${file}`);
  }
  if (!opts.dryRun) prune(p.skillDir);
  if (extras.length) {
    console.log(`kept ${extras.length} file(s) under ${p.skillDir} that this package did not install:`);
    show(extras);
  } else {
    console.log(`${tag} ${p.skillDir}`);
  }
  if (opts.dryRun) console.log('[dry run] nothing removed');
  return 0;
}

function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (e) {
    if (e instanceof UsageError) {
      console.error(`${BIN}: ${e.message}`);
      console.error(`run '${RUN} --help' (or 'node installer/cli.js --help') for usage`);
      return 1;
    }
    throw e;
  }
  if (opts.help) {
    process.stdout.write(HELP);
    return 0;
  }
  if (opts.version) {
    console.log(PKG.version);
    return 0;
  }
  return opts.command === 'uninstall' ? uninstall(opts) : install(opts);
}

try {
  process.exitCode = main(process.argv.slice(2));
} catch (e) {
  console.error(`${BIN}: error: ${e && e.message ? e.message : e}`);
  process.exitCode = 2;
}
