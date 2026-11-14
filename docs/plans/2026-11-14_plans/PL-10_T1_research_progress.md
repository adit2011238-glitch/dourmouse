# PL-10 T1 (research) progress and design, 2026-11-13

Worktree: ~/dourmouse-recon/.claude/worktrees/elastic-babbage-282c69, branch claude/elastic-babbage-282c69, based on cd777c1 (recon-2026-09-11). NOTHING COMMITTED YET: the full suite has not been run. Two new files are uncommitted in that worktree.

Owner decisions asked 2026-11-13, no answer yet: citation style (default MLA) and free OpenAlex and Semantic Scholar keys (default keyless). Keys, if added to .env: OPENALEX_API_KEY, SEMANTIC_SCHOLAR_API_KEY.

## Done (step 1 of 8, tests pass, not committed)

- dourmouse/research_pipeline/papers.py: PaperClient over OpenAlex (search, reviews by citations, DOI lookup), Semantic Scholar (search, references, citations) and arXiv (Atom via defusedxml). One throttle per service (1 s, arXiv 3 s per arXiv's API terms), 429 waited out via Retry-After (cap 30 s, 2 retries) then reported per service, disk cache 7 days keyed without the API key, merge by DOI plus arXiv id plus title and first author, rank_papers (relevance 0.55, citations per year 0.2, recency 0.1, venue 0.15).
- dourmouse/tests/test_research_papers.py: 38 tests, all pass; ruff clean. One real bug found by them: S2 needs the DOI slash unescaped in the path.

## Facts found live (2026-11-13)

- OpenAlex keyless: shared budget of $0.10 a day per network address, a search costs $0.001, so about 100 searches a day. A free key gives $1 a day. OpenAlex's own notice suggests an agent register a key with the user's e-mail: NOT done, that is the owner's decision.
- Semantic Scholar keyless: answered 429 at once (shared pool). Expect it to fail often without a key; the code reports it and carries on.
- arxiv.org/robots.txt allows /pdf with Crawl-delay 15 (our politeness caps at 10 s); export.arxiv.org robots disallows crawling, but it is the documented API host.
- OpenAlex sometimes types a journal article as "preprint" (CSHL DOI prefix); venue type from primary_location.source.type is used instead.

## Next steps (in order)

2. reading.py: pick an open-access PDF (arXiv first), fetch through acquire.fetch_document (already uses extract.extract_pdf_text), split into sections, drop References; abstract only when no PDF, and the card says so. Verbatim check for PDFs: normalize both sides the same way (NFKC, line-break hyphens, whitespace).
3. cards.py: one card per paper (question, method, data, findings with verified quotes, limitations, bearing) plus a deterministic grade (venue, citations per year, recency, read from PDF or abstract, agreement) with plain reasons.
4. brief.py: Wikipedia plain-text extract plus two most cited reviews (papers.top_reviews), quoted items verbatim-checked; owner can correct before stage 2.
5. questions.py: question, goals, 3 to 6 sub-questions from card gaps; owner edits and approves.
6. cycle.py: per sub-question retrieve, follow S2 citations when thin, answer in 2 to 3 sentences with claim-level citations checked deterministically, contradictions via existing detect_contradictions, stop when a cycle adds no claim or the budget is spent (say which), MLA or APA reference list, Markdown export. Write the final result as a ResearchRecord so the existing RESEARCH screen and graph show it.
7. describe_samples for numeric claims (numbers verified verbatim in quotes) and a registered tool so the "stats" stage shows built; "validate" stage shows the grading tool.
8. Benchmark: 10 fixed questions with reference DOIs verified live through OpenAlex; citation precision (target 95 percent) and source recall (target 70 percent).

Background job: add a native job-handler hook to goal_runtime (assigned_agent "job:research"), tasks created as the job advances (brief, papers, one card task per paper, questions parked WAITING_FOR_APPROVAL, cycles, present). Library: T4 not landed; write behind a thin port add(source, text, meta), search(query, filters), cite(passage_id) with an interim SQLite FTS5 store (FTS5 available, sqlite 3.53.4).

At merge time: audit numbers collide across six threads; renumber to the next free number in recon-2026-09-11 after rebasing.
