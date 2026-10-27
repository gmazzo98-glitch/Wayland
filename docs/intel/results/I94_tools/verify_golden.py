"""verify_golden.py WORKTREE [file ...] [--fresh]

The acceptance checker for the repaired golden set. For every fact of every golden file (the six I03 companies by default):
  Q  grounding : each `quote` (str or list) is found, verbatim after normalisation, in the text or raw HTML of one of the
                 fact's http sources (fetched now, cached) or, for sources named 'AIDA ...', in the live-DB text of the company
                 (read-only SELECTs, same chunks intel_gap.py scores against). A fact without a quote fails.
  S  self-test : the fact's own match patterns hit its own claim+quote (a pattern that cannot match the evidence is dead).
  H  hostile   : the match patterns do NOT hit (1) a generic machinery-company blurb, (2) the claims+quotes of every OTHER golden
                 company. A hit means the pattern is trivially true or cross-contaminated.
  L  lint      : no pattern is a bare year, a bare number of fewer than 5 digits, or a bare alphabetic token shorter than 6 letters
                 without a word boundary.
Exit code 1 when any check fails.
"""
import glob
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
WT = Path(args[0])
names = args[1:]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(WT)
sys.path.insert(0, str(WT / "scripts"))
sys.path.insert(0, str(WT))
import intel_gap as g  # noqa: E402

try:
    from bs4 import BeautifulSoup
except Exception as exc:  # pragma: no cover
    raise SystemExit("bs4 needed") from exc

CACHE = Path(__file__).with_name("grabcache")
CACHE.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (compatible; ViennaGoldenAudit/1.0; +research)"
_last = {}


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(re.findall(r"\w+", s))


def fetch(url):
    key = CACHE / (hashlib.md5(url.encode()).hexdigest() + ".json")
    if key.exists():
        rec = json.loads(key.read_text(encoding="utf-8"))
        return rec
    host = urllib.parse.urlparse(url).netloc
    wait = 1.0 - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    rec = {"url": url}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it,en;q=0.8,de;q=0.6"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(6_000_000)
            rec.update(status=r.status, final=r.geturl(), ctype=r.headers.get("content-type", ""))
            if "pdf" in rec["ctype"].lower():
                try:
                    import io

                    from pypdf import PdfReader

                    rd = PdfReader(io.BytesIO(body))
                    rec["html"] = "\n".join((pg.extract_text() or "") for pg in rd.pages[:20])
                except Exception as e:  # noqa: BLE001
                    rec["html"] = f"[pdf unreadable {e}]"
            else:
                enc = r.headers.get_content_charset() or "utf-8"
                rec["html"] = body.decode(enc, errors="replace")
    except urllib.error.HTTPError as e:
        rec.update(status=e.code, error=f"HTTP {e.code}", html="")
    except Exception as e:  # noqa: BLE001
        rec.update(status=0, error=f"{type(e).__name__}: {e}"[:200], html="")
    key.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


_page_cache = {}


def page_norms(url):
    if url in _page_cache:
        return _page_cache[url]
    rec = fetch(url)
    html = rec.get("html", "")
    if rec.get("status") != 200 or not html:
        res = (None, None, rec)
    else:
        if "pdf" in rec.get("ctype", "").lower():
            vis = html
        else:
            soup = BeautifulSoup(html, "html.parser")
            for t in soup(["script", "style", "noscript"]):
                t.decompose()
            vis = soup.get_text(" ")
        res = (norm(vis), norm(html), rec)
    _page_cache[url] = res
    return res


GENERIC = """
Leading manufacturer of industrial machinery and automation systems. Founded in 2007, the company has more than 15 years of
experience. Our quality management system is certified ISO 9001 and ISO 14001. We offer a full range of services: design,
service, spare parts, installation, assistance and customer support. Our brochure and datasheet are available for download.
Technical data, certificates and the company profile are published in the downloads section. The company is a subsidiary of a
group and is controlled by its parent. The board of directors (consiglio di amministrazione) and the managing director
(amministratore delegato) manage the company; the engineering department (ingegneria) designs the machines. Quality, service
and innovation. We exhibit at the main trade fairs in Milan, Dusseldorf and Hannover and are members of the national industry
association and of the chamber of commerce. Registered office: Via Roma 1, Milano (MI), Italy. Share capital EUR 12,000 fully
paid up. Phone +39 02 1234567, fax, e-mail, certified e-mail (PEC). Italy Germany USA China India. Family owned. Founded in
1950, 1965, 1985, 2002, 2010. Revenue 20 million euro, 100 employees, EBIT. Website built with WordPress, Apache, PHP, Google Analytics.
Patents, trademarks, standards EN, UNI, TUV, ATEX, PED, ASME, CE marking. Worldwide customers, partners, distributors.
"""

# the company text the tool holds (read-only), built once per company lazily
_db_cache = {}


def db_norm(company_id):
    if company_id in _db_cache:
        return _db_cache[company_id]
    from database import get_db_session

    db = get_db_session()
    chunks = g.chunks_from_session(db, company_id)
    # chunks are lower-cased dumps; norm() them as one text
    res = norm("\n".join(c[2] for c in chunks))
    _db_cache[company_id] = res
    return res


_roster = []


def roster_norm():
    """Legal name, VAT, province, headcount, website of every roster company (read-only)."""
    if not _roster:
        from database import get_db_session
        from models import Company

        db = get_db_session()
        rows = [f"{c.legal_name} {c.registration_number} {c.province} {c.headcount} {c.website_url}" for c in db.query(Company).all()]
        _roster.append(norm("\n".join(rows)))
    return _roster[0]


def lint(pat):
    p = pat.strip()
    if re.fullmatch(r"(19|20)\d\d", p):
        return "bare year"
    if re.fullmatch(r"\d{1,4}", p):
        return "bare short number"
    core = re.sub(r"\\b", "", p)
    if re.fullmatch(r"[a-zA-Z]{1,5}", core) and "\\b" not in p:
        return "short alphabetic token without word boundary"
    if len(re.sub(r"[^a-zA-Z0-9]", "", p)) < 4:
        return "too short"
    return None


def hits(match, text):
    all_p = [re.compile(p, re.I | re.S) for p in match.get("all", [])]
    any_p = [re.compile(p, re.I | re.S) for p in match.get("any", [])]
    if all_p:
        return all(p.search(text) for p in all_p)
    return any(p.search(text) for p in any_p)


files = names or [Path(p).name for p in sorted(glob.glob(str(WT / "docs/intel/golden/*.json")))]
all_files = {Path(p).name: g.load_golden(Path(p)) for p in sorted(glob.glob(str(WT / "docs/intel/golden/*.json")))}
fails = 0
for fname in files:
    d = all_files[fname]
    cid = d["subject"].get("company_id")
    others = "\n".join(
        "\n".join(f["quote"] if isinstance(f.get("quote"), list) else [f.get("quote") or ""])
        for n, o in all_files.items() if n != fname for f in o["facts"]
    )
    print(f"=== {fname}  ({len(d['facts'])} facts)")
    cats = set()
    nf = d.get("not_found") or []
    for f in d["facts"]:
        cats.add(f["category"])
        probs = []
        quotes = f.get("quote")
        quotes = [quotes] if isinstance(quotes, str) else (quotes or [])
        if not quotes:
            probs.append("Q:no quote")
        for q in quotes:
            nq = norm(q)
            ok = False
            tried = []
            for s in f["sources"]:
                if s.lower().startswith("aida"):
                    if cid and nq in db_norm(cid):
                        ok = True
                        break
                    tried.append("aida-miss")
                elif s.lower().startswith("roster"):
                    if nq in roster_norm():
                        ok = True
                        break
                    tried.append("roster-miss")
                elif s.startswith("http"):
                    vis, raw, rec = page_norms(s)
                    if vis is None:
                        tried.append(f"dead({rec.get('status')})")
                        continue
                    if nq in vis or nq in raw:
                        ok = True
                        break
                    tried.append("miss")
            if not ok:
                probs.append(f"Q:quote not found [{', '.join(tried)}]: {q[:60]!r}")
        evidence = f["claim"] + "\n" + "\n".join(quotes)
        if not hits(f["match"], evidence.lower()):
            probs.append("S:own pattern does not hit own claim+quote")
        if hits(f["match"], GENERIC.lower()):
            probs.append("H:hits generic blurb")
        if hits(f["match"], others.lower()):
            probs.append("H:hits other companies' claims/quotes")
        for pat in list(f["match"].get("any", [])) + list(f["match"].get("all", [])):
            lp = lint(pat)
            if lp:
                probs.append(f"L:{pat!r} {lp}")
        if probs:
            fails += 1
            print(f"  FAIL {f['id']}: " + " | ".join(probs))
    print(f"  categories: {len(cats)} {sorted(cats)}; not_found entries: {len(nf)}; split={d.get('split')}")
    if len(d["facts"]) < 30 or len(cats) < 8:
        fails += 1
        print("  FAIL: fewer than 30 facts or 8 categories")
    if not nf:
        fails += 1
        print("  FAIL: no not_found list")
print("TOTAL FAILING CHECKS:", fails)
sys.exit(1 if fails else 0)
