/**
 * claude-session.ts — the process handler.
 *
 * Spawns the Claude Code CLI, keeps the user's local login working, and
 * turns its stream-json output into typed events. Everything here is
 * main-process only; nothing in this file is reachable from the renderer.
 *
 * ---------------------------------------------------------------------------
 * How the login actually resolves, and what this file must NOT do
 * ---------------------------------------------------------------------------
 * The CLI authenticates itself from local storage. It is not passed a token
 * and does not want one:
 *
 *   macOS          the login keychain, generic password, service name
 *                  "Claude Code-credentials". Verify with:
 *                    security find-generic-password -s "Claude Code-credentials"
 *   Linux/Windows  ~/.claude/.credentials.json
 *
 * So the single requirement on us is: give the child the real user
 * environment. `HOME` is what makes `~/.claude` resolve; on macOS the
 * keychain lookup runs as the logged-in user and needs that session intact.
 *
 * Do NOT hand-build a minimal env, do NOT strip it "for hygiene", and do NOT
 * read the credential yourself to forward it. Any of those breaks the native
 * lookup, and the last one puts a long-lived token somewhere it does not
 * belong. We copy `process.env` wholesale and only ADD to PATH.
 *
 * ---------------------------------------------------------------------------
 * Why PATH needs widening — a real, reproduced failure, not defensiveness
 * ---------------------------------------------------------------------------
 * A macOS app launched from the Dock or via `open` does not inherit the
 * user's shell PATH. A sibling app's server process was measured running
 * with literally:
 *
 *     PATH=/usr/bin:/bin:/usr/sbin:/sbin
 *
 * That contains neither `~/.local/bin` (where `claude` installs) nor any
 * Node install. The result was `which('claude') === null` and every request
 * failing with "CLI not found", while the CLI was installed, logged in, and
 * working fine from a terminal. Worse, resolving the binary by absolute path
 * alone still fails, because `claude` is a Node program that needs `node` on
 * PATH to run at all.
 *
 * Hence: resolve the binary across the real install locations, and prepend
 * those same locations (plus the binary's own directory) to the child's PATH.
 */

import { spawn, type ChildProcessByStdio } from 'node:child_process';
import type { Readable } from 'node:stream';
import { createInterface } from 'node:readline';
import { accessSync, constants, existsSync, readdirSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, join, resolve as resolvePath } from 'node:path';

/** Directories a `claude` install really lands in, in priority order. */
const CLI_SEARCH_DIRS = [
  join(homedir(), '.local', 'bin'),
  join(homedir(), '.claude', 'local'),
  join(homedir(), 'bin'),
  '/opt/homebrew/bin',
  '/usr/local/bin',
  join(homedir(), '.npm-global', 'bin'),
  join(homedir(), '.yarn', 'bin'),
  '/opt/local/bin',
];

function isExecutable(p: string): boolean {
  try {
    accessSync(p, constants.X_OK);
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

/** The newest nvm-managed Node bin dir, if nvm is in use. */
function nvmBinDir(): string | null {
  const base = join(homedir(), '.nvm', 'versions', 'node');
  if (!existsSync(base)) return null;
  try {
    const versions = readdirSync(base).sort().reverse();
    for (const v of versions) {
      const bin = join(base, v, 'bin');
      if (existsSync(bin)) return bin;
    }
  } catch {
    /* unreadable nvm dir is not fatal */
  }
  return null;
}

/**
 * Locate the CLI. `CLAUDE_CLI_PATH` wins, then PATH, then the known install
 * locations. Returns null rather than throwing, so the caller can surface a
 * real "not configured" message instead of a stack trace.
 */
export function resolveClaudeCli(): string | null {
  const override = process.env.CLAUDE_CLI_PATH;
  if (override && isExecutable(override)) return override;

  for (const dir of (process.env.PATH || '').split(delimiter)) {
    if (!dir) continue;
    const candidate = join(dir, 'claude');
    if (isExecutable(candidate)) return candidate;
  }

  const extra = [...CLI_SEARCH_DIRS];
  const nvm = nvmBinDir();
  if (nvm) extra.push(nvm);

  for (const dir of extra) {
    const candidate = join(dir, 'claude');
    if (isExecutable(candidate)) return candidate;
  }
  return null;
}

/**
 * The child environment: the parent's, entire, with PATH widened.
 * See the header for why this must not be narrowed.
 */
export function buildCliEnv(cliPath: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };

  const lead = [resolvePath(cliPath, '..'), ...CLI_SEARCH_DIRS];
  const nvm = nvmBinDir();
  if (nvm) lead.push(nvm);
  lead.push('/usr/bin', '/bin');

  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const part of [...lead, ...(env.PATH || '').split(delimiter)]) {
    if (part && !seen.has(part)) {
      seen.add(part);
      ordered.push(part);
    }
  }
  env.PATH = ordered.join(delimiter);
  return env;
}

/* -------------------------------------------------------------------------- */
/* Events                                                                     */
/* -------------------------------------------------------------------------- */

export type ClaudeEvent =
  | { type: 'start'; sessionId: string; model?: string }
  | { type: 'thinking'; sessionId: string; text: string }
  | { type: 'text'; sessionId: string; text: string }
  | { type: 'tool'; sessionId: string; name: string }
  | {
      type: 'usage';
      sessionId: string;
      costUsd: number | null;
      inputTokens: number;
      outputTokens: number;
      cacheCreationInputTokens: number;
      cacheReadInputTokens: number;
    }
  | { type: 'done'; sessionId: string; finalText: string }
  | { type: 'error'; sessionId: string; message: string };

export interface RunOptions {
  prompt: string;
  cwd?: string;
  /** Hard ceiling in ms. The CLI is killed if it overruns. */
  timeoutMs?: number;
}

/* -------------------------------------------------------------------------- */
/* Session                                                                    */
/* -------------------------------------------------------------------------- */

export class ClaudeSession {
  // stdin is 'ignore' (see run()), so this is deliberately the no-stdin
  // child type rather than ChildProcessWithoutNullStreams.
  private child: ChildProcessByStdio<null, Readable, Readable> | null = null;
  private killTimer: NodeJS.Timeout | null = null;

  constructor(
    public readonly sessionId: string,
    private readonly emit: (event: ClaudeEvent) => void,
  ) {}

  get running(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }

  run(options: RunOptions): void {
    if (this.running) {
      this.emit({
        type: 'error',
        sessionId: this.sessionId,
        message: 'This session is already running a prompt.',
      });
      return;
    }

    const cli = resolveClaudeCli();
    if (!cli) {
      this.emit({
        type: 'error',
        sessionId: this.sessionId,
        message:
          'The Claude Code CLI was not found. Install it with ' +
          '`npm i -g @anthropic-ai/claude-code`, or set CLAUDE_CLI_PATH to its ' +
          'absolute path. Nothing was run.',
      });
      return;
    }

    // --output-format stream-json is what actually separates reasoning from
    // answer text; --verbose alone gives prose with the two already merged
    // and is unparseable after the fact. --include-partial-messages is what
    // makes deltas arrive token by token rather than one block per turn.
    // --verbose is required *alongside* stream-json, not instead of it.
    const args = [
      '-p',
      '--output-format',
      'stream-json',
      '--include-partial-messages',
      '--verbose',
      options.prompt,
    ];

    let child: ChildProcessByStdio<null, Readable, Readable>;
    try {
      child = spawn(cli, args, {
        cwd: options.cwd ?? homedir(),
        env: buildCliEnv(cli),
        // `claude -p` blocks for seconds waiting on stdin if it is left open.
        // 'ignore' (not 'pipe') is what makes the first token arrive promptly.
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      this.emit({
        type: 'error',
        sessionId: this.sessionId,
        message: `Could not start the Claude CLI: ${(err as Error).message}`,
      });
      return;
    }
    this.child = child;

    if (options.timeoutMs && options.timeoutMs > 0) {
      this.killTimer = setTimeout(() => this.cancel('timed out'), options.timeoutMs);
    }

    // stream-json is newline-delimited. readline gives us one complete line
    // per 'line' event regardless of how the OS chunked the pipe, which is
    // the whole buffering problem solved -- never concatenate raw chunks and
    // hope a JSON object landed whole.
    const rl = createInterface({ input: child.stdout, crlfDelay: Infinity });

    let finalText = '';
    const textParts: string[] = [];
    const toolNameByIndex = new Map<number, string>();

    rl.on('line', (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;

      let ev: Record<string, unknown>;
      try {
        ev = JSON.parse(trimmed);
      } catch {
        return; // a non-JSON line is noise, not a reason to fail the turn
      }

      switch (ev.type) {
        case 'system': {
          if (ev.subtype === 'init') {
            this.emit({
              type: 'start',
              sessionId: this.sessionId,
              model: typeof ev.model === 'string' ? ev.model : undefined,
            });
          }
          break;
        }

        case 'stream_event': {
          const inner = (ev.event ?? {}) as Record<string, unknown>;

          if (inner.type === 'content_block_start') {
            const block = (inner.content_block ?? {}) as Record<string, unknown>;
            if (block.type === 'tool_use' && typeof block.name === 'string') {
              const idx = typeof inner.index === 'number' ? inner.index : 0;
              toolNameByIndex.set(idx, block.name);
              this.emit({ type: 'tool', sessionId: this.sessionId, name: block.name });
            }
            break;
          }

          if (inner.type === 'content_block_delta') {
            const delta = (inner.delta ?? {}) as Record<string, unknown>;
            // The two streams the UI renders side by side.
            if (delta.type === 'thinking_delta' && typeof delta.thinking === 'string') {
              this.emit({
                type: 'thinking',
                sessionId: this.sessionId,
                text: delta.thinking,
              });
            } else if (delta.type === 'text_delta' && typeof delta.text === 'string') {
              textParts.push(delta.text);
              this.emit({ type: 'text', sessionId: this.sessionId, text: delta.text });
            }
          }
          break;
        }

        case 'result': {
          if (typeof ev.result === 'string' && ev.result) finalText = ev.result;
          const usage = (ev.usage ?? {}) as Record<string, unknown>;
          const num = (v: unknown) => (typeof v === 'number' ? v : 0);
          this.emit({
            type: 'usage',
            sessionId: this.sessionId,
            costUsd: typeof ev.total_cost_usd === 'number' ? ev.total_cost_usd : null,
            inputTokens: num(usage.input_tokens),
            outputTokens: num(usage.output_tokens),
            cacheCreationInputTokens: num(usage.cache_creation_input_tokens),
            cacheReadInputTokens: num(usage.cache_read_input_tokens),
          });
          break;
        }
      }
    });

    // stderr is diagnostic, not the answer. Collected and only surfaced if
    // the process actually fails, so a warning never looks like a reply.
    let stderr = '';
    child.stderr.on('data', (chunk: Buffer) => {
      stderr += chunk.toString();
      if (stderr.length > 64_000) stderr = stderr.slice(-64_000);
    });

    child.on('error', (err) => {
      this.cleanup();
      this.emit({
        type: 'error',
        sessionId: this.sessionId,
        message: `Claude CLI failed to run: ${err.message}`,
      });
    });

    child.on('close', (code) => {
      rl.close();
      this.cleanup();
      if (code === 0) {
        this.emit({
          type: 'done',
          sessionId: this.sessionId,
          finalText: finalText || textParts.join(''),
        });
      } else {
        this.emit({
          type: 'error',
          sessionId: this.sessionId,
          message:
            `Claude CLI exited with code ${code}.` +
            (stderr.trim() ? `\n${stderr.trim().slice(0, 2000)}` : ''),
        });
      }
    });
  }

  cancel(reason = 'cancelled'): void {
    if (!this.child) return;
    const child = this.child;
    this.cleanup();
    try {
      child.kill('SIGTERM');
      // SIGKILL only if it ignores the polite request.
      setTimeout(() => {
        if (child.exitCode === null) child.kill('SIGKILL');
      }, 2000).unref();
    } catch {
      /* already gone */
    }
    this.emit({
      type: 'error',
      sessionId: this.sessionId,
      message: `Run ${reason}.`,
    });
  }

  private cleanup(): void {
    if (this.killTimer) {
      clearTimeout(this.killTimer);
      this.killTimer = null;
    }
    this.child = null;
  }
}
