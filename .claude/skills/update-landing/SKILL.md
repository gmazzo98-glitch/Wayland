---
name: update-landing
description: "Publish a new Claude Design export of the Wayland landing page to wayland.my. Use when the user says 'update landing', 'publish the new landing page' or types /update-landing. Takes the newest Wayland*.html export from Downloads (or a given path), makes its forms POST to /api/submit, commits, pushes, deploys to the Vercel project wayland_landingpage and checks the live site."
user-invocable: true
argument-hint: "[path to exported .html, default: newest Wayland*.html in Downloads]"
---

Publishes the Claude Design landing page to https://wayland.my. Background: `landing/README.md`.

Fixed facts:
- Vercel project `wayland_landingpage`, id `prj_YfwZkHxUIA8frHj0ahlWaFCDOUdm`, team agri-portal. Do not pass `teamId` to the Vercel project tools (an explicit teamId made them return 404 in this account); `create_deployment` works without it.
- Deploys come from GitHub `gmazzo98-glitch/Wayland`, branch master, root directory `landing`. The project is not git-linked, so a push alone publishes nothing: call the Vercel MCP `create_deployment` with the pushed sha (params below).
- Form API: `landing/api/submit.js` writes to Neon schema `landing` (`company_requests`, `startup_proposals`). Fields it accepts: company `fullName, companyName, workEmail, painPoints`; startup `startupName, website, workEmail, pitch, dataNeeded`.

Steps:
1. Pick the file. Use the argument if given, otherwise the newest `C:\Users\gmazz\Downloads\Wayland*.html` (list the three newest with dates and say which one you took). If its date is older than the last deploy, say so and ask before using it.
2. Run `python landing/tools/apply_form_patch.py "<file>"`. It copies the page, adding the POST to `/api/submit` if the design does not already have it. Exit code 2 means the design's form code differs: read the template, patch the `sendCo` / `sendSt` equivalents by hand, keep the success state conditional on a 200 response. Read the NOTE lines: if the page contains a phrase Giorgio asked to remove ("Niente dati, niente pilot...", "Non tutte le proposte vengono abbinate", including the confirmation paragraph), remove it and say so.
3. Compare the form fields in the new design with the API fields above. If a field was added, renamed or dropped, STOP and ask Giorgio before changing `submit.js` or the Neon tables.
4. Commit and push only `landing/` (stage explicit paths; other agents work in this repo). End the commit message with the Co-Authored-By trailer from the system reminder.
5. Deploy: Vercel `create_deployment` with `requestBody` `{"name":"wayland_landingpage","project":"prj_YfwZkHxUIA8frHj0ahlWaFCDOUdm","target":"production","gitSource":{"type":"github","org":"gmazzo98-glitch","repo":"Wayland","ref":"master","sha":"<git rev-parse HEAD>"},"projectSettings":{"framework":null,"rootDirectory":"landing","buildCommand":null,"outputDirectory":null,"installCommand":null}}` and `forceNew` "1".
6. Verify: poll `https://wayland.my/` with curl until it serves the new page and contains `api/submit`; check `GET /api/submit` returns 405 and a POST with an invalid body returns 400; open the page in the built-in browser and read the console for errors. Do not submit real-looking data. If the forms changed in step 3, submit one clearly marked test entry (name starting `ZZ`) through the page, confirm the row in Neon, then ask Giorgio before deleting it (destructive SQL needs a yes).
7. Update the landing bullet in section B of `READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md` (deployment id, commit, date), run `python scripts/open_points.py validate`, commit and push it.
8. Report in plain words: what is live, the deployment id, anything that differed from the previous page, and anything not verified.
