# electron-claude-bridge

Electron wiring for the locally-authenticated Claude Code CLI: spawn it with
the user's real environment, stream every token live, and keep the model's
reasoning separate from its answer all the way to the UI.

```
src/claude-session.ts   process handler — resolve CLI, build env, parse stream
src/main.ts             Electron main — owns sessions, typed IPC, lifecycle
src/preload.ts          contextBridge — four functions, no raw ipcRenderer
src/renderer.ts         UI listener — batched paint, thinking + output panes
renderer/index.html     markup + CSP
```

```bash
npm install
npm start        # build + launch
npm run typecheck
```

## How the login resolves

The CLI authenticates itself from local storage. Nothing in this code reads,
injects, forwards or logs a token.

| Platform | Where |
|---|---|
| macOS | login keychain, generic password, service `Claude Code-credentials` |
| Linux / Windows | `~/.claude/.credentials.json` |

Check it yourself:

```bash
security find-generic-password -s "Claude Code-credentials"   # macOS
```

The only requirement on the host app is to hand the child the **real user
environment**. `buildCliEnv()` copies `process.env` whole and adds to `PATH`;
it never replaces or prunes it. `HOME` is what makes `~/.claude` resolve, and
on macOS the keychain lookup needs the logged-in user's session intact.

## The PATH problem — real, not theoretical

A macOS app launched from the Dock or via `open` does **not** inherit the
shell `PATH`. A sibling app's server process was measured running with
literally:

```
PATH=/usr/bin:/bin:/usr/sbin:/sbin
```

which contains neither `~/.local/bin` (where `claude` installs) nor any Node
install. `which('claude')` returned null and every request failed with "CLI
not found" — while the CLI was installed, logged in, and fine from a terminal.
Resolving the binary by absolute path alone still fails, because `claude` is a
Node program that needs `node` on `PATH` to run.

So `resolveClaudeCli()` searches the real install locations after `PATH`, and
`buildCliEnv()` prepends those same locations plus the binary's own directory.

## Why these exact CLI flags

```
claude -p --output-format stream-json --include-partial-messages --verbose <prompt>
```

- `--output-format stream-json` is what **separates reasoning from answer**.
  `--verbose` alone yields prose with the two already merged and unparseable
  after the fact. `--verbose` is required *alongside* stream-json, not instead.
- `--include-partial-messages` makes deltas arrive token by token rather than
  one block per turn.
- `stdio[0]` is `'ignore'`, not `'pipe'`: `claude -p` blocks for seconds
  waiting on stdin if it is left open.

## Buffering

stream-json is newline-delimited JSON. Output is read through
`readline.createInterface`, which yields one complete line per event no matter
how the OS chunked the pipe. Never concatenate raw `data` chunks and hope an
object landed whole — it will not, under load.

Events consumed:

| Event | Meaning |
|---|---|
| `content_block_delta` / `thinking_delta` | reasoning token |
| `content_block_delta` / `text_delta` | answer token |
| `content_block_start` / `tool_use` | tool invoked |
| `result` | `total_cost_usd`, `usage.*`, authoritative final text |

## Security

- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`.
- The preload exposes four functions. `ipcRenderer` itself is **not** exposed —
  that would let any script in the renderer reach every channel in the app.
- Channel names are constants in the preload, not parameters, so the renderer
  cannot address a channel of its own choosing.
- Events are sent to the exact `WebContents` that started the run; a window can
  only cancel a run it started.
- The renderer writes with `textContent`, never `innerHTML`. Model output is
  untrusted; rendering it as markup in a privileged window is how a prompt
  injection becomes code execution.
- CSP forbids remote script and `eval`. `setWindowOpenHandler` denies new
  Electron windows and sends real URLs to the system browser.
- Child processes are killed on window close and on app quit.

## Rendering

Deltas are buffered and flushed once per animation frame. A fast turn emits
hundreds of tokens per second, and touching the DOM per token costs a layout
and paint each time — the UI stutters and falls behind the process it is
showing. Perceived latency is unchanged (~16ms); cost drops by orders of
magnitude. Auto-scroll only engages when the user is already at the bottom.

The thinking pane opens while the model reasons and folds itself on the first
answer token, staying reachable via its toggle rather than vanishing.

## Verification status

Run against the real CLI with `PATH` forced to `/usr/bin:/bin:/usr/sbin:/sbin`:

- CLI resolved at `~/.local/bin/claude` despite it being absent from `PATH`
- `HOME` preserved, child `PATH` correctly widened
- real streaming, correct answers, real `usage` (cost + all four token counters)

**Not observed live:** `thinking_delta` blocks. The model did not emit extended
thinking for the test prompts on this machine, so the reasoning pane was never
exercised end to end against the real binary. The parser was instead verified
against a stub emitting the exact documented wire shapes, which confirmed clean
separation — reasoning text never leaked into the answer stream. Treat the live
thinking path as untested until you see it on a model that emits it.
