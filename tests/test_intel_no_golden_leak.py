"""
Rule 6 of docs/INTEL_ROADMAP.md, enforced by code instead of by hope: the collection code must not know the golden
companies. A collector that contains ASCO's owners, its VAT, its pages or its brand "works" on the benchmark and
fails on the 950 companies nobody wrote a special case for, and it contaminates the hold-out set.

The scan reads every golden file (docs/intel/golden/*.json), takes each subject's brand word (first word of the legal
name), VAT and website domain, adds a few entity names that come with the ASCO story, and looks for them as whole
tokens in `intel/**/*.py` and `scrapers/deep_profile*.py`. Tests, docs and golden files may name them freely.

ALLOWED_UNTIL_FIXED is the baseline of known offenders, each tied to the ticket that removes it:
  * a NEW offender fails this test at once;
  * an entry whose file no longer offends must be deleted (the list only shrinks);
  * an entry whose ticket is DONE while the file still offends fails ("done is not done").
When your ticket removes the hard-coding, delete your line from ALLOWED_UNTIL_FIXED in the same commit.
"""

import glob
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set

ROOT = Path(__file__).resolve().parent.parent

# Entity names that belong to the golden stories but are not in a subject block.
EXTRA_TOKENS = {"mott", "idex", "binasco", "red-fish", "redfish"}

ALLOWED_UNTIL_FIXED: Dict[str, str] = {
    "intel/events.py": "I96",
    "intel/collectors/events.py": "I96",
    "intel/collectors/site_headers.py": "I18",   # a docstring example only; reword it when you extend the file
}


def golden_tokens() -> Set[str]:
    tokens: Set[str] = set(EXTRA_TOKENS)
    for path in glob.glob(str(ROOT / "docs" / "intel" / "golden" / "*.json")):
        subject = json.loads(Path(path).read_text(encoding="utf-8")).get("subject", {})
        words = re.sub(r"[^a-z0-9 ]", " ", str(subject.get("legal_name", "")).lower()).split()
        if words:
            tokens.add(words[0])
        if subject.get("vat"):
            tokens.add(str(subject["vat"]).lower())
        site = str(subject.get("website") or "").lower()
        site = re.sub(r"^https?://", "", site).replace("www.", "").split("/")[0]
        if site:
            tokens.add(site)
        for extra in subject.get("never_hardcode", []):
            tokens.add(str(extra).lower())
    return {t for t in tokens if len(t) >= 4}


def find_tokens(text: str, tokens: Iterable[str]) -> Dict[str, List[int]]:
    """{token: [line numbers]} for whole-token, case-insensitive matches (a token inside another word is not a hit)."""
    ordered = sorted(tokens, key=len, reverse=True)
    pat = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in ordered) + r")(?![a-z0-9])", re.I)
    hits: Dict[str, List[int]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        for m in pat.finditer(line):
            hits.setdefault(m.group(1).lower(), []).append(number)
    return hits


def scanned_files() -> List[Path]:
    files = [Path(p) for p in glob.glob(str(ROOT / "intel" / "**" / "*.py"), recursive=True)]
    files += [Path(p) for p in glob.glob(str(ROOT / "scrapers" / "deep_profile*.py"))]
    return sorted(files)


def offenders() -> Dict[str, Dict[str, List[int]]]:
    tokens = golden_tokens()
    out: Dict[str, Dict[str, List[int]]] = {}
    for f in scanned_files():
        hits = find_tokens(f.read_text(encoding="utf-8", errors="replace"), tokens)
        if hits:
            out[f.relative_to(ROOT).as_posix()] = hits
    return out


def test_scanner_matches_whole_tokens_only():
    toks = {"asco", "mott"}
    assert find_tokens("the ASCO Valve brand", toks) == {"asco": [1]}
    assert find_tokens("tasco, ascot, mottled", toks) == {}
    assert find_tokens("a\nmott corporation\n# MOTT", toks) == {"mott": [2, 3]}


def test_golden_tokens_cover_every_golden_subject():
    toks = golden_tokens()
    for path in glob.glob(str(ROOT / "docs" / "intel" / "golden" / "*.json")):
        vat = json.loads(Path(path).read_text(encoding="utf-8"))["subject"].get("vat")
        if vat:
            assert str(vat).lower() in toks, path
    assert {"asco", "mott", "idex"} <= toks


def test_collection_code_does_not_hard_code_golden_companies():
    found = offenders()
    unexpected = {f: h for f, h in found.items() if f not in ALLOWED_UNTIL_FIXED}
    assert not unexpected, (
        "Collection code names a golden company (roadmap rule 6: never hard-code the benchmark). "
        + "; ".join(f"{f}: {', '.join(f'{t} x{len(n)}' for t, n in h.items())}" for f, h in unexpected.items()))


def test_the_baseline_only_shrinks_and_tickets_are_not_called_done_early():
    found = offenders()
    board = json.loads((ROOT / "docs" / "intel" / "board.json").read_text(encoding="utf-8"))["tickets"]
    for f, ticket in ALLOWED_UNTIL_FIXED.items():
        assert ticket in board, f"{f}: ticket {ticket} is not on the board"
        if f not in found:
            raise AssertionError(f"{f} no longer names a golden company: delete it from ALLOWED_UNTIL_FIXED")
        assert board[ticket]["status"] != "DONE", (
            f"{ticket} is DONE but {f} still names a golden company ({', '.join(found[f])}); finish the removal "
            f"and delete the line from ALLOWED_UNTIL_FIXED")
