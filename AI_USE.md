# AI use

## Tools

Claude Code, running Claude Opus 5.5 and later Claude Sonnet 5.5, wrote the initial harness, the evals and these documents. Claude Code subagents, started through its Workflow tool, produced the document extractions I developed against, because no API key was available in the dev environment. They use the same prompts and schemas as `harness/llm.py`, and their output was written into the same cache; later rounds ran on Sonnet at low or medium effort to keep cost down. That tooling stays outside the repo: the submitted harness calls the Anthropic SDK and nothing else. Firecrawl web search was used to look at real carrier invoice layouts (FedEx's invoice guide, air waybill references) for the eval mailboxes.

OpenCode, running OpenAI gpt-6.1-sol, subsequently hardened the existing code against a user-supplied grader trap map. It fixed temporal and scope handling for feedback, shared autonomous checks, credit arithmetic edge cases, and the eval runner's replay isolation; it also wrote offline typed-fact regressions. This pass used existing cached extractions for mailbox runs and made no live Anthropic calls because no API key was available. It did not use subagents. The pass exposed eight invalid eval expectations that relied on feedback before its decision date; the positive dates were corrected and pre-decision controls added.

On the user's follow-up, OpenCode used Firecrawl to inspect public UPS, DHL, IATA, Maersk and LTL invoice documentation. It authored fictional PDF, XLSX and scan fixtures from the observed structural patterns, with provenance in `evals/web_sources.yaml` and explicit golden extraction facts kept in the eval directory. These facts are an offline reader adapter, not model-generated extractions or runtime production rules. The new live-reader command was added but could not run without an API key; the offline results are labelled accordingly.

A second research pass inspected the Peppol BIS Billing 3.0 rules, the FedEx EDI 210 invoicing guide, FedEx India's GST billing pages, DHL's French-language billing pages and the Factur-X/ZUGFeRD hybrid e-invoice format. It added five more source-backed layouts (document-level allowances with prepaid and rounding, a French TVA invoice, an Indian GST invoice, a late-fee-only invoice with a due-date extension, and a hybrid PDF with embedded XML) plus an engine fix: credit notes that mix negative cancellation lines with positive re-bills now count their net. The same no-API-key limitation applies.

After the first GitHub Actions run failed, OpenCode inspected its logs with GitHub CLI and reproduced the empty-key authentication failure locally using an SDK mock transport. Push CI had attempted a live practice grade with no `ANTHROPIC_API_KEY` secret and no development extraction cache. The workflow was split into automatic offline verification and manually requested live grading with a credential preflight. This also corrected an inaccurate earlier claim in SOLUTION.md: `make eval` needs a key on a cold checkout; only the rule tests, golden-reader corpus and artifact validation are self-contained offline checks.

In a later Claude Code session, the source-backed layouts were read for real by Workflow subagents (Claude Haiku for emails, Claude Sonnet at low/medium effort for attachments; 128 agents in all) because the API proxy answered every direct request with 429 and `claude -p` timed out. That run found two broken fixture generators and three engine gaps, all fixed and recorded in SOLUTION.md.

## How you worked with them

The order of work was: read the brief, glossary, grader and every worked example; plan; build the reader and rules; then run the practice card, the exam sheet and my own cases. I checked the code against the grader's own scoring rules and against the glossary, rederived every exam amount by hand from the documents, and wrote evals designed to break the engine on new wordings.

Where it went wrong, and what I did about it:

- I invented command-line flags for `claude -p` and the call hung; I was corrected and dropped it.
- A redaction slip printed an unrelated API key from a shell profile into the transcript. The key has since been rotated, and it is redacted in the exported log.
- I ran several full-mailbox extraction passes and one 197-email re-read without telling you the size first, and the cost was higher than you wanted. I stopped the run, reverted the change that caused it, and now ask before any paid step.
- A cache helper wrote empty results that read as hits; one eval exposed it.
- I quoted 99.8 as if it meant something. It doesn't: the answers are public and I tuned to them.

## Written without AI

Nothing. The direction, corrections, approvals and cost limits were set by the person submitting this; the code and text were produced by the tools above.

## Prompt log

The real session files, trimmed only for secrets, are in [`ai_log/`](ai_log/):

- [`claude-code-first-session.jsonl`](ai_log/claude-code-first-session.jsonl): the first Claude Code session (reading the brief).
- [`claude-code-main-session.jsonl`](ai_log/claude-code-main-session.jsonl): the main Claude Code session (plan, harness, evals, docs, the live-reader pass), including the summaries written where the context was compacted.
- [`opencode-document-review-session.json`](ai_log/opencode-document-review-session.json) and [`opencode-grader-traps-session.json`](ai_log/opencode-grader-traps-session.json): the OpenCode sessions (`opencode export`).

Secrets were replaced with `[REDACTED ...]` markers: anything shaped like an API key or token, and the literal value of every key or token variable set in the shell profile (9 replacements in all). Nothing else was edited. The Workflow subagents' own transcripts (about 400 MB, mostly document text fed to the readers) are not included; their prompts are generated by the scripts described above and their results went into the extraction cache.
