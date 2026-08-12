# MODEL BENCHMARK — Phase 1.2 (laptop side)

Date: 2026-08-12 10:08 UTC
Harness: `scripts/model_benchmark.py` · Part B tasks: 8 sampled (seed 7) from `training_data/instruction_pairs.jsonl` (domain split: finance=8)
System prompt: production `dourmouse.dispatch.system_message(registry)` · Ollama http://127.0.0.1:11434
Part A = strict-JSON obedience (5 fixed tasks); Part B = open answers on real tasks.

> Honest limits: keyword coverage is a weak quality proxy; `dourmouse-finetuned` may be in-sample on these pairs (same corpus) — treat its numbers as an upper bound.

## Summary

| model | A valid-JSON | B refusal | B avg coverage | B avg latency (s) | B avg tokens |
|---|---|---|---|---|---|
| dourmouse-finetuned | 100% | 12% | 0.03 | 14.7 | 2278 |

## Per-task detail
### dourmouse-finetuned

- **A0** [-] ok=True lat=6.3s tok=132 — Summarize the risk of holding a leveraged FX position overnight in JSO
  > {"plan": ["Monitor market volatility", "Prepare for potential margin calls"], "risks": ["Market move
- **A1** [-] ok=True lat=2.7s tok=134 — List the three steps to run a backtest on usdjpy_drift_k1 in JSON.
  > {"plan": ["Load historical USDJPY exchange rate data", "Apply drift model with k=1 parameter", "Run 
- **A2** [-] ok=True lat=3.7s tok=146 — What guardrails should a paper-trading loop enforce? Answer in JSON.
  > {"plan": ["Initialize trading environment", "Set up risk management parameters", "Implement trade ex
- **A3** [-] ok=True lat=3.3s tok=139 — Propose a weekly routine for maintaining a strategy lab in JSON.
  > {"plan": ["Review current projects", "Update project documentation", "Conduct performance analysis",
- **A4** [-] ok=True lat=3.3s tok=138 — Explain what NOT CONFIGURED means for a tool result in JSON.
  > {"plan": ["Identify which configuration parameters are missing", "Determine the required configurati
- **B0** [finance] ref=True cov=0.008 lat=16.6s tok=2146 — all in one go
  > I'm sorry, could you please clarify what you mean by "all in one go"? Are you asking for a summary o
- **B1** [finance] ref=False cov=0.026 lat=25.2s tok=2375 — go throigh every one of my chats, everything, and go through it
  > To accomplish this, I'll use the `memory` tool to search your chat history semantically. This will a
- **B2** [finance] ref=False cov=0.063 lat=9.6s tok=2182 — conect you to my obbsidan vault, leave RTK, so you can refer to that f
  > Connecting you to your Obsidian vault. I'll now have read access but will not retain any write permi
- **B3** [finance] ref=False cov=0.056 lat=5.2s tok=2127 — what questions did i get wrong
  > I need more context to understand which specific questions you're referring to. Could you please pro
- **B4** [finance] ref=False cov=0.01 lat=6.7s tok=2148 — can you also give me alsit of my wekaness and strenghts in topics
  > To provide a meaningful assessment, I'll need to know which specific topics or areas you're referrin
- **B5** [finance] ref=False cov=0.016 lat=8.5s tok=2156 — nothing at all js format all the questions on the pd
  > Could you clarify what you mean by "js format all the questions on the pd"? Are you asking to conver
- **B6** [finance] ref=False cov=0.071 lat=40.0s tok=2948 — EOF
aditagrawal@Adits-MacBook-Air ~ % ~/.claude/mcp-venv/bin/python3.1
  > The Obsidian MCP server encountered an error during startup:  ``` ExceptionGroup: unhandled errors i
- **B7** [finance] ref=False cov=0.018 lat=5.9s tok=2143 — i have the exam on wednesday
  > I understand you're preparing for an exam on Wednesday. What specific help do you need? Are there an
