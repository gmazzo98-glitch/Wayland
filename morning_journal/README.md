# Wayland Morning Journal

A daily GitHub Actions workflow (free) that analyses the latest Wayland repository
commits plus European/Italian startup & SME news feeds, has Mistral write
"The Wayland Morning Journal", and saves it as a browsable HTML page in the
shared Google Drive folder **Wayland Morning Journal** (Vienna/Wayland).

- `morning_journal/generate_journal.py` — standalone generator (stdlib only)
- `.github/workflows/wayland-morning-journal.yml` — daily schedule (07:30 Europe/Rome) + manual trigger

## One-time setup

### 1. Repo secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `MISTRAL_API_KEY` | API key from console.mistral.ai (free tier) |
| `JOURNAL_BRIDGE_URL` | the deployed Apps Script web app URL (step 2) |

### 2. The Google Drive bridge (deploy once, ~3 minutes)

1. Open <https://script.google.com> with the account that owns the Drive folder → **New project**.
2. Paste the code below (folder ID is already set).
3. **Deploy → New deployment** → type **Web app**:
   - Execute as: **Me**
   - Who has access: **Anyone**
4. Copy the web app URL and save it as the `JOURNAL_BRIDGE_URL` repo secret.

```javascript
/** Wayland Morning Journal — Drive bridge. Saves posted reports into the shared folder. */
var FOLDER_ID = '1VOAY-i-woL5vx3aqG-9NW8nGEDdKL258';

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var folder = DriveApp.getFolderById(FOLDER_ID);
    var name = data.filename || data.subject || 'report.html';

    var existing = folder.getFilesByName(name);
    while (existing.hasNext()) { folder.removeFile(existing.next()); }
    var file = folder.createFile(name, data.html, MimeType.HTML);
    try { file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW); } catch (err) {}

    var prev = folder.getFilesByName('latest.html');
    while (prev.hasNext()) { folder.removeFile(prev.next()); }
    folder.createFile('latest.html', data.html, MimeType.HTML);

    return ContentService.createTextOutput(JSON.stringify({ ok: true, fileId: file.getId() }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
```

## Files produced

- `The Wayland Morning Journal dd.mm.yy.html` — dated daily report
- `latest.html` — always the most recent report (stable link to share)

## Test / re-run

Push any change to `morning_journal/.trigger`, or use the Actions tab →
"Wayland Morning Journal" → Run workflow. Diagnostics appear as annotations on
the run page and in the run summary.
