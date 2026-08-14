# DOURMOUSE END USER LICENSE AGREEMENT (EULA)

**Version 1.0 — August 2026.** This is a plain-English summary of the key
terms; the license that governs is `LICENSE` in the same folder. Where the
two disagree, the `LICENSE` file wins.

## What you're getting

Dourmouse is a **local-first AI assistant** that runs on your own machine. It
reads and writes files on that machine, runs scheduled tasks, searches the
web, and drafts messages — always with a visible action log, and with approval
gates before anything consequential (sending mail, deleting files, spending
money).

## What Dourmouse does with your data

- **Mostly nothing leaves your machine.** Conversations, files, your memory
  store, and workspace logs stay local unless *you* configure an external
  model provider (see `PRIVACY.md` §2 for the exact list of outbound calls).
- **The model provider sees what you send it.** If you use a cloud model
  backend (NVIDIA NIM, OpenAI-compatible APIs), the prompts you type are sent
  to that provider. That is inherent to how the service works — treat the
  chat surface accordingly.
- **Optional multi-device mesh.** If you enable the Tailscale relay, a
  separate chat feed between your own devices is exchanged over an
  end-to-end encrypted private network. It is off by default.
- **Learning is local and toggleable.** Dourmouse stores what it learns about
  your work in a local memory store. Set `DOURMOUSE_LEARN=0` to disable the
  learning loop entirely.

## What you agree to

1. You will not use Dourmouse to break the law (including financial-securities
   regulation, spam laws, or unauthorized access to systems).
2. You are responsible for what the agent does on your behalf. It executes
   with your approvals — check the action log, use the kill switch.
3. You will not redistribute, resell, or reverse-engineer the Software
   (see `LICENSE` §3).
4. You understand the Software is provided "as is" — see `LICENSE` §4–5 for
   the full no-warranty and liability-limitation terms.

## If something goes wrong

- **Kill switch:** the red stop control in the dashboard halts all in-flight
  automation immediately.
- **Audit trail:** every action is logged with a reason; nothing runs silently.
- **Support:** file issues through the project's public channel named in the
  README. We fix real bugs; we do not guarantee market outcomes or that any
  automated strategy is profitable.

## Termination

This EULA terminates automatically if you breach its terms. On termination,
uninstall the Software and delete your copies.

*Questions: contact the maintainer through the channel named in the README.*
