"""
Document parsing for the deep-intelligence layer (ticket I11).

The fetcher (intel/fetch.py) already turns a PDF URL into text with pypdf. This module adds what a
documents collector needs ON TOP of that text, without any network access of its own:

  * classify_document: what kind of company document the text/file name represents
    (certificate, manual, report, policy, other), from evidence in the text itself;
  * extract_certificate: standard, number, issuing body, issue/expiry dates, from the text of a
    certificate PDF or, where the document is a scan without a text layer, from its file name
    (dates such as ``...-20260629`` are how many sites encode validity);
  * extract_manual: the family code and title of an IOMS-style manual;
  * extract_report: the report year and headline figures (revenue, employees) from a first pages
    excerpt, so a 200-page annual report is never read whole;
  * select_pages: a cheap, token-bounded excerpt of the pages that carry the facts (certificates
    are 1-2 pages; for longer documents the first pages plus pages where key terms occur).

If pypdf produced no text at all the document is "scanned, not read": the collector records it as
such instead of pretending. OCR is deliberately not attempted here (ticket text: OCR only if
available, otherwise mark it); tesseract is not a dependency of this repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# How a certificate's validity is encoded in a file name: ISO_9001-...-13-20260629.pdf -> 2026-06-29
FILENAME_DATE_RE = re.compile(r"(20\d{2})[._-]?(\d{2})[._-]?(\d{2})(?=[._-]|\b)")
YEAR_RE = re.compile(r"\b(19[89]\d|20\d{2})\b")

STANDARD_PATTERNS = [
    ("iso 9001", re.compile(r"iso\s*9001(?:\s*:\s*(\d{4}))?", re.I)),
    ("iso 14001", re.compile(r"iso\s*14001(?:\s*:\s*(\d{4}))?", re.I)),
    ("iso 45001", re.compile(r"iso\s*45001(?:\s*:\s*(\d{4}))?", re.I)),
    ("ped module h1", re.compile(r"module\s+h1\b", re.I)),
    ("ped module h", re.compile(r"module\s+h\b", re.I)),
    ("asme u-stamp", re.compile(r"u[.\-_ ]?stamp", re.I)),
    ("national board", re.compile(r"national\s+board\s+r(egistration)?\b", re.I)),
    ("achilles", re.compile(r"achilles\b", re.I)),
]

# File-name spellings of the same standards (a scan or a masked text layer still names the standard)
STANDARD_FILENAME_PATTERNS = [
    ("iso 9001", re.compile(r"iso[_\- ]?9001", re.I)),
    ("iso 14001", re.compile(r"iso[_\- ]?14001", re.I)),
    ("iso 45001", re.compile(r"iso[_\- ]?45001", re.I)),
    ("ped module h1", re.compile(r"ped[_\- ]?h1\b", re.I)),
    ("ped module h", re.compile(r"ped[_\- ]?h\b", re.I)),
    ("asme u-stamp", re.compile(r"u[\-_]?stamp", re.I)),
    ("national board", re.compile(r"nationalboard\b|national[_\-]?board", re.I)),
    ("achilles", re.compile(r"achilles", re.I)),
]

ISSUER_HINTS = [
    ("DNV", re.compile(r"\bDNV\b|\bDNV[\-_ ]Business\s+Assurance\b", re.I)),
    ("Bureau Veritas", re.compile(r"\bbureau\s+veritas\b|\bBV\s+Certification\b", re.I)),
    ("TUV", re.compile(r"\bt[\u00fcu]v\b", re.I)),
    ("SGS", re.compile(r"\bsgs\b", re.I)),
    ("RINA", re.compile(r"\brina\b", re.I)),
    ("Accredia", re.compile(r"\baccredia\b", re.I)),
    ("ASME", re.compile(r"\basme\b", re.I)),
    ("National Board", re.compile(r"\bnational\s+board\b", re.I)),
    ("Achilles", re.compile(r"\bachilles\b", re.I)),
]

EXPIRY_RE = re.compile(r"\b(?:expiry|expire[ds]?|valid(?:ity)?\s*(?:until|to|date)|scadenza)[^\n]{0,40}?"
                       r"(\d{1,2}[./\-\s]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[./\-\s]+\d{2,4}"
                       r"|\d{4}[\-.]\d{2}[\-.]\d{2}|\d{1,2}[\-.]\d{1,2}[\-.]\d{2,4})", re.I)
ISSUE_RE = re.compile(r"\b(?:issue[ds]?|emission[ei]?|data(?:\s*di)?\s*(?:rilascio|emissione)|released|place and date)\b[^\n]{0,60}?"
                      r"(\d{1,2}[./\-\s]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[./\-\s]+\d{2,4}"
                      r"|\d{4}[\-.]\d{2}[\-.]\d{2}|\d{1,2}[\-.]\d{1,2}[\-.]\d{2,4})", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}


@dataclass
class ParsedDocument:
    kind: str                                   # certificate | manual | report | policy | other | scanned
    title: str = ""
    family_code: str = ""
    standard: str = ""
    number: str = ""
    issuer: str = ""
    issued_on: Optional[str] = None             # ISO yyyy-mm-dd
    expires_on: Optional[str] = None            # ISO yyyy-mm-dd
    year: Optional[int] = None
    figures: Dict[str, str] = field(default_factory=dict)
    date_source: str = "text"                  # text | file_name
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", [], {})}


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "")


def parse_date(token: str) -> Optional[str]:
    """dd.mm.yyyy / yyyy-mm-dd / '27 November 2024' / 'May 01, 2026' -> ISO date, or None."""
    token = _clean(token).strip(" .,-")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", token)
    if m:
        return token
    m = re.match(r"^(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})$", token)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if len(y) == 4 else 2000 + int(y)
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return date(y, mo, d).isoformat()
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})$", token)
    if m:
        name = m.group(2).lower()
        if name in MONTHS:
            return date(int(m.group(3)), MONTHS[name], int(m.group(1))).isoformat()
    m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", token)
    if m and m.group(1).lower() in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))).isoformat()
    return None


def _dates_from_filename(file_name: str) -> List[str]:
    out = []
    for m in FILENAME_DATE_RE.finditer(file_name or ""):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            continue
    return out


def classify_document(text: str, file_name: str = "", url: str = "") -> str:
    """What kind of company document this is, judged on the text (and, weakly, the file name)."""
    t = _clean(text)
    name = (file_name or url.rsplit("/", 1)[-1] or "").lower()
    if not t.strip() or len(t.strip()) < 20:
        return "scanned" if name.endswith(".pdf") else "other"
    low = t.lower()
    if re.search(r"certif", low) or any(p.search(low) for _, p in STANDARD_PATTERNS[:1]):
        if re.search(r"certif", low) and len(t) < 20000:
            return "certificate"
    if "manuale d" in low and "manutenzione" in low or "operating and maintenance manual" in low \
            or "installation and operation" in low:
        return "manual"
    if re.search(r"annual\s+report|sustainability\s+report|relazione\s+finanziaria|bilancio", low):
        return "report"
    if re.search(r"policy|politica", low) and len(t) < 20000:
        return "policy"
    if "certificate" in name or "cert_" in name or re.search(r"iso[_\-]", name) or "stamp" in name:
        return "certificate"
    if "manual" in name or "ioms" in name or "manuale" in name:
        return "manual"
    return "other"


def extract_certificate(text: str, file_name: str = "", url: str = "") -> ParsedDocument:
    """Facts from a certificate PDF (or its file name when the PDF is a scan)."""
    doc = ParsedDocument(kind="certificate", title=(file_name or url.rsplit("/", 1)[-1]))
    low = _clean(text).lower()
    for label, pat in STANDARD_PATTERNS:
        m = pat.search(text or "")
        if m:
            doc.standard = label + (f":{m.group(1)}" if m.groups() and m.group(1) else "")
            break
    if not doc.standard:
        # A scan or a masked text layer still names the standard in the file name (grade stays B;
        # the quote gate in the collector keeps this honest: only dates/labels come from the name).
        for label, pat in STANDARD_FILENAME_PATTERNS:
            if pat.search(file_name or ""):
                doc.standard = label
                doc.date_source = "file_name" if doc.date_source == "text" else doc.date_source
                break
    num = re.search(r"\b(?:certificate|certificato|n[.\u00ba:])\s*n[.\u00ba]?\s*([A-Z0-9][A-Z0-9\-/]{3,})", text or "", re.I)
    if not num:
        num = re.search(r"\bno\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{3,})", text or "", re.I)
    if num and re.search(r"\d", num.group(1)):
        doc.number = num.group(1).strip(".,;")
    for issuer, pat in ISSUER_HINTS:
        if pat.search(text or ""):
            doc.issuer = issuer
            break
    head = "\n".join((text or "").splitlines()[:60])   # certificates carry their facts on page 1
    m = EXPIRY_RE.search(head)
    if m:
        doc.expires_on = parse_date(m.group(1))
    m = ISSUE_RE.search(head)
    if m:
        doc.issued_on = parse_date(m.group(1))
    if not doc.expires_on:
        dates = _dates_from_filename(file_name or url)
        if dates:
            doc.expires_on = max(dates)
            doc.date_source = "file_name"
    doc.year = _first_year(head) or doc.year
    return doc


def extract_manual(text: str, file_name: str = "", url: str = "") -> ParsedDocument:
    """The family code and title of an operating/maintenance manual."""
    doc = ParsedDocument(kind="manual", title=(file_name or url.rsplit("/", 1)[-1]))
    head = "\n".join((text or "").splitlines()[:25])
    m = re.search(r"\b(?:doc(?:ument)?(?:\s*number)?|cod(?:ice)?)\s*[:#]?\s*([A-Z]{2,8}[-_][A-Z0-9\-]+)", head, re.I)
    if not m:
        m = re.search(r"\b([A-Z]{2,8}[-_][A-Z]{2,6}[-_]?\d{2,3}(?:\s*\(\d+\))?)\b", head)
    if m:
        doc.family_code = m.group(1).strip()
    title_m = re.search(r"(operating and maintenance manual|manuale d.{0,2}uso e manutenzione)\s*\n(.{3,80})", head, re.I)
    if title_m:
        doc.title = _clean(title_m.group(2))
    return doc


FIGURE_RES = [
    ("revenue", re.compile(r"(?:revenue|ricavi|fatturato)[^\n]{0,60}?([0-9][\d\.,]{2,12}\s*(?:m(?:illion)?|mln|k)?\s*(?:\u20ac|eur|euro)?)", re.I)),
    ("employees", re.compile(r"(?:employees|dipendenti|headcount|staff)[^\n]{0,40}?(\d{2,5})", re.I)),
    ("ebitda", re.compile(r"ebitda[^\n]{0,60}?([0-9][\d\.,]{2,12})", re.I)),
]


def extract_report(text: str, file_name: str = "") -> ParsedDocument:
    """The year and headline figures of a report, from its first pages only."""
    doc = ParsedDocument(kind="report", title=file_name)
    head = "\n".join((text or "").splitlines()[:120])
    years = [int(y) for y in YEAR_RE.findall(head)]
    doc.year = years[0] if years else None
    for name, pat in FIGURE_RES:
        m = pat.search(head)
        if m:
            doc.figures[name] = _clean(m.group(1))
    return doc


def _first_year(text: str) -> Optional[int]:
    m = YEAR_RE.search(text or "")
    return int(m.group(1)) if m else None


def select_pages(text: str, max_chars: int = 6000) -> str:
    """A bounded excerpt that keeps the fact-bearing pages: all of a short document; otherwise the
    first page plus the pages where certificate/report keywords occur. Never the whole 200-page PDF."""
    if len(text or "") <= max_chars:
        return text or ""
    pages = (text or "").split("\f") if "\f" in text else [text]
    if len(pages) == 1:
        return text[:max_chars]
    keep = pages[0]
    needle = re.compile(r"certif|scadenza|expiry|expire|iso\s*\d{4}|module|revenue|ricavi|bilancio|dipendenti", re.I)
    for page in pages[1:]:
        if len(keep) >= max_chars:
            break
        if needle.search(page):
            keep += "\f" + page[:max_chars - len(keep)]
    return keep[:max_chars]


def is_scanned(text: str) -> bool:
    """A PDF whose text layer carries nothing usable: honest 'scanned, not read', never a guess."""
    return len((text or "").strip()) < 20
