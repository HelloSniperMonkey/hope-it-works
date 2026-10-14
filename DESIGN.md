# Design

The model reads; Python decides. Claude turns each document into typed facts, and plain Python applies the glossary's rules to those facts. The model never picks a verdict, an amount or an action.

## Fact model

Emails and attachments become JSON records under `--state-dir/docs/`: contracts, instruments, invoices, credits, AWBs, declarations, tracking, ledgers and email facts. The model cites `[page:line]` tags; ingest resolves these to the grader-compatible page text. Email receipt dates control availability. Reports recompute authority from Schedule 1 addresses and scoped feedback, then rebuild from all available facts. Content-hash caching avoids rereading unchanged documents. Ingestion preserves successful documents but exits nonzero on extraction failures; report diagnostics expose parsed invoice ids, line counts, totals and unreadable documents.

## Conflicts

One resolver gathers the MSA, signed instruments and authorised notices in force on the pickup date, excluding quoted and automation-directed terms. The held contract's precedence clause ranks them. Unranked sources giving different correct amounts produce a conflict: `[max(0, billed − higher), billed − lower]`. A referenced rates document that is absent and could change the answer produces `cannot_determine`. Its holder comes from Schedule 1. Resent credit notes count once by contract and printed credit id.

## Confidence

Hand-set, not fitted: findings start at 0.93, conflicts/missing documents at 0.85. Penalties cover aggregated credits, weaker pickup sources, INR conversion, feedback and inconsistent invoice arithmetic (−0.25). Practice currently observes no wrong reported items, so AUROC is unmeasurable. The exam has no public key; the authored tests do not yet provide enough real extractor errors to calibrate these weights.

## People in the loop

Supervised mode drafts outbound actions; internal alerts/reminders run. Conflicts escalate. An invoice's aggregate outbound amount of at least $5,000 requires review in both modes. Shared autonomous checks cover repricing, waivers, duplicates and credits: trusted pickups, consistent arithmetic, a Schedule 1 recipient, no relevant conflict and a reversible remedy. Document requests use the Schedule 1 holder. Failed checks require review. No acceptance, renewal confirmation or payment action is emitted.

## Feedback

Feedback persists ids, batch and decision timestamps. Future decisions are excluded, then available decisions apply chronologically. Every `applies_to` filter must match, including contract, lane, person, party, category and full key; unknown filters fail closed. Standing dates cover documents/shipments. One-off approvals can retrospectively approve only their named key/months. Close/reject remove that key; a later amount override can reopen it. Later signed amendments beat older feedback, including backdated amendments. Changed items carry `learned_at`.

## New documents

Snapshots persist key, id, verdict and amount per mode. Changes create a new id with `supersedes`; a newly discoverable backdated finding gets `retro: true`. Recomputed windows remove stale expiry and risk items. Classification has heading safeguards for credit/debit adjustments, customs commercial invoices, pro formas and AWBs. The reader distinguishes billed/net amounts, correction deltas, piece details, prior balances and remittance repetitions. Fuel bases and per-quantity fees follow the held contract, rather than carrier-wide defaults.

## Cost

Unmeasured estimate: $40–60 per 1,000 documents at the assumed Opus 5.5 prices ($4/$20 per million input/output tokens). Contracts dominate. Hash caches avoid repeated calls. Twelve parallel email pipelines amortise latency, but live throughput against the 600-second grader limit is untested.

## What I'd do next

Exercise live invoice/contract extraction, then the new layout corpus; the contract schema remains large. Test cheaper models by document kind. Measure confidence against actual wrong extractions. Margin step-down and promised credits have arithmetic regressions, but need live extraction cases. Novel waiver wording, units and layouts remain the main uncertainties.

## Verification after the grader-trap review

`make eval` uses the grader's isolated replay: fresh processes, restricted environment, incremental batches and reports before feedback. Every report's schema, keys and citations are checked. Timeline expectations cover `retro`, `supersedes`, `learned_at`, source documents and forbidden outbound text. Eight old expectations incorrectly used June feedback in earlier reports; positive dates were corrected and pre-decision controls added.

`make test` checks arithmetic, scope, the feedback triple, newer amendments, exactly-$5,000 aggregation, stale risks, all three credit types, isolated persistence and all five scorer gates. `make eval-web` builds source-backed fictional PDFs, spreadsheets and a scan, then replays both modes with explicit golden reader facts. It also checks parsed invoice ids, line counts and totals, so negative cases cannot pass by ignoring the document. UPS, DHL, IATA, Maersk, LTL, Peppol, FedEx EDI/GST and Factur-X source provenance is in `evals/web_sources.yaml`. A second pass added Peppol e-invoice semantics (document allowances, applied prepaid amounts, cash rounding, mixed-sign credit notes counted net), a French TVA layout with HT/TTC totals, an Indian GST invoice with CGST/SGST/IGST, a FedEx-style late-fee-only invoice with an authorised due-date extension, and a hybrid Factur-X PDF with embedded XML. `make eval-web LIVE=1` exercises the actual reader; on a cheaper Haiku/Sonnet reader it passes 54 of 56. Golden facts and cached mailbox passes do not establish live extraction quality or a hidden score.
