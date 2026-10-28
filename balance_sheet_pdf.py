"""Annual financial statements (PDF) read into dated, source-referenced lines.

Reading order: the PDF text layer first, OCR of image-only pages through a vision
model second. Only the primary statements are parsed (balance sheet assets and
liabilities, income statement, statement of comprehensive income); every page's
text stays available as evidence. Each line keeps its label, item number, page and
printed value per actual period-end date, and gets a line id that does not depend
on the year, so a reviewed mapping can be reused on next year's statement.

Mapping suggestions come from explicit Italian/English rules first. A language
model may add further suggestions for the lines the rules leave open; those are
never applied without the operator.
"""
import hashlib
import io
import json
import re
import unicodedata
from datetime import date, datetime

FORMAT = "balance_sheet_pdf_v1"
DATASET_PREFIX = "Balance Sheet PDF "
MONETARY_BASES = (
    "revenue", "production_value", "ebit", "ebitda", "gross_margin", "production_costs", "personnel_costs",
    "materials", "cash", "total_debt", "material_capex", "immaterial_capex", "total_assets", "intangibles",
    "equity", "net_income",
)
OCR_MIN_CHARS = 60

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}


def key(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# ------------------------------------------------------------------------------------------------ page text

def render_page_png(content, index, scale=2.0):
    import pypdfium2
    pdf = pypdfium2.PdfDocument(content)
    try:
        buffer = io.BytesIO()
        pdf[index].render(scale=scale).to_pil().save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        pdf.close()


def extract_pages(content, ocr=None):
    """[{page, text, method}] - text layer, or OCR (a callable taking PNG bytes) for pages without one."""
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            method = "text"
            if len(text) < OCR_MIN_CHARS:
                if ocr is None:
                    method = "empty"
                else:
                    text, method = (ocr(render_page_png(content, number - 1)) or "").strip(), "ocr"
            pages.append(dict(page=number, text=text, method=method))
    return pages


def _llm_endpoints(kind):
    import config
    if kind == "vision":
        get = config.get_config_var
        out = [(get("CRAWLER_VISION_LLM_API_KEY"), get("CRAWLER_VISION_LLM_BASE_URL", "https://api.groq.com/openai/v1"),
                get("CRAWLER_VISION_LLM_MODEL", "llama-3.2-11b-vision-preview")),
               (get("CRAWLER_VISION_LLM_FALLBACK_API_KEY"), get("CRAWLER_VISION_LLM_FALLBACK_BASE_URL"),
                get("CRAWLER_VISION_LLM_FALLBACK_MODEL"))]
        out += [(e.get("api_key"), e.get("base_url"), e.get("model")) for e in config._numbered_llm_fallbacks("CRAWLER_VISION_LLM")]
    else:
        s = config.crawler_llm_settings()
        out = [(s.get("api_key"), s.get("base_url"), s.get("model")),
               (s.get("fallback_api_key"), s.get("fallback_base_url"), s.get("fallback_model"))]
        out += [(e.get("api_key"), e.get("base_url"), e.get("model")) for e in s.get("extra_fallbacks") or []]
    return [(k, b.rstrip("/"), m) for k, b, m in out if k and b and m]


def llm_available(kind="text"):
    try:
        return bool(_llm_endpoints(kind))
    except Exception:
        return False


def chat(messages, kind="text", max_tokens=4000, timeout=90):
    """First answer from the configured OpenAI-compatible providers; raises when none answers."""
    import requests
    errors = []
    for api_key, base, model in _llm_endpoints(kind):
        try:
            response = requests.post(f"{base}/chat/completions", timeout=timeout,
                                     headers={"Authorization": f"Bearer {api_key}"},
                                     json=dict(model=model, messages=messages, temperature=0, max_tokens=max_tokens))
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            errors.append(f"{model}: HTTP {response.status_code}")
        except Exception as exc:  # next provider
            errors.append(f"{model}: {exc}")
    raise RuntimeError("No language model answered. " + ("; ".join(errors) if errors else "No provider key is configured."))


def vision_ocr(png_bytes):
    import base64
    image = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
    return chat([dict(role="user", content=[
        dict(type="text", text="Transcribe this page of a company's financial statements exactly as printed, one table row per "
                               "line (label, then every number). Keep item numbers, brackets and minus signs. Output text only."),
        dict(type="image_url", image_url=dict(url=image))])], kind="vision")


# ------------------------------------------------------------------------------------------------ numbers and dates

_NUM = re.compile(r"^\(?-?\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?\)?$|^\(?-?\d+(?:[.,]\d+)?\)?$|^-$")


def _is_number_token(token):
    return bool(_NUM.match(token))


def parse_number(token, decimal=None):
    """Printed statement number; '-' is the nil marker. Brackets (even unclosed ones) are negative.

    Dots and commas are read per number: a group of exactly three digits after a lone separator is a thousands
    group (statements print whole euros, and English translations keep Italian dots), otherwise it is a decimal."""
    token = token.strip()
    if token == "-":
        return 0.0
    negative = token.startswith("(") or token.startswith("-")
    digits = token.strip("()-")
    if "." in digits and "," in digits:
        last = max(digits.rfind("."), digits.rfind(","))
        digits = digits[:last].replace(".", "").replace(",", "") + "." + digits[last + 1:]
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", digits):
        digits = re.sub(r"[.,]", "", digits)
    else:
        digits = digits.replace(",", ".")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def _language(text):
    english = len(re.findall(r"\b(?:total|assets|liabilities|balance sheet|income statement|financial statements|the)\b", text, re.I))
    italian = len(re.findall(r"\b(?:totale|attivo|passivo|conto economico|stato patrimoniale|bilancio|societ)", text, re.I))
    return "en" if english > italian else "it"


def parse_date(text, language="it"):
    text = text.strip()
    match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text)
    if match:
        a, b, year = int(match[1]), int(match[2]), int(match[3])
        first_is_month = a <= 12 and (b > 12 or (b <= 12 and language == "en"))
        month, day = (a, b) if first_is_month else (b, a)
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    match = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", text)
    if match and match[1].casefold() in _MONTHS:
        return date(int(match[3]), _MONTHS[match[1].casefold()], int(match[2])).isoformat()
    return None


def _prior_year(iso):
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28).isoformat()


# ------------------------------------------------------------------------------------------------ statements

_HEADINGS = (
    ("assets", re.compile(r"^(?:stato patrimoniale\s+attivo|balance sheet\s+assets?|assets)\b", re.I)),
    ("liabilities", re.compile(r"^(?:stato patrimoniale\s+passivo|balance sheet\s+liabilities|liabilities and (?:net )?equity)\b", re.I)),
    ("income", re.compile(r"^(?:conto economico|income statement|profit and loss(?: account)?)\b", re.I)),
    ("comprehensive", re.compile(r"^(?:prospetto della redditivit|overview of overall profitability|statement of comprehensive income)", re.I)),
)
_OTHER_HEADING = re.compile(r"^(?:prospetto delle variazioni|statement of changes|rendiconto finanziario|cash flow statement|"
                            r"nota integrativa|notes to the|explanatory notes|parte [a-z]\b|criteri di valutazione)", re.I)
_NOISE = re.compile(r"^(?:pag\.?\s*\d+|page\s*\d+|il consiglio|the board|l.amministratore|\(?[A-Z ]+\)$|subscribe to deepl|visit www)", re.I)
_ITEM = re.compile(r"^\s*(\d{1,3})\.\s+")
_LETTER_ITEM = re.compile(r"^\s*([a-z])\)\s+", re.I)

# Positions in the Bank of Italy layout, used only to restore a label the PDF printed as scattered letters.
_STANDARD_ITEMS = {
    "260": "UTILE (PERDITA) DELL'ATTIVITA' CORRENTE AL LORDO DELLE IMPOSTE",
    "280": "UTILE (PERDITA) DELL'ATTIVITA' CORRENTE AL NETTO DELLE IMPOSTE",
}


def _clean_lines(text):
    """Drop rotated/scattered one-letter fragments but keep the digits they spell as an item number."""
    raw = [line.strip() for line in text.splitlines() if line.strip()]
    out, i = [], 0
    while i < len(raw):
        j = i
        while j < len(raw) and len(raw[j].replace(" ", "")) <= 3 and not _is_number_token(raw[j]):
            j += 1
        if j - i >= 4:
            digits = "".join(c for line in raw[i:j] for c in line if c.isdigit())
            tail = raw[j] if j < len(raw) and not _is_number_token(raw[j].split()[-1]) else ""
            out.append(dict(text=(f"{digits}. " if digits else "") + tail, garbled=True))
            i = j + (1 if tail else 0)
        else:
            out.append(dict(text=raw[i], garbled=False))
            i += 1
    return out


def _split_numbers(line, ncols):
    tokens = line.split()
    nums = []
    while tokens and len(nums) < ncols and _is_number_token(tokens[-1]):
        nums.insert(0, tokens.pop())
    return " ".join(tokens), nums


def parse_pages(pages, filename="statement.pdf", sha256=""):
    """Pure parser over [{page, text, method}]."""
    full = "\n".join(p["text"] for p in pages)
    language = _language(full)
    decimal = None
    report = dict(format=FORMAT, filename=filename, sha256=sha256, language=language, company={}, period_end=None,
                  unit=None, unit_text=None, dates=[], pages=[dict(page=p["page"], method=p["method"], chars=len(p["text"])) for p in pages],
                  page_text={str(p["page"]): p["text"] for p in pages}, lines=[], checks=[], warnings=[])
    warnings = report["warnings"]

    fiscal = re.search(r"(?:codice fiscale|tax code|fiscal code)\s*(?:e n\.? iscrizione registro imprese)?\s*(\d{11}|[A-Z0-9]{16})\b", full, re.I) \
        or re.search(r"(?:p\.?\s?i\.?|partita iva|vat(?: number)?)\s*:?\s*(\d{11})\b", full, re.I)
    if fiscal:
        from utils import normalize_registration_nr
        report["company"]["registration_number"] = normalize_registration_nr(fiscal[1], "Italy")
    first = next((line.strip() for line in (pages[0]["text"] if pages else "").splitlines()
                  if re.search(r"\b(?:s\.?p\.?a\.?|s\.?r\.?l\.?|s\.?a\.?s\.?|s\.?n\.?c\.?|s\.?c\.?a\.?r\.?l\.?|ltd|gmbh|ag|spa|srl)\b", line, re.I)), None)
    if first:
        report["company"]["legal_name"] = first.title() if first.isupper() else first
    for pattern in (r"bilancio(?: di esercizio)?\s+al\s+([\d/.\-]+)", r"(?:financial statements|balance sheet)\s+(?:as of|as at|at)\s+([A-Za-z0-9 ,/.\-]+?\d{4})"):
        match = re.search(pattern, full, re.I)
        if match and parse_date(match[1], language):
            report["period_end"] = parse_date(match[1], language)
            break
    if re.search(r"(?:espress\w+|in)\s+(?:migliaia|mille)\s+di\s+euro|in thousands? of euro|\(?€\s?/\s?000\)?|euro/000|amounts? (?:are )?in thousands", full, re.I):
        report["unit"], report["unit_text"] = "thousand EUR", "thousands of euro"
    elif re.search(r"importi (?:presenti )?(?:sono )?espressi in euro|amounts (?:shown )?are in euros?|expressed in (?:units of )?euro|in unit. di euro", full, re.I):
        report["unit"], report["unit_text"] = "EUR", "euro"
    if not report["period_end"]:
        warnings.append("The statement date could not be read; enter the period end to continue.")

    statement, seen, ncols, dates = None, set(), 2, []
    pending, last_wrapped, counters, pending_garbled = [], None, {}, False
    for page in pages:
        for entry in _clean_lines(page["text"]):
            line = entry["text"]
            if not line:
                continue
            head = next((name for name, pattern in _HEADINGS if pattern.search(line)), None)
            if head:
                if head in seen:
                    statement = None
                    continue
                statement = head
                seen.add(head)
                pending, last_wrapped = [], None
                header_dates = [d for d in (parse_date(t, language) for t in re.findall(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}", line)) if d]
                dates = header_dates if len(header_dates) >= 2 else dates
                continue
            if _OTHER_HEADING.search(line):
                statement, pending, last_wrapped = None, [], None
                continue
            if statement is None or _NOISE.match(line):
                continue
            label, nums = _split_numbers(line, ncols)
            if len(nums) == ncols:
                full_label = " ".join(pending + ([label] if label else []))
                wrapped = bool(pending) or not label
                restored_garble, pending, pending_garbled = pending_garbled or entry["garbled"], [], False
                if not full_label:
                    continue
                item_match = _ITEM.match(full_label)
                item = item_match[1] if item_match else None
                restored = False
                if restored_garble and item in _STANDARD_ITEMS:
                    full_label, restored = f"{item}. {_STANDARD_ITEMS[item]}", True
                period_end = report["period_end"]
                columns = dates[:ncols] if len(dates) >= ncols else ([period_end, _prior_year(period_end)] if period_end else [f"column {i + 1}" for i in range(ncols)])
                stem = f"{statement}|{item or key(full_label)}"
                counters[stem] = counters.get(stem, 0) + 1
                row = dict(id=f"{stem}|{counters[stem]}", statement=statement, item=item, label=full_label, page=page["page"],
                           values={d: parse_number(n, decimal) for d, n in zip(columns, nums)},
                           raw=dict(zip(columns, nums)), flags=[])
                if restored:
                    row["flags"].append("label restored from the standard schema (the PDF printed scattered letters)")
                if any(n.startswith("(") and not n.endswith(")") for n in nums):
                    row["flags"].append("closing bracket missing in the PDF; read as negative")
                if any(v is None for v in row["values"].values()):
                    row["flags"].append("a number could not be read")
                report["lines"].append(row)
                last_wrapped = row if wrapped else None
                continue
            if len(nums) == 0 and not entry["garbled"]:
                if last_wrapped is not None and not _ITEM.match(line) and len(line.split()) <= 3:
                    last_wrapped["label"] += " " + line
                    last_wrapped = None
                    continue
                last_wrapped = None
                if _ITEM.match(line) or _LETTER_ITEM.match(line) or pending:
                    pending.append(line)
                elif not re.search(r"\b(?:sede|capitale|iscritt|codice fiscale|partita iva|registro|albo|headquarters|share capital|tax code|"
                                   r"register|vat|rea|codice|code|chamber|financial statements|bilancio al)\b", line, re.I):
                    pending.append(line)
            elif entry["garbled"]:
                pending, pending_garbled = [line], True

    if dates and report["period_end"] is None:
        report["period_end"] = dates[0]
    report["dates"] = sorted({d for row in report["lines"] for d in row["values"]}, reverse=True)
    if not report["lines"]:
        warnings.append("No statement lines were found. If the PDF is a scan, configure the OCR model and read it again.")
    if report["period_end"] and report["dates"] and report["dates"][0] != report["period_end"]:
        warnings.append(f"The first column date {report['dates'][0]} differs from the statement date {report['period_end']}.")
    if any(p["method"] == "ocr" for p in report["pages"]):
        warnings.append("Some pages were read by OCR; check every number against the PDF before importing.")
    if any(p["method"] == "empty" for p in report["pages"]):
        warnings.append(f"{sum(p['method'] == 'empty' for p in report['pages'])} page(s) have no readable text and OCR was not run.")
    if "�" in full:
        warnings.append("The PDF text contains damaged characters (accents). Labels are kept as printed.")
    report["checks"] = consistency_checks(report)
    for check in report["checks"]:
        if not check["ok"]:
            warnings.append(f"Check failed - {check['name']}: {check['detail']}")
    return report


def parse_statement(source, filename=None, ocr=None):
    if isinstance(source, (str, bytes)) and not isinstance(source, bytes):
        from pathlib import Path
        content = Path(source).read_bytes()
        filename = filename or Path(source).name
    elif isinstance(source, bytes):
        content = source
    else:
        source.seek(0)
        content = source.read()
        filename = filename or getattr(source, "name", "statement.pdf")
    return parse_pages(extract_pages(content, ocr=ocr), filename or "statement.pdf", hashlib.sha256(content).hexdigest())


def _find(report, statement, pattern):
    for row in report["lines"]:
        if row["statement"] == statement and re.search(pattern, key(row["label"])):
            return row
    return None


def consistency_checks(report):
    """Totals that must agree if the lines were read correctly."""
    checks = []
    assets = _find(report, "assets", r"^(?:totale attivo|total assets)$")
    liabilities = _find(report, "liabilities", r"^(?:totale passivo e patrimonio netto|total liabilities and (?:net )?equity)")
    if assets and liabilities:
        for d in assets["values"]:
            a, b = assets["values"].get(d), liabilities["values"].get(d)
            checks.append(dict(name=f"Assets equal liabilities and equity ({d})", ok=a is not None and a == b, detail=f"{a} vs {b}"))
    income_net = _find(report, "income", r"(?:utile|profit) \w*\s*\(?\w*\)?\s*(?:d esercizio|dell esercizio|for the (?:financial )?year)")
    balance_net = _find(report, "liabilities", r"(?:utile|profit) \w*\s*\(?\w*\)?\s*(?:d esercizio|dell esercizio|for the (?:financial )?year)")
    if income_net and balance_net:
        for d in income_net["values"]:
            a, b = income_net["values"].get(d), balance_net["values"].get(d)
            checks.append(dict(name=f"Net result agrees between income statement and balance sheet ({d})", ok=a == b, detail=f"{a} vs {b}"))
    before = next((r for r in report["lines"] if r["statement"] == "income" and r["item"] == "260"), None)
    tax = next((r for r in report["lines"] if r["statement"] == "income" and r["item"] == "270"), None)
    after = next((r for r in report["lines"] if r["statement"] == "income" and r["item"] == "280"), None)
    if before and tax and after:
        for d in before["values"]:
            vals = [before["values"].get(d), tax["values"].get(d), after["values"].get(d)]
            if None not in vals:
                checks.append(dict(name=f"Result before tax plus tax equals result after tax ({d})",
                                   ok=abs(vals[0] + vals[1] - vals[2]) < 1, detail=f"{vals[0]} + {vals[1]} vs {vals[2]}"))
    return checks


# ------------------------------------------------------------------------------------------------ mapping rules

# (statement filter, label pattern on the folded label, target base, confidence, note)
_RULES = (
    ("assets", r"^(?:totale attivo|total assets)$", "total_assets", 0.98, ""),
    ("liabilities", r"^(?:a )?(?:totale patrimonio netto|total (?:share|stock)holders s? equity|total net equity|total equity)$", "equity", 0.95, ""),
    ("income", r"(?:utile|profit) \w*\s*(?:perdita|loss)?\s*(?:d esercizio|dell esercizio|for the (?:financial )?year|of the year)$", "net_income", 0.95, ""),
    ("income", r"^(?:\d+ )?(?:utile|profit|net profit|net income)\s*(?:perdita|loss)?\s*(?:dell esercizio|d esercizio|for the year)?$", "net_income", 0.9, ""),
    ("income", r"^(?:a 1 |1 )?ricavi (?:delle )?vendite(?: e (?:delle )?prestazioni)?$|^(?:net )?(?:sales|revenues?)$|^revenue from sales", "revenue", 0.9, ""),
    ("income", r"^\d+ (?:commissioni attive|fee and commission income)$", "revenue", 0.6,
     "Financial intermediary: fee and commission income is the closest revenue measure. Net commissions and the intermediation margin stay separate lines. Confirm before use."),
    ("income", r"^(?:a )?(?:totale )?valore della produzione$|^total (?:value of )?production$", "production_value", 0.9, ""),
    ("income", r"^(?:b )?(?:totale )?costi della produzione$|^total production costs$", "production_costs", 0.9, ""),
    ("income", r"^(?:differenza tra valore e costi della produzione|risultato operativo|operating (?:result|profit|income))(?: a b)?$", "ebit", 0.85, ""),
    ("income", r"^(?:ebitda|margine operativo lordo|mol)$", "ebitda", 0.9, ""),
    ("income", r"per materie prime sussidiarie di consumo e di merci$|^(?:b 6 |6 )?materie prime|raw materials", "materials", 0.8, ""),
    ("income", r"^(?:b 9 |9 )?(?:totale )?(?:costi )?(?:del personale|per il personale)$|^personnel (?:costs|expenses)$|^totale costi del personale$", "personnel_costs", 0.85, ""),
    ("income", r"^[a-z] (?:spese per il personale|personnel expenses)$", "personnel_costs", 0.85, ""),
    ("assets", r"^(?:\d+ )?(?:cassa e )?disponibilit\w* liquide$|^(?:iv )?totale disponibilit\w* liquide$|^(?:\d+ )?cash and cash equivalents$", "cash", 0.9, ""),
    ("liabilities", r"^(?:d )?totale debiti$|^total (?:debts?|payables)$", "total_debt", 0.9,
     "AIDA's own total debt line is total liabilities; check that this line means the same."),
    ("assets", r"^(?:b i )?totale immobilizzazioni immateriali$|^(?:\d+ )?(?:attivit\w* immateriali|intangible assets)$|^total intangible assets$", "intangibles", 0.85, ""),
)


def suggest_mapping(report, profile=None):
    """{line id: dict(target, confidence, origin, note)} - rules, then a saved profile on top."""
    profile = profile or {}
    suggestions, taken = {}, set()
    for row in report["lines"]:
        label = key(row["label"])
        picked = None
        for statement, pattern, base, confidence, note in _RULES:
            if row["statement"] == statement and re.search(pattern, label):
                picked = dict(target=f"base:{base}", confidence=confidence, origin="rule", note=note)
                break
        # One printed figure should feed one fact: the income statement wins over the balance-sheet copy of the result.
        if picked and picked["target"] in taken and picked["target"] == "base:net_income":
            picked = dict(target="", confidence=0.0, origin="rule", note="Same figure as the income statement result.")
        if picked and picked["target"]:
            taken.add(picked["target"])
        suggestions[row["id"]] = picked or dict(target="", confidence=0.0, origin="none", note="")
    for line_id, target in profile.items():
        if line_id in suggestions:
            suggestions[line_id] = dict(target=target or "", confidence=1.0, origin="profile", note="Saved mapping profile")
    return suggestions


_AI_PROMPT = (
    "You map lines of a company's annual financial statement to a fixed list of facts. Answer with JSON only: "
    '[{"id": "<line id>", "target": "<fact or none>", "confidence": 0-1, "rationale": "<one sentence>"}]. '
    "Allowed facts: " + ", ".join(MONETARY_BASES) + ". "
    "Use none unless the line is the same economic quantity. Never map a financial intermediary's margins or commissions to "
    "ebitda, gross_margin or production_costs. Totals only, never a sub-line, unless no total exists. "
    "Revenue is net sales; total_debt is total payables."
)


def ai_suggestions(report, mapping, chat_fn=None):
    """Extra suggestions for lines the rules left open. Returns {line id: dict(target, confidence, origin, note)}."""
    chat_fn = chat_fn or chat
    open_lines = [dict(id=r["id"], statement=r["statement"], item=r["item"], label=r["label"],
                       values=r["values"]) for r in report["lines"]
                  if not mapping.get(r["id"], {}).get("target") and any(v for v in r["values"].values())]
    if not open_lines:
        return {}
    answer = chat_fn([dict(role="system", content=_AI_PROMPT),
                      dict(role="user", content=json.dumps(open_lines[:120], ensure_ascii=False))])
    match = re.search(r"\[.*\]", answer, re.S)
    if not match:
        raise ValueError("The language model did not return a list of mappings.")
    allowed, ids = set(MONETARY_BASES), {r["id"] for r in open_lines}
    out = {}
    for item in json.loads(match[0]):
        if item.get("id") in ids and item.get("target") in allowed:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            out[item["id"]] = dict(target=f"base:{item['target']}", confidence=confidence, origin="ai",
                                   note=str(item.get("rationale") or "")[:300])
    return out
