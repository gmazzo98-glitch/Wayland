# wayland.my landing page

Live at https://wayland.my, Vercel project `wayland_landingpage` (team agri-portal). Source: `index.html` (Claude Design export) and `api/submit.js` (form endpoint, writes to Neon schema `landing`).

## Updating the page

1. Change the design in Claude Design and export the HTML into `Downloads`.
2. In Claude Code, in this repo, type `/update-landing`. It patches the forms if needed, pushes and deploys, then checks the live site.

The Vercel project is not linked to git, so a push alone does not publish; `/update-landing` does the deploy.

## Prompt to paste once into Claude Design

```
Wire the two forms ("Azienda" and "Startup") to a real backend. Do not change the layout, the animations, the colors or the copy, except where this brief says so.

On submit, keep today's required-field and email validation, then call:
fetch('/api/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })

Azienda payload: { form: 'company', fullName, companyName, workEmail, painPoints }
- fullName = "Nome e cognome" (required), companyName = "Ragione sociale" (required), workEmail = "Email di lavoro" (required), painPoints = "Dove perdete soldi o tempo nei progetti?" (optional, send an empty string if blank).

Startup payload: { form: 'startup', startupName, website, workEmail, pitch, dataNeeded }
- startupName = "Nome della startup" (required), website = "Sito web" (optional), workEmail = "Email di lavoro" (required), pitch = "Cosa risolvi (breve pitch)" (optional), dataNeeded = "Dati che ti servono dall'azienda" (required).

Show the thank-you state only when the response is 200 (response.ok). On any error keep the typed values and show the message: "Invio non riuscito. Riprova tra qualche istante." Disable the send button while the request is running so it cannot be sent twice.

Use exactly these field names and no others: the database has these columns only. If you want an extra field, ask me first. Do not use mailto, third-party form services or localStorage.

Remove every note saying the form is a mockup or that nothing is sent. Do not include these texts anywhere: "Niente dati, niente pilot. Confermiamo che l'azienda possa fornirli prima di abbinarti.", "Non tutte le proposte vengono abbinate." and the confirmation paragraph "Non tutte le proposte vengono abbinate: confrontiamo ogni candidatura con le sfide reali delle aziende."
```

If an export already posts to `/api/submit`, `/update-landing` publishes it without changing it.
