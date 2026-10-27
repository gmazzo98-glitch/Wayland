# wayland.my landing page

Live at https://wayland.my, Vercel project `wayland_landingpage` (team agri-portal). Source: `index.html` (Claude Design export) and `api/submit.js` (form endpoint, writes to Neon schema `landing`).

## Updating the page

1. Change the design in Claude Design and export the HTML into `Downloads`.
2. In Claude Code, in this repo, type `/update-landing`. It patches the forms if needed, pushes and deploys, then checks the live site.

The Vercel project is not linked to git, so a push alone does not publish; `/update-landing` does the deploy.

## Prompt to paste into Claude Design (use it again after any redesign)

```
Wire the two forms ("Azienda" and "Startup") to a real backend and add the legal links. Do not change the layout, the animations, the colors or the copy, except where this brief says so.

On submit, keep today's required-field and email validation, then call:
fetch('/api/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })

Azienda payload: { form: 'company', termsAccepted: true, termsVersion: '1.0', fullName, companyName, workEmail, painPoints }
- fullName = "Nome e cognome" (required), companyName = "Ragione sociale" (required), workEmail = "Email di lavoro" (required), painPoints = "Dove perdete soldi o tempo nei progetti?" (optional, send an empty string if blank).

Startup payload: { form: 'startup', termsAccepted: true, termsVersion: '1.0', startupName, website, workEmail, pitch, dataNeeded }
- startupName = "Nome della startup" (required), website = "Sito web" (optional), workEmail = "Email di lavoro" (required), pitch = "Cosa risolvi (breve pitch)" (optional), dataNeeded = "Dati che ti servono dall'azienda" (required).

Each form has one required checkbox, unticked by default, above the send button, with the text: "Ho letto e accetto i Termini di servizio e ho preso visione dell'Informativa privacy." The words "Termini di servizio" link to /terms and "Informativa privacy" link to /privacy, both with target="_blank" rel="noopener". If it is not ticked, do not send, and add "accettazione dei Termini di servizio e presa visione dell'Informativa privacy" to the missing-fields message. Keep the version in one constant (LEGAL_VERSION = '1.0') and send it as termsVersion.

Add a footer with "(c) 2026 WAYLAND. Tutti i diritti riservati." and two links, "Informativa privacy" (/privacy) and "Termini di servizio" (/terms), opening in a new tab. Do not build pop-ups and do not copy the legal text into the page: /privacy and /terms are separate pages that already exist.

Show the thank-you state only when the response is 200 (response.ok). On any error keep the typed values and show the message: "Invio non riuscito. Riprova tra qualche istante." Disable the send button while the request is running so it cannot be sent twice.

Use exactly these field names and no others: the database has these columns only. If you want an extra field, ask me first. Do not use mailto, third-party form services, third-party fonts or scripts loaded from other sites, cookies or localStorage.

Remove every note saying the form is a mockup or that nothing is sent. Do not include these texts anywhere: "Niente dati, niente pilot. Confermiamo che l'azienda possa fornirli prima di abbinarti.", "Non tutte le proposte vengono abbinate." and the confirmation paragraph "Non tutte le proposte vengono abbinate: confrontiamo ogni candidatura con le sfide reali delle aziende."
```

## Legal pages (privacy notice and terms)

- `privacy/index.html` and `terms/index.html` are the only copy of the texts, served at `/privacy` and `/terms`. They are static pages with no external requests (no web fonts from Google). The forms link to them in a new tab; there are no pop-ups.
- Every request stores `terms_accepted_at` and `terms_version` (Neon, both tables). The API refuses a request without `termsAccepted: true` and a `termsVersion` like `1.0`.
- When the wording of either text changes in substance: change the version number and the date at the top of both pages and `LEGAL_VERSION` in `src/page.html` together, then run `pack.py`.
- The texts describe the real setup (Vercel and Neon in Germany, the ntfy.sh notification with only the company or startup name, no cookies, no promotional emails). If the setup changes (new tool, analytics, email sending), the privacy text must change first.
- Still blank, only Giorgio can supply them (search the pages for `placeholder`): legal name and form, registered office, VAT number (P.IVA), PEC, court (foro) for the terms, and the email provider. The addresses info@wayland.my and privacy@wayland.my are shown as contact points: they must really exist (wayland.my has no mail records yet).
- The retention periods in the privacy text (24 months, 12 months, 10 years, logs 6-12 months) are promises, not automatic: nothing deletes data by itself yet.
- Have a lawyer read both texts before relying on them.

If an export already posts to `/api/submit`, `/update-landing` publishes it without changing it.

## Notifications

Each saved request also sends a push message through ntfy.sh to the private topic in the Vercel env var `NTFY_TOPIC` (production). Subscribe to that topic in the ntfy app or at https://ntfy.sh/<topic>. The message carries only the form type and the company or startup name, no email address; the details are in Neon. If ntfy is down the request is still saved.

## Editing the page (Claude, Antigravity, Codex, Mistral, ChatGPT, by hand)

`index.html` is a Claude Design bundle: fonts and the intro video are embedded, and the page itself sits JSON-escaped on one line. Do not edit that line. The readable copy is `src/page.html`.

1. Edit `src/page.html` (text, CSS, animations, the script at the bottom).
2. `python landing/tools/pack.py` rebuilds `index.html` from it (it refuses a page that lost the form fields or the POST to `/api/submit`).
3. `python landing/tools/pack.py --check` says "In sync" when both files match. Commit `src/page.html` and `index.html` together.
4. Publish: with the Vercel project connected to the repo (Project Settings, Git, root directory `landing`), pushing a branch gives a preview link and merging to `master` goes live. Until it is connected, ask Claude Code to run `/update-landing` or deploy from the Vercel dashboard.

After a NEW Claude Design export (`/update-landing`): `python landing/tools/apply_form_patch.py "<export>.html"` then `python landing/tools/pack.py --unpack` to refresh `src/page.html`.

Rules for any agent: see "Landing page" in the root `AGENTS.md` (keep the form fields, no secrets in the repo, do not touch the Neon tables, keep the removed copy out).

Chat-only tools (ChatGPT or Mistral in the browser) cannot read the repo: paste `src/page.html` into the chat, ask for the change, then save the answer over `src/page.html` and run `pack.py`.
