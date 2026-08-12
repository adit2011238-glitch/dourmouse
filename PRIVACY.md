# DOURMOUSE PRIVACY POLICY

**Version 1.0 — August 2026.** This document describes what data Dourmouse
collects, what leaves your machine, and what stays. It is written to match the
code, not the marketing. If you find a discrepancy, the code wins and we want
to know about it.

## 1. What stays on your machine

- **Conversations** — chat history and agent activity are stored locally.
- **Memory store** — what Dourmouse learns about your work (facts, preferences,
  session summaries) lives in a local file. Disable with `DOURMOUSE_LEARN=0`.
- **Workspace and audit logs** — sandbox files and the action log stay local.
- **Scheduled-task definitions** — user-defined recurring workflows are stored
  in a local JSON file under the workspace root.
- **Uploaded files** — files you give the agent via the dashboard are saved
  into the local uploads sandbox.

## 2. What leaves your machine (and when)

| Destination | What is sent | When | How to disable |
|---|---|---|---|
| Your local Ollama | Prompts (full conversation context) | If `DOURMOUSE_LLM_BACKEND=auto` finds Ollama running | None needed — loopback only |
| NVIDIA NIM / cloud model API | Prompts sent to the model provider | When a cloud backend is selected | `DOURMOUSE_LLM_BACKEND=ollama` |
| Public web (news, search, quotes) | The search query or URL you asked about | Only when you request it | Just don't ask |
| Your own devices (Tailscale mesh) | The encrypted chat-feed between your devices | Only if you enable the relay | Leave the relay off |
| GitHub (your own repos) | Commits/pushes you explicitly make | Only when you run git through the agent | Don't run git commands |

**No telemetry, no analytics, no ads, no third-party tracking.** Dourmouse has
no phone-home beacon. It makes only the outbound calls you cause it to make.

## 3. Third-party services

- **Model providers** (if you use one) receive the text you submit to them and
  are governed by *their* privacy policies. We cannot control what they do
  with it — treat the chat surface as something a provider can see.
- **The relay feed** between your own devices is your own infrastructure
  (Tailscale), end-to-end encrypted; we do not operate a cloud relay for it.

## 4. Data retention and deletion

- Delete a conversation in the dashboard and its rows are removed.
- Delete the workspace folder to remove all sandbox files and session logs.
- Delete the memory store file to erase everything Dourmouse learned about you.
- There is no cloud copy of any of the above; removing local files removes the
  data, period.

## 5. Security notes (honest limits)

- Approval gates and the kill switch protect against *unintended* actions.
  They are not a guarantee against a malicious prompt injection that tricks
  the model into requesting an action you approve without reading. Review
  what you approve.
- The web server binds to loopback by default. If you expose it to your
  Tailnet (`DOURMOUSE_HOST`), a login page protects it; keep
  `DOURMOUSE_ACCESS_TOKEN` strong.
- Secrets in `.env` are not sent anywhere. Do not share your `.env` file — it
  is gitignored and the packaging scripts refuse to include it.

## 6. Changes to this policy

If we change how data flows, this document changes with it and the version
number bumps. Material changes will be called out in the project changelog.

*Contact: the maintainer via the public channel named in the README.*
