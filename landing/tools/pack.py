#!/usr/bin/env python3
"""Edit the wayland.my landing page as normal HTML instead of one escaped line.

landing/index.html is a Claude Design bundle: the page itself is stored, JSON-escaped on a
single line, in its <script type="__bundler/template"> tag, next to the embedded fonts and
video. landing/src/page.html is that page as readable HTML/CSS/JS.

    python landing/tools/pack.py            rebuild landing/index.html from landing/src/page.html
    python landing/tools/pack.py --unpack   refresh landing/src/page.html from landing/index.html
    python landing/tools/pack.py --check    exit 1 if the two are out of sync

Edit src/page.html, run pack.py, then publish. Never edit the escaped line in index.html by hand.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / 'index.html'
SRC = ROOT / 'src' / 'page.html'
BS = chr(92)
TEMPLATE = re.compile(r'(<script type="__bundler/template"[^>]*>)(.*?)(</script>)', re.S)

# The API (landing/api/submit.js) and the Neon tables accept exactly these fields.
REQUIRED_STRINGS = (
    '/api/submit', "form: 'company'", "form: 'startup'",
    'fullName', 'companyName', 'workEmail', 'painPoints',
    'startupName', 'website', 'pitch', 'dataNeeded',
)


def encode(text):
    return json.dumps(text, ensure_ascii=False).replace('</', '<' + BS + 'u002F')


def read_index():
    text = INDEX.read_text(encoding='utf-8')
    m = TEMPLATE.search(text)
    if not m:
        sys.exit('index.html has no __bundler/template tag: it is not a Claude Design bundle.')
    raw = m.group(2)
    body = raw.strip()
    tpl = json.loads(body)
    if encode(tpl) != body:
        sys.exit('The template in index.html does not round-trip; refusing to touch it.')
    return text, m, tpl


def write(path, text):
    # OneDrive can briefly lock a file it is syncing; retry before giving up.
    for attempt in range(6):
        try:
            path.write_text(text, encoding='utf-8', newline='\n')
            return
        except OSError:
            if attempt == 5:
                raise
            time.sleep(0.5)


def read_src():
    return SRC.read_text(encoding='utf-8').replace('\r\n', '\n')


def pack():
    text, m, _ = read_index()
    page = read_src()
    missing = [s for s in REQUIRED_STRINGS
               if not re.search(r'(?<![A-Za-z0-9_])' + re.escape(s) + r'(?![A-Za-z0-9_])', page)]
    if missing:
        sys.exit('page.html no longer contains: ' + ', '.join(missing) +
                 '\nThe form must keep posting these fields to /api/submit (see landing/README.md).')
    raw = m.group(2)
    lead, trail = raw[:len(raw) - len(raw.lstrip())], raw[len(raw.rstrip()):]
    out = text[:m.start(2)] + lead + encode(page) + trail + text[m.end(2):]
    write(INDEX, out)
    print('Rebuilt landing/index.html from landing/src/page.html')


def unpack():
    _, _, tpl = read_index()
    SRC.parent.mkdir(parents=True, exist_ok=True)
    write(SRC, tpl)
    print('Wrote landing/src/page.html (%d characters)' % len(tpl))


def check():
    _, _, tpl = read_index()
    if not SRC.exists() or read_src() != tpl:
        print('landing/src/page.html and landing/index.html are out of sync.')
        print('If you edited page.html run: python landing/tools/pack.py')
        print('If index.html is newer (new Claude Design export) run: python landing/tools/pack.py --unpack')
        return 1
    print('In sync.')
    return 0


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == '--unpack':
        unpack()
    elif arg == '--check':
        sys.exit(check())
    elif arg in ('', '--pack'):
        pack()
    else:
        sys.exit(__doc__)
