import { neon } from '@neondatabase/serverless';

// Receives the two landing page forms and stores them in the Neon `landing` schema.
// DATABASE_URL must be a role that can only INSERT into those two tables (landing_writer).

const EMAIL = /\S+@\S+\.\S+/;
const ALLOWED_HOSTS = new Set(['wayland.my', 'www.wayland.my']);

const clean = (v, max) => (typeof v === 'string' ? v.trim().slice(0, max) : '');

// Best-effort push notification through ntfy.sh (NTFY_TOPIC is the private topic name).
// It never blocks or fails a submission, and carries no email address.
async function notify(title, text) {
  const topic = process.env.NTFY_TOPIC;
  if (!topic) return;
  try {
    await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
      method: 'POST',
      headers: { Title: title, Tags: 'inbox_tray' },
      body: text.replace(/\s+/g, ' ').slice(0, 200),
      signal: AbortSignal.timeout(3000),
    });
  } catch (err) {
    console.error('notify failed:', err && err.message);
  }
}

function originAllowed(req) {
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    const host = new URL(origin).hostname;
    return ALLOWED_HOSTS.has(host) || host.endsWith('.vercel.app');
  } catch {
    return false;
  }
}

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'method_not_allowed' });
  }
  if (!originAllowed(req)) return res.status(403).json({ error: 'forbidden' });

  const b = req.body && typeof req.body === 'object' ? req.body : {};
  if (clean(b.hp, 100)) return res.status(200).json({ ok: true }); // honeypot, bots only

  const sql = neon(process.env.DATABASE_URL);

  try {
    if (b.form === 'company') {
      const fullName = clean(b.fullName, 200);
      const companyName = clean(b.companyName, 200);
      const workEmail = clean(b.workEmail, 254);
      const painPoints = clean(b.painPoints, 5000) || null;
      if (!fullName || !companyName || !EMAIL.test(workEmail)) {
        return res.status(400).json({ error: 'invalid' });
      }
      await sql`INSERT INTO landing.company_requests (full_name, company_name, work_email, pain_points)
                VALUES (${fullName}, ${companyName}, ${workEmail}, ${painPoints})`;
      await notify('New company request', `${companyName}. Details in Neon: landing.company_requests`);
      return res.status(200).json({ ok: true });
    }

    if (b.form === 'startup') {
      const startupName = clean(b.startupName, 200);
      let website = clean(b.website, 500);
      if (/^https?:\/\/?$/i.test(website)) website = '';
      const workEmail = clean(b.workEmail, 254);
      const pitch = clean(b.pitch, 5000) || null;
      const dataNeeded = clean(b.dataNeeded, 5000);
      if (!startupName || !EMAIL.test(workEmail) || !dataNeeded) {
        return res.status(400).json({ error: 'invalid' });
      }
      await sql`INSERT INTO landing.startup_proposals (startup_name, website, work_email, pitch, data_needed)
                VALUES (${startupName}, ${website || null}, ${workEmail}, ${pitch}, ${dataNeeded})`;
      await notify('New startup proposal', `${startupName}. Details in Neon: landing.startup_proposals`);
      return res.status(200).json({ ok: true });
    }

    return res.status(400).json({ error: 'unknown_form' });
  } catch (err) {
    console.error('submit failed:', err && err.message);
    return res.status(500).json({ error: 'server_error' });
  }
}
