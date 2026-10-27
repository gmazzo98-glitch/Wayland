"""Deterministic multilingual query packs used before optional LLM follow-ups."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List


TOPICS = ("ownership", "people", "events", "certifications", "awards", "tenders", "hiring")


@dataclass(frozen=True)
class Query:
    text: str
    topic: str
    language: str


def _variants(company: Dict) -> List[str]:
    name = (company.get("legal_name") or company.get("name") or "").strip()
    variants = [name]
    if name:
        variants += [name.replace(" S.P.A.", " SpA").replace(" S.p.A.", " SpA"),
                     name.replace(" S.R.L.", " Srl").replace(" S.r.l.", " Srl")]
    brand = (company.get("brand") or "").strip()
    if brand and brand not in variants:
        variants.append(brand)
    return list(dict.fromkeys(v for v in variants if v))


_TERMS = {
    "ownership": {"it": "proprietà acquisizione gruppo", "en": "ownership acquisition parent group", "de": "Eigentümer Übernahme Konzern"},
    "people": {"it": "amministratore direttore team", "en": "director manager team", "de": "Geschäftsführer Leitung Team"},
    "events": {"it": "storia acquisizione investimento stabilimento", "en": "history acquisition investment plant", "de": "Geschichte Übernahme Investition Werk"},
    "certifications": {"it": "certificazioni ISO certificato", "en": "certifications ISO certificate", "de": "Zertifizierungen ISO Zertifikat"},
    "awards": {"it": "premio riconoscimento associazione", "en": "award recognition association", "de": "Auszeichnung Verband"},
    "tenders": {"it": "gara appalto fornitura", "en": "tender contract supply", "de": "Ausschreibung Auftrag"},
    "hiring": {"it": "lavora con noi offerte lavoro", "en": "careers jobs hiring", "de": "Karriere Stellenangebote"},
}


def build_query_pack(company: Dict, topics: Iterable[str] = TOPICS) -> List[Query]:
    """Build stable Italian/English/German queries; callers can add LLM-generated follow-ups later."""
    out = []
    for topic in topics:
        if topic not in _TERMS:
            continue
        for lang, terms in _TERMS[topic].items():
            for variant in _variants(company):
                suffix = company.get("vat") if lang == "it" and company.get("vat") else ""
                text = f'"{variant}" {terms}' + (f' {suffix}' if suffix else "")
                out.append(Query(text=text, topic=topic, language=lang))
    return out


def queries_for(company: Dict, topic: str) -> List[str]:
    return [q.text for q in build_query_pack(company, (topic,))]
