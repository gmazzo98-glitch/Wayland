# I96 — I13 rework: events collector de-hard-coded, rebuilt on discovery and quote grounding

## What I built (changed)

- `intel/collectors/events.py` — rewritten harvest, one behavior at a time:
  * **Removed every company-specific artifact the review found**: the six golden-source URLs
    (PR Newswire, two BeBeez articles, two mottcorp pages, MarketScreener) fired by an `if "asco"`
    test; the `red-fish`/`mott`/`asco-filtri` path tokens; every pre-written event (the 2019 club
    deal with parties and the "EUR 19.9M revenue (FY2018)" amount, the 2022 Mott event with its
    pre-written conflict note, the 2015 partnership, the Michelini leadership event); the
    parties keyed on description words; and the `ascofiltri.com`/`mottcorp.com` domain tests for
    the evidence source type (now: the company's own `website` from the input, like every other
    collector).
  * **Press URLs are discovered, never listed**: the I05 search client (`ctx.search`, when the
    orchestrator supplies one), the public site-search endpoint of the deal press the roadmap names
    for I13 (BeBeez `?s=<company name>`), and DuckDuckGo HTML through `ctx.fetch`. Same-host article
    links only; navigation/author/category/pagination filtered; 14 pages max.
  * **Events are built only from sentences of the fetched page**: a sentence must name the company
    (word-boundary entity match) or — only for financial-figure sentences (ricavi/EBITDA/milioni) —
    sit within 4 sentences of one that does; it must start like a sentence (not a cut fragment),
    pass `quote_in_text` against the page text, be under 350 characters (menu blobs are not
    events) and not contain sharing-bar/byline glue. Parties are the capitalised names in that
    same sentence. Title and description are the sentence itself, quotes capped at 390 (ledger
    limit); an event whose quotes were all over-long is dropped, not stored without evidence.
- `intel/events.py` — `_is_same_event` no longer holds the golden deal tokens (`mott`, `red-fish`,
  `idex`, `ascorp`, `binasco`, `emerson`): the same-event test is kind + shared non-generic tokens,
  and `merge_events` takes the company name so the subject's own tokens never count as a shared
  "party" (this bug merged ASCO's 2022 Mott and 2024 IDEX milestones into one; fixed and verified).
- `tests/test_intel_events.py` — the `RecordedFetcher` now exposes links like the real fetcher;
  two new regression tests: the source must not contain any golden-company URL/owner token/
  pre-written event string, and a generic deal page (Acme Pressure) must yield only quote-grounded
  events about that company while a sentence about another company's deal is not recorded.
- `tests/test_intel_no_golden_leak.py` — deleted the two `I96` lines from `ALLOWED_UNTIL_FIXED`
  (the baseline only shrinks).

## Verified live

- **ASCO Filtri (golden)**: `intel_run --collector events` → 13 facts, 0 refused, 27 pages, ~110 s.
  Timeline: 2002 foundation, 2004 global expansion, 2013 new headquarters, 2019 Red-Fish club-deal
  ownership_change (discovered via BeBeez site search, sentence "La holding di partecipazioni
  industriali Red-Fish Kapital ha rilevato il 100% di Asco Filtri…"), 2022 Mott, 2023 India, 2024
  IDEX — all quote-grounded, the own-site ones grade B, the press one grade C. No canned content:
  every string comes from the fetched pages.
- **Isolcell (hold-out)**: 13 facts, 0 refused — its complete own-site milestone timeline
  (1958 Bolzano founding through 2019 ADOX redesign, 15 milestones) discovered from the history
  page in Italian, each with its sentence as quote.
- **Corrada (hold-out)**: 4 facts, 0 refused — press-sourced events from the EuroGroup pages
  (founded-family shareholding sentence, FY estimates), honest: no timeline page exists on its own
  site (SPA with client-side rendering), and the collector says so by returning only what it found.
- **Precision judgement (read by eye)**: ASCO 11/11 timeline events genuinely about ASCO; Isolcell
  15/15 about Isolcell; Corrada 3/3 about Corrada/EuroGroup. Earlier drafts of the press sentence
  filter let sidebar noise in (menus, other companies' deals) — fixed and re-verified on all three
  companies.

## Golden score before / after

- The **I13 claimed score (10/10 golden facts, 100%) was produced by the canned events** and is
  void. After the rework, with honest discovery only: `--require-ticket I96` → **OK** (I96 has no
  golden facts of its own); ASCO web-only coverage from this collector alone: **11 of 42 facts
  (33% weighted)** including OW-01..04, EV-01..03, PE-02, PE-03. The facts still missed honestly:
  **OW-05 / PE-05** (the 19,9M revenue and Arcari sentences live in the BeBeez club-deal article,
  which the collector *does* read — but during batch fetching BeBeez serves a shorter page variant
  without the article body; when fetched alone the sentences are extracted correctly), and
  **PR-04** (Mott's authorised-representative fact lives on mottcorp.com, which no generic
  company-name search surfaced). These are search-layer limits (I05), not collector limits, and
  are recorded here rather than gamed.
- The status-file line "Combined with I14, web-only golden coverage on ASCO reached 21/42 (56%)" is
  **inflated by the canned events** and must be re-measured by I90 with this collector.

## Decisions taken and why

- Press discovery keeps BeBeez's public site search: it is the source the roadmap names for I13,
  it is name-keyed (no company hard-coding), and it found the real deal articles. DuckDuckGo HTML
  returns 202/challenge pages from this environment and Google News article links are blocked by
  robots.txt, so they are attempted but currently contribute nothing — I05 owns making the free
  search layer reliable, and a keyed provider (decision G1) would change this picture materially.
- Financial-figure sentences may lean on ±4-sentence context; deal sentences may not. This is the
  minimal context rule that captured "Ha chiuso il 2018 con ricavi per 19,9 milioni…" without
  letting "Zeta Capital acquisisce Unrelated Wheels" through.
- The collector tags facts `I96:events@2` so a reviewer can tell reworked facts from any stored
  before the rework. The Collector's `ticket` stays "I13" (traceability to the roadmap deliverable).
- No gg_input/gg_approval needs applied.

## Not done / open points

- OW-05/PE-05 depend on BeBeez serving the full article body during a run (observed both variants
  within minutes); a re-run may capture them. A retry/backoff for press pages belongs to I05.
- PR-04 (mottcorp.com partnership page) is unreachable by generic search: needs either the I05
  keyed search (G1) or the parent-group site walk from I12.
- `events.timeline` merges by kind + tokens; two genuinely different same-year acquisitions by the
  same acquirer would merge (rare; conflict detection still records date disagreements).
- Corrada's own site is client-side rendered: no own-site events. The Playwright fallback is I05's.

## New tickets proposed

- I05 follow-up (not filed here; I05 is DONE and owned by another agent): the free search layer
  returns 0 usable web results in this environment (DDG 202, Google News robots-blocked, GDELT
  429-throttled). Every press-dependent collector inherits this limit. Should be raised as a
  rework ticket by the reviewer of I05, or G1 (a keyed search API) unblocks it.

## What the next agent must know

- `python -m pytest tests/test_intel_events.py tests/test_intel_no_golden_leak.py` is the guard:
  the leak test now fails if anyone re-adds a golden token to `intel/events.py` or
  `intel/collectors/events.py` (the `I96` allowlist entries are gone; do not re-add them).
- `merge_events(events, company_name=...)`: pass the company name or different milestones that
  share only the subject's own tokens will merge.
- Press sentences: keep the three gates (entity-or-bounded-context, sentence-start, quote_in_text)
  if you touch the filter; each was added to fix a real observed false positive on live pages.
- I90 (release captain) must re-measure the wave-1 ASCO web-only coverage with this collector: the
  56% figure in the status file includes the void I13 claims.
