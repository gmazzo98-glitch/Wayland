# Golden Benchmark Set

Human-checked ground truth for Project Vienna's deep-intelligence layer: for each company, what a researcher can find with public sources, fact by fact, with the page it was read on, a grade, a weight, the regex that tells `scripts/intel_gap.py` whether the tool holds it, and a list of what was searched for and **not** found.

The set was first written by ticket I03 (2026-10-27) and **rebuilt by ticket I94** after an audit found unverifiable sources and patterns that matched any text (see `docs/intel/results/I94.md`). `asco_filtri.json` is the older I01 file and has not been through the I94 checks yet (ticket I101).

---

## 1. Split

| File | Split | Archetype | Facts | Categories |
|---|---|---|:---:|:---:|
| `asco_filtri.json` | development | reference company: subsidiary of a US listed group (I01, not yet re-checked) | 42 | 13 |
| `isolcell.json` | **development** | (c) South Tyrol company, six-language site, German Impressum; owner-controlled through a personal holding (a naive subsidiary flag is a false positive) | 32 | 12 |
| `corrada.json` | **development** | subsidiary of a listed group whose only web presence is a page on the parent site (`corrada.it` redirects); the page footer carries the PARENT's VAT. Not JavaScript-only (see 4) | 31 | 11 |
| `bertazzon_3b.json` | **hold-out** | (a) owner-managed family firm, thin custom PHP site, no named people, no certificates | 31 | 11 |
| `pomini_rubber_plastics.json` | **hold-out** | (b) empty `website_url` in the database; the real site is a JavaScript-only single-page app (also (f)); German foundation group | 37 | 12 |
| `eurosicma.json` | **hold-out** | (d) wholly owned subsidiary of an Italian group (IMA), operating loss, `eurosicma.it` only redirects to the IMA brand page | 30 | 11 |
| `emmegi_heat_exchangers.json` | **hold-out** | (e) name collision: another Emmegi (Voilap group) and a second `EMMEGI S.R.L.` in the roster itself | 30 | 11 |

> **Hold-out rule.** Agents building collectors (waves 1 and 2) develop against the development files only. The four hold-out files are reserved for release captains (I90, I91, I92) and evaluation CI (I43). Each file carries its `split`.

---

## 2. Why these companies, and what the research found (corrected)

* **(a) Bertazzon 3B** (Sernaglia della Battaglia, TV; VAT 00190680264). Whole board and 99.99 percent of the shares are one family (AIDA, independence A+). The site (about 11 pages) publishes address, phone, product models, fairs, public-funding notices, but no people and no certificate: the earlier draft's EN 13814 / ASTM claims had no source. Corrections: VAT digits were transposed, ATECO is 28.99.92 not 28.99.99.
* **(b) Pomini Rubber & Plastics** (Rescaldina, MI; VAT 05699990965). The database has no website. `hf-mixinggroup.com` now redirects to `www.hf-group.com`, a client-rendered app that returns the same 10,808-byte shell for every path, so product facts were verified on archived copies (web.archive.org) of the old static site. The earlier draft's phone number was wrong (real: +39 0331 743-111) and its `pominirp.com` does not resolve.
* **(c) Isolcell** (Laives, BZ; VAT 00671570216). Four divisions (storage, industry, fire prevention, cultural heritage) built on controlled-atmosphere and oxygen-reduction technology, certified ISO 9001/14001/45001, product standards listed, trade fairs in the news feed. Two traps: the site states two different share capitals (EUR 4,000,000 footer, EUR 2,000,000 Impressum) and the registry names Markus Telch as sole administrator while Marco Pruneri is general manager (the earlier draft called him CEO).
* **(d) Eurosicma** (Vignate, MI today, Segrate in 2017 news; VAT 00730100153). IMA bought 60 percent in 2017 (about EUR 26M, put and call on the rest by April 2027); AIDA shows IMA at 100 percent and a control chain up to an investment fund. Latest accounts: revenue EUR 16.1M and an operating loss of EUR 3.2M.
* **(e) Emmegi S.p.A.** (Cassano d'Adda, MI; VAT 11057370154). Hydraulic and engine heat exchangers, 100 percent one family, plants in Italy and Slovakia, branches in seven countries. Collision: the Voilap group brand Emmegi (aluminium and PVC machining) and `EMMEGI S.R.L.` of Reggio nell'Emilia (VAT IT01681290357, in the roster) are other companies.
* **Corrada** (Lainate, MI; VAT 08890640157). Founded 1933, progressive lamination dies, 100 percent EuroGroup Laminations (listed since February 2023, controlled by EMS). Latest accounts show an operating loss of EUR 1.1M. The earlier draft called `corrada.it` a JavaScript-only site: it redirects to a server-rendered group page.

---

## 3. File format

```json
{
  "schema": 1, "split": "hold-out",
  "subject": {"company_id": "...", "legal_name": "...", "vat": "...", "website": "https://... or null",
              "website_note": "...", "city": "...", "never_hardcode": ["distinctive tokens, read by tests/test_intel_no_golden_leak.py"]},
  "researched_on": "2026-10-27", "researcher": "...", "how_to_read": "...",
  "facts": [{
    "id": "PR-01", "category": "products", "weight": 3, "grade": "B",
    "claim": "what the researcher found",
    "sources": ["https://page the quote was read on", "AIDA export held in the tool", "Roster company table (live database)"],
    "quote": "verbatim text from that page (or the AIDA / roster text), or a list of such quotes",
    "match": {"any": ["regex"], "all": ["regex"]},
    "closed_by": ["I10"]
  }],
  "not_found": [{"looked_for": "...", "where": "...", "result": "not found / unresolved / contradicted"}]
}
```

* Categories: `identity, contact, ownership, events, financials, people, products, certifications, locations, networks, digital, documents`. Grades: `A` official or filed data, `B` the company's own publication, `C` verified third party, `D` inference or targeting consequence. Weights: `3` decisive for outreach, `2` important, `1` context.
* `quote` and `not_found` are additions of I94 (`intel_gap.load_golden` ignores them). A fact whose evidence is the AIDA text uses the source `AIDA export held in the tool`; its quote is a fragment of the text `intel_gap.py --db` scores against.
* `weight 3` facts of grade `D` are *targeting consequences* (for example "this is a controlled subsidiary"); they only match text that states the consequence, never the raw shareholder list.
* `quote` is not a promise that the page still says the same: pages change. `web.archive.org` copies are used where the live site cannot be read without rendering.

## 4. Known gaps

* No development company is JavaScript-only (Corrada is not). The only JavaScript-only site is the HF Group site behind the Pomini hold-out file. Ticket I102.
* `asco_filtri.json` has no quotes or not_found list and fails the I94 pattern checks. Ticket I101.
* The I94 checker is attached to `docs/intel/results/I94.md` (`I94_tools/verify_golden.py`); it needs the network and the live-database read access, so it is not part of the offline test suite. Ticket I101 promotes it.
* Baselines (see the results note): the database holds mostly AIDA and registry data; the web layer holds almost nothing of these six companies.
