#!/usr/bin/env python3
"""Prepare a Claude Design HTML export for wayland.my.

Usage: python landing/tools/apply_form_patch.py EXPORT.html [--out landing/index.html]

A Claude Design export is a bundle whose page lives, JSON-escaped, in the
<script type="__bundler/template"> tag. If the page already POSTs to /api/submit
(the Claude Design prompt in landing/README.md asks for that) it is copied as is.
Otherwise the known mock-up form handlers (sendCo / sendSt) are rewritten to POST
to /api/submit. Exit 2 means the design's form code differs: patch it by hand.
"""
import json
import re
import sys
from pathlib import Path

BS = chr(92)
TEMPLATE = re.compile(r'(<script type="__bundler/template"[^>]*>)(.*?)(</script>)', re.S)
STANDING_REMOVALS = (
    "Niente dati, niente pilot",
    "Non tutte le proposte vengono abbinate",
)

SUBMIT_METHOD = (
    "  submit(form, payload, key, errKey) {\n"
    "    if (this.busy) return;\n"
    "    this.busy = true;\n"
    "    fetch('/api/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ form, ...payload }) })\n"
    "      .then(r => { if (!r.ok) throw new Error(String(r.status)); this.setState({ [errKey]: '', [key]: 'done' }); })\n"
    "      .catch(() => this.setState({ [errKey]: 'Invio non riuscito. Riprova tra qualche istante.' }))\n"
    "      .then(() => { this.busy = false; });\n"
    "  }\n"
)


def encode(tpl):
    return json.dumps(tpl, ensure_ascii=False).replace('</', '<' + BS + 'u002F')


def swap(tpl, old, new):
    if tpl.count(old) != 1:
        raise ValueError("expected exactly one match for: " + old[:70])
    return tpl.replace(old, new)


def patch(tpl):
    tpl = swap(tpl, "  set(k, v) { this.setState(s => ({ f: { ...s.f, [k]: v } })); }\n",
               "  set(k, v) { this.setState(s => ({ f: { ...s.f, [k]: v } })); }\n" + SUBMIT_METHOD)
    tpl = swap(
        tpl,
        "if (m.length) this.setState({ coErr: 'Completa questi campi: ' + m.join(', ') + '.' }); else this.setState({ coErr: '', co: 'done' }); },",
        "if (m.length) { this.setState({ coErr: 'Completa questi campi: ' + m.join(', ') + '.' }); return; }\n"
        "        this.submit('company', { fullName: f.coName, companyName: f.coCompany, workEmail: f.coEmail, painPoints: f.coPain }, 'co', 'coErr'); },")
    tpl = swap(
        tpl,
        "if (m.length) this.setState({ stErr: 'Completa questi campi: ' + m.join(', ') + '.' }); else this.setState({ stErr: '', st: 'done' }); }",
        "if (m.length) { this.setState({ stErr: 'Completa questi campi: ' + m.join(', ') + '.' }); return; }\n"
        "        this.submit('startup', { startupName: f.stName, website: f.stWeb, workEmail: f.stEmail, pitch: f.stDesc, dataNeeded: f.stData }, 'st', 'stErr'); }")
    # the mock-up notes say nothing is sent; with a real backend they are wrong
    for note in ("Mockup: non viene inviato nulla. Non tutte le proposte vengono abbinate.",
                 "Mockup: non viene inviato nulla."):
        if note in tpl:
            tpl = tpl.replace(note, "")
    return tpl


def main(argv):
    if not argv or argv[0].startswith('-'):
        print(__doc__)
        return 1
    src = Path(argv[0])
    out = Path(argv[argv.index('--out') + 1]) if '--out' in argv else Path('landing/index.html')
    text = src.read_text(encoding='utf-8')
    m = TEMPLATE.search(text)
    if not m:
        print("No __bundler/template tag found: this is not a Claude Design bundle export.")
        return 2
    raw = m.group(2)
    body = raw.strip()
    lead, trail = raw[:len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
    tpl = json.loads(body)
    if encode(tpl) != body:
        print("The template encoding does not round-trip, refusing to rewrite it.")
        return 2

    if '/api/submit' in tpl:
        print("Page already posts to /api/submit: copied without changes.")
    else:
        try:
            tpl = patch(tpl)
        except ValueError as err:
            print("The design's form code differs from the known mock-up: " + str(err))
            print("Patch the sendCo / sendSt handlers by hand so they POST to /api/submit.")
            return 2
        print("Patched the form handlers to POST to /api/submit.")

    for phrase in STANDING_REMOVALS:
        if phrase in tpl:
            print("NOTE: the page contains a phrase Giorgio asked to remove earlier: " + phrase)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text[:m.start(2)] + lead + encode(tpl) + trail + text[m.end(2):],
                   encoding='utf-8', newline='\n')
    print("Wrote " + str(out))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
