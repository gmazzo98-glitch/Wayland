"""
Unit tests for the verification gate (intel/verify.py, ticket I06).
Acceptance:
  - 20+ labelled benchmark cases (precision >= 0.95, recall reported).
  - Name-collision detection: Emerson-ASCO, MP Filtri patents, Voilap Emmegi, Tenova Pomini, sports homonyms.
  - Quote grounding, entity matching with discriminators, date/numeric sanity, contradiction handling.
  - Optional LLM judge with cache.
  - Gate never raises on malformed input.
  - Documented thresholds as constants.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from intel.ledger import Evidence, Fact, Ledger
from intel.verify import (
    CONFIDENCE_COLLISION_PENALTY,
    CONFIDENCE_DATE_SANITY_PENALTY,
    CONFIDENCE_GROUNDING_FAILED,
    CONFIDENCE_NO_DISCRIMINATOR_PENALTY,
    CONFIDENCE_NUMERIC_SANITY_PENALTY,
    MAX_EMPLOYEE_FACTOR,
    MAX_REVENUE_FACTOR,
    MIN_CONFIDENCE_FOR_VERIFIED,
    MIN_EMPLOYEE_FACTOR,
    MIN_PLAUSIBLE_YEAR,
    MIN_QUOTE_LENGTH,
    MIN_REVENUE_FACTOR,
    Verdict,
    _LLM_JUDGE_CACHE,
    check_known_collisions,
    extract_brand_tokens,
    extract_domain,
    normalize_vat,
    verify,
)


def _make_fact(
    key: str = "ownership.direct_owner",
    value: Any = "Mott Corporation",
    quote: str = "In 2022 Mott Corporation acquired ASCO Filtri S.p.A. based in Binasco.",
    url: str = "https://www.ascofiltri.com/about",
    grade: str = "C",
    confidence: float = 0.85,
    source_type: str = "press",
    as_of: str = "2024-01-01",
) -> Fact:
    ev = Evidence(source_type=source_type, url=url, quote=quote)
    return Fact(
        key=key,
        value=value,
        grade=grade,
        confidence=confidence,
        collector="test:collector",
        as_of=as_of,
        evidence=[ev],
    )


# Standard test company baseline (ASCO Filtri)
ASCO_COMPANY: Dict[str, Any] = {
    "id": "asco-001",
    "legal_name": "ASCO FILTRI S.P.A.",
    "vat": "02688370135",
    "registration_number": "02688370135",
    "city": "Binasco",
    "province": "MI",
    "address": "Via Achille Grandi 2, 20082 Binasco MI",
    "website_url": "https://www.ascofiltri.com",
    "headcount": 68,
    "turnover": 28_000_000,
    "people": ["Massimo Mascherpa", "Ennio Madaschi"],
}


# ===========================================================================
# 1. Documented Constants
# ===========================================================================

def test_documented_constants_are_exposed():
    assert MIN_QUOTE_LENGTH >= 10
    assert MAX_EMPLOYEE_FACTOR == 3.0
    assert MIN_EMPLOYEE_FACTOR == pytest.approx(1.0 / 3.0)
    assert MAX_REVENUE_FACTOR == 2.0
    assert MIN_REVENUE_FACTOR == 0.5
    assert MIN_PLAUSIBLE_YEAR == 1850
    assert MIN_CONFIDENCE_FOR_VERIFIED >= 0.60
    assert CONFIDENCE_GROUNDING_FAILED < 0.10


# ===========================================================================
# 2. Benchmark Suite: 24 Labelled Cases (Precision >= 0.95 & Recall Reported)
# ===========================================================================

LABELLED_CASES: List[Dict[str, Any]] = [
    # --- TRUE POSITIVES (12 cases) ---
    {
        "name": "TP01_valid_company_website_about",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="ownership.direct_owner",
            value="Mott Corporation",
            quote="In 2022 Mott Corporation acquired ASCO Filtri S.p.A. based in Binasco.",
            url="https://www.ascofiltri.com/about",
        ),
        "page_text": (
            "ASCO FILTRI S.P.A. - Binasco (MI) - P.IVA 02688370135. "
            "In 2022 Mott Corporation acquired ASCO Filtri S.p.A. based in Binasco."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP02_valid_press_release_with_vat",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="events.acquisition",
            value="IDEX Corporation acquisition",
            quote="IDEX Corporation completes acquisition of Mott Corporation and its subsidiary ASCO Filtri.",
            url="https://www.prnewswire.com/news-releases/idex-completes-acquisition-mott",
        ),
        "page_text": (
            "MILAN -- IDEX Corporation completes acquisition of Mott Corporation and its subsidiary ASCO Filtri. "
            "The Italian operations in Binasco MI (P.IVA 02688370135) join the IDEX energy group."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP03_valid_news_with_city_and_brand",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="products.lines",
            value="Process filtration systems",
            quote="ASCO Filtri manufactures advanced process filtration systems for oil and gas",
            url="https://www.filtsep.com/news/asco-filtri-systems",
        ),
        "page_text": (
            "FILTSEP NEWS: ASCO Filtri manufactures advanced process filtration systems for oil and gas "
            "from its headquarters in Binasco."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP04_valid_people_with_known_director",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="people.managing_director",
            value="Massimo Mascherpa",
            quote="Massimo Mascherpa leads ASCO Filtri as Managing Director.",
            url="https://www.ascofiltri.com/team",
        ),
        "page_text": (
            "Company Profile - ASCO Filtri. Massimo Mascherpa leads ASCO Filtri as Managing Director. "
            "Registered office in Binasco."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP05_valid_cap_postal_code_discriminator",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="contact.switchboard",
            value="+39 02 897031",
            quote="ASCO Filtri switchboard is available at +39 02 897031 for inquiries.",
            url="https://www.paginegialle.it/asco-filtri",
        ),
        "page_text": (
            "ASCO Filtri - Via Achille Grandi 2, 20082 Binasco. "
            "ASCO Filtri switchboard is available at +39 02 897031 for inquiries."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP06_valid_employee_count_in_range",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="financials.headcount",
            value=72,
            quote="ASCO Filtri currently employs 72 specialists at its production facility.",
            url="https://www.ascofiltri.com/company",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco. ASCO Filtri currently employs 72 specialists at its production facility."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP07_valid_revenue_in_range",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="financials.revenue",
            value=31_500_000,
            quote="The company closed 2023 with 31,500,000 euro turnover.",
            url="https://www.bebeez.it/asco-filtri",
        ),
        "page_text": (
            "Asco Filtri S.p.A. (Binasco, Milano). The company closed 2023 with 31,500,000 euro turnover."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP08_valid_grade_a_official_registry_no_quote_needed",
        "company": ASCO_COMPANY,
        "fact": Fact(
            key="ownership.ultimate_owner",
            value="IDEX CORPORATION",
            grade="A",
            confidence=0.98,
            collector="aida:registry",
            evidence=[Evidence(source_type="registry", ref="AIDA Raw Exports: controlling_shareholders")],
        ),
        "page_text": "",
        "expected_verified": True,
    },
    {
        "name": "TP09_valid_german_bilingual_isolcell",
        "company": {
            "id": "iso-001",
            "legal_name": "ISOLCELL S.P.A.",
            "vat": "00123450218",
            "city": "Laives",
            "province": "BZ",
            "website_url": "https://www.isolcell.com",
            "headcount": 75,
            "turnover": 25_000_000,
        },
        "fact": _make_fact(
            key="products.lines",
            value="N2ORS Fire Prevention",
            quote="Isolcell liefert N2ORS Sauerstoffreduktionssysteme fur den Brandschutz weltweit.",
            url="https://www.isolcell.com/de/brandschutz",
        ),
        "page_text": (
            "ISOLCELL S.p.A. - Leifers / Laives (BZ) - MwSt 00123450218. "
            "Isolcell liefert N2ORS Sauerstoffreduktionssysteme fur den Brandschutz weltweit."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP10_valid_corrada_spa_tooling",
        "company": {
            "id": "cor-001",
            "legal_name": "CORRADA S.P.A.",
            "vat": "00845120158",
            "city": "Lainate",
            "province": "MI",
            "website_url": "https://www.corrada.it",
            "headcount": 80,
            "turnover": 22_000_000,
        },
        "fact": _make_fact(
            key="ownership.direct_owner",
            value="EuroGroup Laminations S.p.A.",
            quote="Corrada S.p.A. opera come centro di eccellenza stampi per EuroGroup Laminations.",
            url="https://www.corrada.it/about",
        ),
        "page_text": (
            "CORRADA S.p.A. Lainate (MI) P.IVA 00845120158. "
            "Corrada S.p.A. opera come centro di eccellenza stampi per EuroGroup Laminations."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP11_valid_certifications_iso_ped",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="certifications.iso_9001",
            value="ISO 9001:2015",
            quote="ASCO Filtri holds quality certification ISO 9001:2015 certified by DNV.",
            url="https://www.ascofiltri.com/certifications",
        ),
        "page_text": (
            "Certifications of ASCO Filtri S.p.A. Binasco: "
            "ASCO Filtri holds quality certification ISO 9001:2015 certified by DNV."
        ),
        "expected_verified": True,
    },
    {
        "name": "TP12_valid_past_incorporation_event",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="events.foundation",
            value=2002,
            quote="ASCO Filtri was founded in 2002 as a filtration engineering company.",
            url="https://www.ascofiltri.com/history",
        ),
        "page_text": (
            "ASCO Filtri Binasco MI - ASCO Filtri was founded in 2002 as a filtration engineering company."
        ),
        "expected_verified": True,
    },

    # --- TRUE NEGATIVES / REJECTIONS (12 cases) ---
    {
        "name": "TN01_collision_emerson_asco_solenoid_valve",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="products.valves",
            value="ASCO RedHat Solenoid Valves",
            quote="Emerson ASCO solenoid valves deliver precise flow control for industrial automation.",
            url="https://www.emerson.com/en-us/automation/asco",
        ),
        "page_text": (
            "Emerson ASCO solenoid valves deliver precise flow control for industrial automation. "
            "ASCO Numatics leads the world in fluid control solutions."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN02_collision_mp_filtri_patents",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="ip.patents",
            value="Hydraulic Filter Patent EP1234567",
            quote="Patent EP1234567 assigned to MP Filtri S.p.A. for hydraulic return line filters.",
            url="https://patents.google.com/patent/EP1234567",
        ),
        "page_text": (
            "Patent EP1234567 assigned to MP Filtri S.p.A. for hydraulic return line filters. "
            "MP Filtri filtration technology."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN03_collision_emmegi_voilap_cnc_aluminum",
        "company": {
            "id": "emm-001",
            "legal_name": "EMMEGI S.P.A.",
            "vat": "00789010156",
            "city": "Cassano d'Adda",
            "province": "MI",
            "website_url": "https://www.emmegi-heat-exchangers.com",
            "headcount": 55,
            "turnover": 18_000_000,
        },
        "fact": _make_fact(
            key="products.cnc_machinery",
            value="5-axis CNC machining centers for aluminium",
            quote="Voilap Emmegi produce centri di lavoro CNC per profili in alluminio e serramenti.",
            url="https://www.emmegi.com/it/prodotti",
        ),
        "page_text": (
            "Emmegi - Soliera Limidi (MO) - Voilap Group. "
            "Voilap Emmegi produce centri di lavoro CNC per profili in alluminio e serramenti."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN04_collision_tenova_pomini_roll_grinding",
        "company": {
            "id": "pom-001",
            "legal_name": "POMINI RUBBER & PLASTICS S.R.L.",
            "vat": "04567890152",
            "city": "Rescaldina",
            "province": "MI",
            "website_url": "https://www.pominirp.com",
            "headcount": 60,
            "turnover": 20_000_000,
        },
        "fact": _make_fact(
            key="products.roll_grinders",
            value="Heavy roll grinding machines for steel mills",
            quote="Tenova Pomini delivers roll grinding machines and rettificatrici for steel mills worldwide.",
            url="https://www.tenova.com/metals/pomini",
        ),
        "page_text": (
            "Tenova Pomini delivers roll grinding machines and rettificatrici for steel mills worldwide. "
            "Pomini heavy roll shops for acciaierie."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN05_collision_corrada_football_player_athlete",
        "company": {
            "id": "cor-001",
            "legal_name": "CORRADA S.P.A.",
            "vat": "00845120158",
            "city": "Lainate",
            "province": "MI",
            "website_url": "https://www.corrada.it",
            "headcount": 80,
            "turnover": 22_000_000,
        },
        "fact": _make_fact(
            key="people.news",
            value="Serie B transfer news",
            quote="Il calciatore Corrada segna il gol decisivo in Serie B per la sua squadra",
            url="https://www.gazzetta.it/calcio/corrada-gol",
        ),
        "page_text": (
            "Calciomercato Serie B: Il calciatore Corrada segna il gol decisivo in Serie B per la sua squadra "
            "secondo l'allenatore."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN06_grounding_failure_hallucinated_quote",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="ownership.founder",
            value="Leonardo Da Vinci",
            quote="ASCO Filtri was founded in 1500 by Leonardo Da Vinci himself.",
            url="https://www.ascofiltri.com/history",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco. The company specializes in filtration engineering since 2002."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN07_brand_match_only_zero_discriminators",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="products.general",
            value="Generic filter bags",
            quote="ASCO filters supply premium filtration bags for swimming pools and spas.",
            url="https://www.random-filter-shop.com/pool",
        ),
        "page_text": (
            "Welcome to Filter Emporium! ASCO filters supply premium filtration bags for swimming pools and spas. "
            "Order online today from Dallas Texas."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN08_unrelated_company_entirely",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="ownership.owner",
            value="Stellantis N.V.",
            quote="Stellantis NV acquired Ferrari automotive production plants in Maranello Modena",
            url="https://www.autonews.com/stellantis",
        ),
        "page_text": (
            "Stellantis NV acquired Ferrari automotive production plants in Maranello Modena."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN09_date_sanity_future_year",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="events.planned_acquisition",
            value=2045,
            quote="In 2045 ASCO Filtri will expand into Martian orbital colonies.",
            url="https://www.ascofiltri.com/future",
            as_of="2045-06-01",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco P.IVA 02688370135. "
            "In 2045 ASCO Filtri will expand into Martian orbital colonies."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN10_date_sanity_ancient_year",
        "company": ASCO_COMPANY,
        "fact": _make_fact(
            key="events.foundation",
            value=1620,
            quote="ASCO Filtri started manufacturing during the Renaissance in 1620.",
            url="https://www.ascofiltri.com/history",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco MI - P.IVA 02688370135. "
            "ASCO Filtri started manufacturing during the Renaissance in 1620."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN11_numeric_sanity_wild_employee_count",
        "company": ASCO_COMPANY,  # baseline is 68
        "fact": _make_fact(
            key="financials.headcount",
            value=8_500,
            quote="ASCO Filtri employs over 8,500 factory workers worldwide.",
            url="https://www.ascofiltri.com/global",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco MI P.IVA 02688370135. "
            "ASCO Filtri employs over 8,500 factory workers worldwide."
        ),
        "expected_verified": False,
    },
    {
        "name": "TN12_numeric_sanity_wild_revenue",
        "company": ASCO_COMPANY,  # baseline is 28M
        "fact": _make_fact(
            key="financials.revenue",
            value=450_000_000,
            quote="The company reported 450,000,000 euro revenue for the latest fiscal year.",
            url="https://www.ascofiltri.com/financials",
        ),
        "page_text": (
            "ASCO Filtri S.p.A. Binasco MI P.IVA 02688370135. "
            "The company reported 450,000,000 euro revenue for the latest fiscal year."
        ),
        "expected_verified": False,
    },
]


def test_24_labelled_cases_precision_and_recall():
    """
    Evaluate verification gate on 24 labelled test cases:
    - 12 Positives (grounded, entity matched, discriminators present, sane metrics).
    - 12 Negatives (Emerson valve, MP filtri patent, Voilap Emmegi CNC, Tenova Pomini, sports homonym,
                    hallucinated quote, no discriminators, future date, ancient date, wild employees, wild revenue).
    Asserts precision >= 0.95 and reports recall.
    """
    tp = fp = tn = fn = 0
    results: List[Dict[str, Any]] = []

    for case in LABELLED_CASES:
        verdict = verify(
            fact=case["fact"],
            page_text=case["page_text"],
            company=case["company"],
        )
        predicted = verdict.verified
        expected = case["expected_verified"]

        if predicted and expected:
            tp += 1
            outcome = "TP"
        elif predicted and not expected:
            fp += 1
            outcome = "FP (BUG: false acceptance)"
        elif not predicted and not expected:
            tn += 1
            outcome = "TN"
        else:
            fn += 1
            outcome = "FN (false rejection)"

        results.append({
            "name": case["name"],
            "expected": expected,
            "predicted": predicted,
            "outcome": outcome,
            "decision": verdict.decision,
            "reasons": verdict.reasons,
        })

    total = len(LABELLED_CASES)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    print("\n--- I06 Verification Gate Benchmark Results ---")
    print(f"Total Cases: {total} | TP: {tp} | TN: {tn} | FP: {fp} | FN: {fn}")
    print(f"Precision: {precision:.4f} (Required: >= 0.9500)")
    print(f"Recall:    {recall:.4f}")

    for r in results:
        status_sym = "[OK]" if ("TP" in r["outcome"] or "TN" in r["outcome"]) else "[FAIL]"
        print(f"  {status_sym} {r['name']}: {r['outcome']} | decision={r['decision']} | reasons={r['reasons'][:2]}")

    assert precision >= 0.95, f"Precision {precision:.4f} fell below required 0.95 (FP={fp})"
    assert recall >= 0.90, f"Recall {recall:.4f} fell below acceptable 0.90 (FN={fn})"


# ===========================================================================
# 3. Specific Component Unit Tests
# ===========================================================================

def test_extract_brand_tokens():
    assert "asco filtri" in extract_brand_tokens("ASCO FILTRI S.P.A.")
    assert "asco" in extract_brand_tokens("ASCO FILTRI S.P.A.")
    assert "corrada" in extract_brand_tokens("CORRADA S.P.A.")
    assert "isolcell" in extract_brand_tokens("ISOLCELL S.P.A.")
    assert "bertazzon" in extract_brand_tokens("BERTAZZON 3B S.R.L.")
    assert extract_brand_tokens("") == []


def test_normalize_vat():
    assert normalize_vat("IT02688370135") == "02688370135"
    assert normalize_vat("02688370135") == "02688370135"
    assert normalize_vat("IT 0268-837-0135") == "02688370135"
    assert normalize_vat(None) == ""


def test_extract_domain():
    assert extract_domain("https://www.ascofiltri.com/about") == "ascofiltri.com"
    assert extract_domain("http://corrada.it:8080/path") == "corrada.it"
    assert extract_domain("isolcell.com") == "isolcell.com"
    assert extract_domain(None) == ""


def test_known_collisions_check():
    # ASCO valve collision
    res = check_known_collisions("asco", "Emerson ASCO solenoid valve catalog")
    assert res is not None and "Emerson" in res

    # ASCO exception pattern
    res_exc = check_known_collisions("asco", "Mott acquired ASCO Filtri based in Binasco")
    assert res_exc is None

    # Emmegi Voilap collision
    res_emm = check_known_collisions("emmegi", "Voilap Emmegi centri di lavoro per profili in alluminio")
    assert res_emm is not None and "Voilap" in res_emm

    # Pomini Tenova collision
    res_pom = check_known_collisions("pomini", "Tenova Pomini roll grinding machines for acciaierie")
    assert res_pom is not None and "Tenova" in res_pom

    # Corrada athlete collision
    res_cor = check_known_collisions("corrada", "Il calciatore Corrada autore di un grande gol in Serie B")
    assert res_cor is not None and "athlete" in res_cor


def test_contradiction_handling_against_existing_ledger():
    ledger = Ledger()
    # Existing verified fact
    f1 = _make_fact(
        key="ownership.ultimate_owner",
        value="IDEX CORPORATION",
        quote="ASCO Filtri is part of Mott, a subsidiary of IDEX Corporation.",
    )
    f1.verified = True
    ledger.add(f1)

    # New incoming fact claiming a different owner
    f2 = _make_fact(
        key="ownership.ultimate_owner",
        value="General Electric Inc.",
        quote="ASCO Filtri Binasco was acquired by General Electric Inc.",
    )
    page_text = "ASCO Filtri S.p.A. Binasco MI - ASCO Filtri Binasco was acquired by General Electric Inc."

    verdict = verify(fact=f2, page_text=page_text, company=ASCO_COMPANY, ledger=ledger)
    assert verdict.verified is False
    assert verdict.decision == "contradiction"
    assert verdict.contradiction_detected is True
    assert "General Electric" in str(verdict.contradiction_details)
    assert "IDEX CORPORATION" in str(verdict.contradiction_details)


def test_llm_judge_fallback_with_cache():
    # Setup mock LLM
    mock_llm = MagicMock()
    mock_res = MagicMock()
    mock_res.content = json.dumps({"same_company": True, "reason": "Mentions industrial filtration operations"})
    mock_llm.complete.return_value = mock_res

    # Fact with brand match but no explicit discriminators in raw page and external URL (no domain match)
    fact = _make_fact(
        key="products.lines",
        value="Syngas filtration units",
        quote="ASCO Filtri engineers high performance syngas filtration units.",
        url="https://www.thirdparty-energy-news.org/articles/syngas-filtration",
    )
    page_text = "ASCO Filtri engineers high performance syngas filtration units."

    verdict = verify(
        fact=fact,
        page_text=page_text,
        company=ASCO_COMPANY,
        llm=mock_llm,
    )
    assert verdict.verified is True
    assert "llm_judge" in verdict.discriminators_found
    assert mock_llm.complete.call_count == 1

    # Second call should hit the cache and not call LLM again
    verdict2 = verify(
        fact=fact,
        page_text=page_text,
        company=ASCO_COMPANY,
        llm=mock_llm,
    )
    assert verdict2.verified is True
    assert mock_llm.complete.call_count == 1  # Unchanged due to cache


def test_verify_never_raises_on_malformed_input():
    # Test completely garbage inputs
    bad_fact = Fact(
        key="test.key",
        value=None,
        grade="C",
        collector="none",
        evidence=[],
    )
    # Should not raise exception
    verdict = verify(fact=bad_fact, page_text="", company={})
    assert isinstance(verdict, Verdict)
    assert verdict.verified is False
    assert verdict.decision in ("rejected", "unverified")

    # None page_text
    verdict2 = verify(fact=bad_fact, page_text=None, company=None)
    assert isinstance(verdict2, Verdict)
    assert verdict2.verified is False
