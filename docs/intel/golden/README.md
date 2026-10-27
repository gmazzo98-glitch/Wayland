# Golden Benchmark Set

This directory contains the human-researched ground truth for Project Vienna's deep-intelligence layer. Each file represents what an expert researcher discovered in approximately 90 minutes using public web search, corporate filings, registries, and official portals, fact by fact with URLs, evidence grades, weights, and match regex patterns.

The gap scorer (`scripts/intel_gap.py`) evaluates the tool's stored knowledge against these golden benchmarks.

---

## 1. Dataset Split

To prevent agents from overfitting collectors to a single company, the dataset is split into **Development** and **Hold-out** sets:

| File | Split | Archetype / Test Purpose | Facts | Categories |
|---|---|---|:---:|:---:|
| `asco_filtri.json` | Development | Foundational reference benchmark: US listed corporate group subsidiary (IDEX/Mott) | 42 | 13 |
| `isolcell.json` | Development | **(c) South Tyrol bilingual firm**: German/Italian naming, regional subsidies/networks | 33 | 11 |
| `corrada.json` | Development | **(f) JavaScript-only site**: Modern Next.js client-side SPA, zero fields extracted by legacy crawler | 31 | 12 |
| `bertazzon_3b.json` | **Hold-out** | **(a) Owner-managed family firm**: Thin traditional site, 100% individual family ownership | 32 | 12 |
| `pomini_rubber_plastics.json` | **Hold-out** | **(b) Empty/missing website in DB**: Possehl/HF German foundation subsidiary, historical brand | 31 | 12 |
| `eurosicma.json` | **Hold-out** | **(d) Italian group subsidiary**: Acquired by IMA S.p.A. (Vacchi family / BDT Capital) | 31 | 12 |
| `emmegi_heat_exchangers.json` | **Hold-out** | **(e) Name collision (T20 problem)**: Conflated with Voilap Emmegi CNC and Argo Emmegi Srl | 31 | 12 |

> [!IMPORTANT]
> **Hold-out rules**: Ticket agents building collectors (Wave 1 and Wave 2) must develop against `asco_filtri.json` and the development companies (`isolcell.json`, `corrada.json`). The four hold-out files are strictly reserved for Release Captains (`I90`, `I91`, `I92`) and Evaluation CI (`I43`) to assess generalization on unseen companies.

---

## 2. Selection Rationale for the Six Benchmark Companies

The 6 companies were picked from the 953 live roster to cover the core failure modes identified in `docs/INTEL_ROADMAP.md`:

### (a) Owner-managed family firm with thin site: `bertazzon_3b.json`
* **Subject**: BERTAZZON 3B S.R.L. (Sernaglia della Battaglia, TV; P.IVA `01906800264`)
* **Why chosen**: 100% owned by individual family descendants of the three founding brothers (Luigi, Ferruccio, Marcello). No holding company, no corporate parent, independence class A. Operates in niche manufacturing (fairground rides, carousels, bumper cars) with a traditional product-gallery website (`bertazzon.com`).
* **Test challenge**: Tests whether the tool can detect authentic family ownership without corporate parent chains, recognize specialized niche manufacturing, and capture international standards (EN 13814, ASTM) from traditional HTML.

### (b) Empty or missing website URL in the database: `pomini_rubber_plastics.json`
* **Subject**: POMINI RUBBER & PLASTICS S.R.L. (Rescaldina MI / Castellanza VA; P.IVA `05699990965`)
* **Why chosen**: One of 16 companies in the roster with `website_url` set to `None` in the database. A historic Italian brand (Luigi Pomini, founded in Castellanza in 1886) that was acquired by Harburg-Freudenberger and Possehl Group (Lübeck, Germany). Actual websites are `www.hf-mixinggroup.com` and `www.pominirp.com`.
* **Test challenge**: Tests web discovery (finding the real URL when the database field is blank), historical brand attribution (differentiating from Tenova Pomini), and tracing ultimate German foundation ownership.

### (c) South Tyrol / German-named bilingual company: `isolcell.json`
* **Subject**: ISOLCELL S.P.A. (Laives / Leifers BZ; P.IVA `00671570216`)
* **Why chosen**: Located in Bolzano province (Südtirol). World leader in controlled atmosphere storage (DCA) and active oxygen-reduction fire prevention (N2ORS). Website and documents are published in Italian, German, and English.
* **Test challenge**: Tests multilingual entity resolution (Laives vs. Leifers, Finanziaria Unterland, VdS certifications in German), regional network discovery (IDM Südtirol, Assoimprenditori Alto Adige), and patented technological terms.

### (d) Subsidiary of an Italian industrial group: `eurosicma.json`
* **Subject**: EUROSICMA - COSTRUZIONI MACCHINE AUTOMATICHE S.P.A. (Segrate MI; P.IVA `00730100153`)
* **Why chosen**: Founded in 1965 by the Redaelli family, acquired in July 2017 (60% stake for EUR 25.8M) by packaging giant I.M.A. S.p.A. (Bologna, Vacchi family / BDT Capital).
* **Test challenge**: Tests whether the tool identifies that a seemingly independent S.p.A. is actually a subsidiary of an Italian packaging group, detects the 2017 M&A event, and distinguishes local operating management from parent-group governance.

### (e) Name-collision case (The T20 Problem): `emmegi_heat_exchangers.json`
* **Subject**: EMMEGI S.P.A. (Cassano d'Adda MI; P.IVA `11057370154`)
* **Why chosen**: Severe name collision. A web search for "Emmegi S.p.A." is overwhelmingly dominated by `EMMEGI S.P.A.` in Limidi di Soliera (MO) (Voilap Group, CNC machining centers for aluminium profiles, P.IVA `01978870364`). Furthermore, the Vienna roster itself contains another company named `EMMEGI S.R.L.` in Reggio Emilia (P.IVA `01681290357`, Argo Tractors).
* **Test challenge**: The tool must use entity discriminators (VAT `11057370154`, city Cassano d'Adda, product "scambiatori di calore / heat exchangers") and reject articles, revenues, and addresses belonging to Voilap or Argo.

### (f) JavaScript-only / Client-rendered site: `corrada.json`
* **Subject**: CORRADA S.P.A. (Lainate MI; P.IVA `08890640157`)
* **Why chosen**: Founded in 1933, precision tooling and progressive die manufacturer, subsidiary of Euronext-listed EuroGroup Laminations S.p.A. Its official site `www.corrada.it` is built entirely as a Next.js / React client-rendered Single Page Application.
* **Test challenge**: The legacy crawler crawled 1 page and extracted 0 fields (`not_found` across all 12 fields). Tests headless rendering, Next.js JSON data extraction, and linking the company to parent EuroGroup Laminations filings.

---

## 3. Fact Schema & Verification Standards

Every golden fact adheres to the schema validated by `scripts/intel_gap.py`:

```json
{
  "id": "PR-01",
  "category": "products",
  "weight": 3,
  "grade": "B",
  "claim": "Clear descriptive statement of the fact.",
  "sources": ["https://..."],
  "match": {
    "any": ["pattern1", "pattern2"],
    "all": ["required_term1", "required_term2"]
  },
  "closed_by": ["I10"]
}
```

* **Categories**: `identity`, `contact`, `ownership`, `events`, `financials`, `people`, `products`, `certifications`, `locations`, `networks`, `digital`, `documents`.
* **Grades**: `A` (official / filed data), `B` (company's own publication), `C` (verified third party), `D` (inference / consequence).
* **Weights**: `3` (decisive for outreach), `2` (important), `1` (useful context).
* **Pattern Tightness**: All regex patterns are tested against empty, random, and unrelated text to guarantee that no pattern is trivially true.
