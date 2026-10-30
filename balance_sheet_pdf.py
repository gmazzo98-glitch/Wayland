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
PDF_BASES = MONETARY_BASES + ("employees",)  # facts a statement line can feed; headcount is not converted
OCR_MIN_CHARS = 60

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTHS_IT = {m: i for i, m in enumerate(
    "gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split(), 1)}


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
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("Reading PDFs needs the 'pdfplumber' package: run  pip install -r requirements.txt  "
                           "(and restart the app).") from exc
    pages = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            method, error = "text", None
            if len(text) < OCR_MIN_CHARS:
                if ocr is None:
                    method = "empty"
                else:
                    try:
                        text, method = (ocr(render_page_png(content, number - 1)) or "").strip(), "ocr"
                    except Exception as exc:  # a page the vision model cannot read must not lose the pages that have text
                        text, method, error = "", "empty", str(exc)
            pages.append(dict(page=number, text=text, method=method, **({"error": error} if error else {})))
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
        a = config.assistant_llm_settings()
        out += [(e["key"], e["base"], e["model"]) for e in _assistant_endpoints(a) if re.search(r"gemini|pixtral|vision|gpt-4", e["model"], re.I)]
    else:
        s = config.crawler_llm_settings()
        out = [(s.get("api_key"), s.get("base_url"), s.get("model")),
               (s.get("fallback_api_key"), s.get("fallback_base_url"), s.get("fallback_model"))]
        out += [(e.get("api_key"), e.get("base_url"), e.get("model")) for e in s.get("extra_fallbacks") or []]
        out += [(e["key"], e["base"], e["model"]) for e in _assistant_endpoints(config.assistant_llm_settings())]
    return [(k, b.rstrip("/"), m) for k, b, m in out if k and b and m]


def _assistant_endpoints(a):
    return [dict(key=a.get("api_key"), base=a.get("base_url"), model=a.get("model") or ""),
            dict(key=a.get("fallback_api_key"), base=a.get("fallback_base_url"), model=a.get("fallback_model") or "")]


def llm_available(kind="text"):
    try:
        return bool(_llm_endpoints(kind))
    except Exception:
        return False


def chat(messages, kind="text", max_tokens=4000, timeout=120, attempts=2):
    """First answer from the configured OpenAI-compatible providers. A rate-limited provider (429) is waited for
    (its Retry-After, capped) before the next provider is tried; raises when none answers."""
    import time
    import requests
    errors = []
    for api_key, base, model in _llm_endpoints(kind):
        for attempt in range(attempts):
            try:
                response = requests.post(f"{base}/chat/completions", timeout=timeout,
                                         headers={"Authorization": f"Bearer {api_key}"},
                                         json=dict(model=model, messages=messages, temperature=0, max_tokens=max_tokens,
                                                   **({"reasoning_effort": "low"} if "gpt-oss" in model else {})))
            except Exception as exc:  # next provider
                errors.append(f"{model}: {exc}")
                break
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            if response.status_code == 429 and attempt < attempts - 1:
                try:
                    wait = float(response.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 2.0 * (attempt + 1)), 20.0))
                continue
            errors.append(f"{model}: HTTP {response.status_code}" + (" (model not found: the name is retired or wrong, update it in the settings)"
                                                                     if response.status_code == 404 else ""))
            break
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
        try:
            return date(int(match[3]), _MONTHS[match[1].casefold()], int(match[2])).isoformat()
        except ValueError:
            return None
    # "31 dicembre 2025", "31 December 2025"
    match = re.search(r"\b(\d{1,2})\s*(?:º|°)?\s+([A-Za-zàèéìòù]+)\.?\s+(\d{4})\b", text)
    if match:
        month = _MONTHS_IT.get(match[2].casefold()) or _MONTHS.get(match[2].casefold())
        if month:
            try:
                return date(int(match[3]), month, int(match[1])).isoformat()
            except ValueError:
                return None
    return None


def _with_year(iso, year):
    """The same month and day in another year (29 February falls back to the 28th)."""
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=year).isoformat()
    except ValueError:
        return d.replace(year=year, day=28).isoformat()


_YEAR_HEADER = re.compile(r"^(?:(?:esercizio|anno|year|fy|al)\s*)?(20\d{2})\s+(?:(?:esercizio|anno|year|fy|al)\s*)?(20\d{2})$", re.I)


def _scan_column_headers(pages, language):
    """What the statement's own column headings say about the years of the figure columns, in printed order:
    ([dates of each dated heading], [years of the first year-only heading])."""
    dated, years = [], []
    for page in pages:
        entries = [e["text"] for e in _clean_lines(page["text"]) if e["text"]]
        for i, line in enumerate(entries):
            if not any(pattern.search(line) for _, pattern in _HEADINGS):
                continue
            found = [d for d in (parse_date(t, language) for t in re.findall(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}", line)) if d]
            if found:
                dated.append(found)
            elif not years:
                for nxt in entries[i + 1:i + 4]:  # the years sit on a line of their own under the heading
                    match = _YEAR_HEADER.match(nxt.strip())
                    if match:
                        years = [int(match[1]), int(match[2])]
                        break
    return dated, years


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
_ITEM = re.compile(r"^\s*(\d{1,3})[.)]\s+")  # "10. " (Bank of Italy layout) and "6) " (OIC layout)
_LETTER_ITEM = re.compile(r"^\s*([a-z])\)\s+", re.I)
# Headings that carry no figures of their own ("B) IMMOBILIZZAZIONI", "IV - Disponibilita liquide", "9) per il personale:"):
# they group the rows below and must not become the first words of the next row's label.
_SECTION = re.compile(r"^(?:[A-Z]\)\s+[A-Z][A-Z0-9'’ ,.\-/&()]*|(?:XII|XI|IX|X|VIII|VII|VI|IV|V|III|II|I)\s*[-–.]\s+\S.*|.*:)$")

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
    for pattern in (r"(?:bilancio(?: di esercizio)?|esercizio(?: sociale)?)\s+(?:chiuso\s+)?al\s+(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}|\d{1,2}\s*(?:º|°)?\s+[A-Za-zàèéìòù]+\.?\s+\d{4})",
                    r"(?:financial statements|balance sheet)\s+(?:as of|as at|at)\s+([A-Za-z0-9 ,/.\-]+?\d{4})"):
        match = re.search(pattern, full, re.I)
        if match and parse_date(match[1], language):
            report["period_end"] = parse_date(match[1], language)
            break
    if re.search(r"(?:espress\w+|in)\s+(?:migliaia|mille)\s+di\s+euro|in thousands? of euro|\(?€\s?/\s?000\)?|euro/000|amounts? (?:are )?in thousands", full, re.I):
        report["unit"], report["unit_text"] = "thousand EUR", "thousands of euro"
    elif re.search(r"importi (?:presenti )?(?:sono )?espressi in euro|amounts (?:shown )?are in euros?|expressed in (?:units of )?euro|in unit. di euro", full, re.I):
        report["unit"], report["unit_text"] = "EUR", "euro"
    ncols = 2  # current and previous year, as Italian statements print them
    dated_headings, header_years = _scan_column_headers(pages, language)
    header_cols = next((d for d in dated_headings if len(d) >= ncols), None)
    fallback_columns = None
    if header_cols:
        # The dates over the figure columns are the direct evidence of which year each number belongs to; a date elsewhere in
        # the text (the cover, an approval date) is not.
        newest = max(header_cols)
        if report["period_end"] and report["period_end"] != newest:
            warnings.append(f"The text names {report['period_end']} but the figure columns are headed {', '.join(header_cols)}; "
                            f"the column headings are used (the statement closes {newest}).")
        report["period_end"], fallback_columns = newest, header_cols[:ncols]
    elif len(header_years) >= ncols:
        if report["period_end"]:
            if int(report["period_end"][:4]) != max(header_years):
                warnings.append(f"The text names {report['period_end']} but the figure columns are headed {header_years[0]} and "
                                f"{header_years[1]}; the column years are used.")
                report["period_end"] = _with_year(report["period_end"], max(header_years))
            fallback_columns = [_with_year(report["period_end"], y) for y in header_years[:ncols]]
        else:
            report["column_years"] = header_years[:ncols]  # months and days unknown: columns stay placeholders until the date is given
    if not report["period_end"]:
        hint = f" The columns are headed {header_years[0]} and {header_years[1]}." if len(header_years) >= ncols else ""
        warnings.append("The statement date could not be read; enter the closing date of the newest column to continue." + hint)

    statement, seen, dates = None, set(), []
    pending, last_wrapped, counters, pending_garbled = [], None, {}, False
    for page in pages:
        entries = _clean_lines(page["text"])
        for position, entry in enumerate(entries):
            line = entry["text"]
            if not line:
                continue
            following = entries[position + 1]["text"] if position + 1 < len(entries) else ""
            figures_next = bool(following) and not _split_numbers(following, ncols)[0] and len(_split_numbers(following, ncols)[1]) == ncols
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
                columns = dates[:ncols] if len(dates) >= ncols else (
                    fallback_columns or ([period_end, _prior_year(period_end)] if period_end else [f"column {i + 1}" for i in range(ncols)]))
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
                if _SECTION.match(line.strip()) and not figures_next:  # a label with its figures on the next line is not a heading
                    pending, last_wrapped = [], None
                    continue
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

    report["dates"] = sorted({d for row in report["lines"] for d in row["values"]}, reverse=True)
    report["columns"] = list(report["lines"][0]["values"]) if report["lines"] else (fallback_columns or [])  # printed order
    if not report["lines"]:
        warnings.append("No statement lines were found. The reader expects two figure columns (current and previous year). "
                        "If the PDF is a scan, configure the OCR model and read it again.")
    if report["period_end"] and report["dates"] and report["dates"][0] != report["period_end"] and not str(report["dates"][0]).startswith("column"):
        warnings.append(f"The newest column date {report['dates'][0]} differs from the statement date {report['period_end']}.")
    if any(p["method"] == "ocr" for p in report["pages"]):
        warnings.append("Some pages were read by OCR; check every number against the PDF before importing.")
    if any(p["method"] == "empty" for p in report["pages"]):
        failed = [p for p in pages if p.get("error")]
        warnings.append(f"{sum(p['method'] == 'empty' for p in report['pages'])} page(s) have no readable text"
                        + (f" and the OCR model failed on {len(failed)} of them ({failed[0]['error']}); check the vision model name and key in the settings."
                           if failed else " and OCR was not run."))
    if "�" in full:
        warnings.append("The PDF text contains damaged characters (accents). Labels are kept as printed.")
    report["checks"] = consistency_checks(report)
    for check in report["checks"]:
        if not check["ok"]:
            warnings.append(f"Check failed - {check['name']}: {check['detail']}")
    return report


def parse_statement(source, filename=None, ocr=None, chat_fn=None):
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
    pages = extract_pages(content, ocr=ocr)
    report = parse_pages(pages, filename or "statement.pdf", hashlib.sha256(content).hexdigest())
    if chat_fn is not None or llm_available("text"):
        try:
            enrich_with_ai(report, pages, chat_fn)
        except Exception as exc:  # the rules' reading stands on its own
            report["warnings"].append(f"The AI reading could not run ({exc}); the lines come from the rule-based reader.")
    else:
        report["warnings"].append("No language model is configured, so only the rule-based reader ran. Add a CRAWLER_LLM key for the AI reading.")
    return report


def apply_period_end(report, closing_date):
    """Give the figure columns their dates once the closing date of the NEWEST column is known (the PDF printed no readable date).

    Until then the columns are placeholders ("column 1", "column 2") in printed order. The first printed column is taken to be the
    newest, as Italian statements print it; when the headings printed the years (report["column_years"]) those are used, so a
    statement that prints the older year first lands correctly. Idempotent: a report whose columns are dated is left alone."""
    closing = date.fromisoformat(str(closing_date)[:10]).isoformat()
    old = list(report.get("columns") or [])
    if not old or not any(str(c).startswith("column") for c in old):
        report["period_end"] = report.get("period_end") or closing
        return report
    years = report.get("column_years")
    if years and len(years) == len(old) and max(years) == int(closing[:4]):
        new = [_with_year(closing, y) for y in years]
    else:
        new = [closing] + [_with_year(closing, int(closing[:4]) - i) for i in range(1, len(old))]
    rename = dict(zip(old, new))
    for row in report["lines"]:
        row["values"] = {rename.get(k, k): v for k, v in row["values"].items()}
        row["raw"] = {rename.get(k, k): v for k, v in row["raw"].items()}
    report["columns"], report["period_end"] = new, max(new)
    report["dates"] = sorted({d for row in report["lines"] for d in row["values"]}, reverse=True)
    report["warnings"] = [w for w in report["warnings"] if not w.startswith("The statement date could not be read")
                          and not w.startswith("Check failed")]
    report["checks"] = consistency_checks(report)
    report["warnings"] += [f"Check failed - {c['name']}: {c['detail']}" for c in report["checks"] if not c["ok"]]
    return report


def _find(report, statement, pattern):
    for row in report["lines"]:
        if row["statement"] == statement and re.search(pattern, key(row["label"])):
            return row
    return None


def consistency_checks(report):
    """Totals that must agree if the lines were read correctly."""
    checks = []
    assets = _find(report, "assets", r"^(?:totale attivo|total assets)$")
    liabilities = _find(report, "liabilities", r"^(?:totale passivo(?: e patrimonio netto)?|total liabilities and (?:net )?equity)$")
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
    value = _find(report, "income", r"^(?:\d+ )?(?:a )?(?:totale )?valore della produzione$|^total (?:value of )?production$")
    costs = _find(report, "income", r"^(?:\d+ )?(?:b )?(?:totale )?costi della produzione$|^total production costs$")
    result = _find(report, "income", r"^differenza tra valore e costi della produzione(?: a b)?$")
    if value and costs and result:  # costs are printed positive (OIC) or in brackets: the sign is not what is checked
        for d in value["values"]:
            a, b, r = value["values"].get(d), costs["values"].get(d), result["values"].get(d)
            if None not in (a, b, r):
                checks.append(dict(name=f"Value of production minus production costs equals the operating result ({d})",
                                   ok=abs(a - abs(b) - r) < 1, detail=f"{a} - {abs(b)} vs {r}"))
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
    ("details", r"^legal form$|^forma giuridica$", "company:legal_form", 0.8, ""),
    ("other", r"(?:numero medio|average number)[a-z ]*(?:dipendenti|employees)|^(?:dipendenti|employees)$|^number of employees$", "employees", 0.7,
     "Average headcount printed outside the primary statements."),
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
                picked = dict(target=base if ":" in base else f"base:{base}", confidence=confidence, origin="rule", note=note)
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
    "Allowed facts: " + ", ".join(PDF_BASES) + ". "
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
    allowed, ids = set(PDF_BASES), {r["id"] for r in open_lines}
    out = {}
    for item in json.loads(match[0]):
        if item.get("id") in ids and item.get("target") in allowed:
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            out[item["id"]] = dict(target=f"base:{item['target']}", confidence=confidence, origin="ai",
                                   note=str(item.get("rationale") or "")[:300])
    return out


# ------------------------------------------------------------------------------------------------ AI extraction

_EXTRACT_PROMPT = (
    "You read pages of a company's annual financial statements (already OCR'd or taken from the PDF text layer, Italian or "
    "English). Extract EVERY data point printed on the pages you are given, exactly as printed. Answer with JSON only:\n"
    '{"company": {"legal_name": "", "registration_number": "", "legal_form": "", "share_capital": "", "registered_office": "", '
    '"rea": ""}, "facts": [[page, "assets|liabilities|income|comprehensive|other", "item number or empty", '
    '"line label as printed", "current column text", "previous column text"], ...]}\n'
    "Rules: copy each number exactly as printed in `printed` (keep dots, commas, brackets, minus signs, '-' for nil); never "
    "calculate, round or invent; one fact per table row, including sub-lines; `other` is for figures outside the primary "
    "statements (average number of employees, ...), with the label saying what it is; omit what is not on the pages; "
    "leave company fields empty when not printed."
)
EXTRACT_PAGES_PER_CALL = 1  # a statement page has dozens of rows; more pages per call overrun the answer limit
EXTRACT_MAX_PAGES = 18


def _squash(text):
    return re.sub(r"\s+", "", text or "")


def _printed_on_page(token, page_text):
    """The token stands on the page as its own number, not as part of a longer one ("1" is not in "952.074")."""
    token = re.sub(r"\s+", " ", token or "").strip()
    if not token:
        return False
    if set(token) <= {"-", " "}:
        return "-" in page_text
    return bool(re.search(r"(?<![\w.,])" + re.escape(token) + r"(?!\w|[.,]\d)", re.sub(r"\s+", " ", page_text)))


def _pages_to_read(pages):
    picked = []
    for p in pages:
        text = p["text"]
        headed = any(pattern.search(line.strip()) for _, pattern in _HEADINGS for line in text.splitlines())
        if p["page"] == 1 or headed or re.search(r"dipendenti|employees", text, re.I):
            picked.append(p)
    return picked[:EXTRACT_MAX_PAGES]


def _row_to_fact(row):
    """Compact row [page, statement, item, label, printed...] -> fact dict (dict rows pass through)."""
    if isinstance(row, dict):
        return row
    row = list(row)
    return dict(page=row[0] if row else None, statement=row[1] if len(row) > 1 else "other", item=row[2] if len(row) > 2 else None,
                label=row[3] if len(row) > 3 else "", printed=row[4:])


def _parse_answer(answer):
    """(data, truncated). A cut-off answer still yields every complete row it contained."""
    match = re.search(r"\{.*\}", answer, re.S)
    if match:
        try:
            return json.loads(match[0]), False
        except ValueError:
            pass
    rows = []
    for text in re.findall(r"\{[^{}]*\"printed\"\s*:\s*\[[^\]]*\][^{}]*\}|\[\s*\d+\s*,\s*\"[a-z]+\"\s*,[^\[\]]*\]", answer, re.S):
        try:
            rows.append(json.loads(text))
        except ValueError:
            continue
    company = {}
    head = re.search(r"\"company\"\s*:\s*(\{[^{}]*\})", answer, re.S)
    if head:
        try:
            company = json.loads(head[1])
        except ValueError:
            pass
    return ({"company": company, "facts": rows}, True) if rows or company else (None, True)


def ai_extract(pages, chat_fn=None):
    """{company, facts, ungrounded}: the model's reading, kept only where it is printed on the page."""
    chat_fn = chat_fn or chat
    text_of = {p["page"]: p["text"] for p in pages}
    company, facts, ungrounded, failures = {}, [], 0, []
    chosen = _pages_to_read(pages)
    for start in range(0, len(chosen), EXTRACT_PAGES_PER_CALL):
        batch = chosen[start:start + EXTRACT_PAGES_PER_CALL]
        body = "\n\n".join(f"=== PAGE {p['page']} ===\n{p['text']}" for p in batch)
        try:
            answer = chat_fn([dict(role="system", content=_EXTRACT_PROMPT), dict(role="user", content=body)])
        except Exception as exc:  # the other batches still count
            failures.append(f"pages {batch[0]['page']}-{batch[-1]['page']}: {exc}")
            continue
        data, truncated = _parse_answer(answer)
        if data is not None:
            data["facts"] = [_row_to_fact(f) for f in data.get("facts") or []]
        if data is None:
            failures.append(f"pages {batch[0]['page']}-{batch[-1]['page']}: the answer was not readable")
            continue
        if truncated:
            failures.append(f"pages {batch[0]['page']}-{batch[-1]['page']}: the answer was cut off; only its complete rows were used")
        joined = _squash(" ".join(text_of[p["page"]] for p in batch)).casefold()
        for field, value in (data.get("company") or {}).items():
            value = str(value or "").strip()
            if value and field not in company and _squash(value).casefold() in joined:
                company[field] = value
        allowed = {p["page"] for p in batch}
        for fact in data.get("facts") or []:
            page = fact.get("page") if fact.get("page") in allowed else (batch[0]["page"] if len(batch) == 1 else None)
            printed = [str(x).strip() for x in (fact.get("printed") or []) if str(x).strip()]
            label = str(fact.get("label") or "").strip()
            if page is None or not label or not printed:
                ungrounded += 1
                continue
            page_text = _squash(text_of[page]).casefold()
            if not all(_printed_on_page(token, text_of[page]) for token in printed) or _squash(label)[:12].casefold() not in page_text:
                ungrounded += 1  # a number or label that is not on the page is not accepted
                continue
            statement = fact.get("statement") if fact.get("statement") in {"assets", "liabilities", "income", "comprehensive", "other"} else "other"
            item = str(fact["item"]) if fact.get("item") not in (None, "", "null") else None
            facts.append(dict(page=page, statement=statement, item=item, label=label, printed=printed))
    return dict(company=company, facts=facts, ungrounded=ungrounded, failures=failures)


def _base_key(label):
    """Label without its item number or letter, so "a) crediti verso banche", "6) per servizi" and "per servizi" meet."""
    return key(re.sub(r"^\s*(?:\d{1,3}[.)]|[A-Za-z]\))\s*", "", label or ""))


def _printed_number(token):
    token = re.sub(r"\s+", "", token or "")
    if token and set(token) <= {"-"}:
        return 0.0                       # nil marker, however many the model repeated
    return parse_number(token) if _is_number_token(token) else None


def enrich_with_ai(report, pages, chat_fn=None):
    """Fold the model's reading into a rule-parsed report: it may add lines the rules could not read, and company details.
    Numbers are always parsed from the printed text; where model and rules read different numbers the rules' value is kept
    and the line says so."""
    extraction = ai_extract(pages, chat_fn)
    # The columns in the order the PDF prints them: the model's figures follow that order, not the order of the dates
    # (a statement that prints the older year first would otherwise have its two years swapped).
    columns = report.get("columns") or report["dates"][:2] or (
        [report["period_end"], _prior_year(report["period_end"])] if report["period_end"] else [])
    counters = {}
    for row in report["lines"]:
        stem, number = row["id"].rsplit("|", 1)
        counters[stem] = max(counters.get(stem, 0), int(number))
    known = {}
    for row in report["lines"]:
        known.setdefault((row["statement"], row["page"], row["item"] if row["item"] else _base_key(row["label"])), []).append(row)
        known.setdefault((row["statement"], row["page"], _base_key(row["label"])), []).append(row)
        row.setdefault("origin", "rules")
    stats = dict(agreed=0, ai_only=0, mismatch=0, rules_only=0, ungrounded=extraction["ungrounded"])
    for fact in extraction["facts"]:
        numeric_item = fact["item"] if fact["item"] and fact["item"].isdigit() else None
        numbers = [_printed_number(t) for t in fact["printed"]]
        identifier = bool(re.search(r"codice fiscale|(?<!\w)p\.?\s?i\.?(?!\w)|partita iva|(?<!\w)rea(?!\w)|iscrizione|denominazione|paese|"
                                    r"capitale sociale|tax code|(?<!\w)vat(?!\w)|registration|albo", fact["label"], re.I))
        if fact["statement"] == "other" and (identifier or (numbers and any(n is None for n in numbers))) and report["period_end"]:
            # Text printed outside the tables (cover page): a company detail, not a figure.
            stem = f"details|{_base_key(fact['label'])}"
            counters[stem] = counters.get(stem, 0) + 1
            report["lines"].append(dict(id=f"{stem}|{counters[stem]}", statement="details", item=None, label=fact["label"],
                                        page=fact["page"], values={report["period_end"]: fact["printed"][0]},
                                        raw={report["period_end"]: fact["printed"][0]}, flags=[], origin="ai"))
            stats["details"] = stats.get("details", 0) + 1
            continue
        values = dict(zip(columns, numbers))
        usable = lambda rows: [r for r in rows if r["origin"] in {"rules", "ai+rules"}]  # noqa: E731
        # The same printed label first; a numeric item only when it names one row on the page (OIC sub-items repeat "1)").
        candidates = usable(known.get((fact["statement"], fact["page"], _base_key(fact["label"])), []))
        if not candidates and numeric_item:
            by_item = usable(known.get((fact["statement"], fact["page"], numeric_item), []))
            candidates = by_item if len(by_item) == 1 else []
        match = candidates[0] if candidates else None
        if match:
            if all(match["values"].get(d) == v for d, v in values.items() if v is not None):
                match["origin"] = "ai+rules"
                stats["agreed"] += 1
            else:
                match["flags"].append("the AI read different numbers: " + ", ".join(fact["printed"])
                                      + " (kept the numbers parsed from the printed text)")
                stats["mismatch"] += 1
            continue
        if not columns or not values or any(v is None for v in values.values()):
            stats["ungrounded"] += 1
            continue
        stem = f"{fact['statement']}|{numeric_item or key(fact['label'])}"
        counters[stem] = counters.get(stem, 0) + 1
        report["lines"].append(dict(id=f"{stem}|{counters[stem]}", statement=fact["statement"], item=fact["item"], label=fact["label"],
                                    page=fact["page"], values=values, raw=dict(zip(columns, fact["printed"])),
                                    flags=["read by the AI only"], origin="ai"))
        stats["ai_only"] += 1
    stats["rules_only"] = sum(1 for r in report["lines"] if r["origin"] == "rules")
    for field, value in extraction["company"].items():
        report["company"].setdefault(field, value)
    if report["period_end"]:
        for field, label in (("legal_form", "Legal form"), ("share_capital", "Share capital"),
                             ("registered_office", "Registered office"), ("rea", "REA number")):
            if extraction["company"].get(field):
                report["lines"].append(dict(id=f"details|{field}|1", statement="details", item=None, label=label, page=1,
                                            values={report["period_end"]: extraction["company"][field]},
                                            raw={report["period_end"]: extraction["company"][field]}, flags=[], origin="ai"))
    report["extraction"] = stats
    report["checks"] = consistency_checks(report)
    report["dates"] = sorted({d for row in report["lines"] if row["statement"] != "details" for d in row["values"]}, reverse=True)
    for failure in extraction["failures"]:
        report["warnings"].append(f"The AI could not read {failure}; those pages come from the rule-based reader only.")
    if stats["ungrounded"]:
        report["warnings"].append(f"{stats['ungrounded']} items the AI returned were dropped because they are not printed on the page.")
    return report
