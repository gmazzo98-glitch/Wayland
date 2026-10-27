"""
Collector `tech-stack`: the company's technology and digital footprint, read from its own website (ticket I18).

What it reads, in order: the homepage and a bounded sample of the internal pages the homepage itself links to
(never guessed paths), then three public lookups about the domain. Everything it emits is a ledger fact under
`digital.` with the exact markup, header or API text as its quote, checked with quote_in_text() against what was
fetched and passed through the verification gate (intel/verify.py) like every other collector's facts.

  technologies     CMS, e-commerce, JavaScript frameworks, analytics, tag managers, marketing automation, CRM,
                   B2B visitor identification, live chat, cookie consent, CAD and 3D product portals, configurators
                   and CPQ, customer / support / IoT portals, web agency credit. Many-valued keys: one fact per
                   technology, value "Name" or "Name 1.2.3".
  outdated         jQuery below 3.5, Bootstrap 3 and 4, AngularJS, Flash, a Universal Analytics tag that no longer
                   collects (`digital.outdated_software`, shared with site_headers.py which does the server side).
  site quality     `digital.quality_issue`: placeholder or template text (alt text "John Doe", lorem ipsum, default
                   server pages), statistic counters that ship as 0, broken internal links, a stale copyright year,
                   template demo files still linked, a homepage set to noindex, mixed-language pages.
                   Every issue states WHERE and quotes the markup. Observations, not verdicts.
  social           `digital.social_<network>`: the company accounts its site links (LinkedIn, Facebook, Instagram,
                   YouTube, X, Xing, TikTok); for YouTube the public channel feed gives the last public upload.
  domain           `digital.domain_registered_on` (RDAP, only for TLDs that publish RDAP: .com, .net, .org, .fr ...;
                   NOT .it, .de, .eu, .ch, .at, which have none), `digital.wayback_first_seen` (Internet Archive).

The signature set in this file is original (written from how each product announces itself in markup), NOT an
import of the Wappalyzer / webappanalyzer database: that database is GPL-3.0 (checked 2026-10-27), which this
repository must not absorb. It is a starting set; add a signature as one `Sig(...)` line plus a test.

Not here: `tech_stack_intensity` and every other score or signal (ticket I35 owns score-moving changes); typo
checking; LinkedIn or Facebook page content (decision G2: public pages only, no logged-in reading).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urldefrag, urlparse

from intel.collector import Collector, CollectorContext, CollectorResult
from intel.collectors.site_headers import FactSink, fetch_homepage, header_block, registrable_host
from intel.fetch import Page
from intel.ledger import quote_in_text
from intel.verify import extract_brand_tokens, normalize_vat

COLLECTOR_TAG = "I18:tech-stack@1"
MAX_SAMPLED_PAGES = 12            # internal pages read besides the homepage
STALE_COPYRIGHT_YEARS = 2         # a footer year this many years behind (or more) is reported
MIN_COUNTERS_FOR_ISSUE = 2        # one bare 0 can be legitimate ("0 accidents"); two or more is a pattern
MIXED_LANGUAGE_MIN_WORDS = 80     # stop-word hits needed before a page is judged on its language mix
MIXED_LANGUAGE_MIN_SHARE = 0.30   # the second language must carry at least this share of the hits
MAX_ISSUE_FACTS_PER_KIND = 5


# ======================================================================================================
# Signatures
# ======================================================================================================

@dataclass(frozen=True)
class Sig:
    key: str                              # fact key suffix, e.g. "cms" -> digital.cms
    name: str
    patterns: Tuple[str, ...]             # regexes, case-insensitive; any one is enough
    where: str = "raw"                    # "raw" = page markup including scripts, "header" = response headers
    version: Tuple[str, ...] = ()         # regexes with ONE group, tried in order on the matched snippet


S = Sig
SIGNATURES: Tuple[Sig, ...] = (
    # --- content management and site builders -------------------------------------------------------------
    S("cms", "WordPress", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']WordPress", r"/wp-content/(?:themes|plugins|uploads)/",
                           r"/wp-includes/", r"/wp-json/"),
      version=(r"content=[\"']WordPress\s+(\d+\.\d+(?:\.\d+)?)", r"wp-emoji-release\.min\.js\?ver=(\d+\.\d+(?:\.\d+)?)")),
    S("cms", "Joomla", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Joomla", r"/media/jui/", r"/media/system/js/core\.js",
                        r"/templates/[a-z0-9_-]+/css/template\.css")),
    S("cms", "Drupal", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Drupal", r"/sites/default/files/", r"Drupal\.settings",
                        r"/core/misc/drupal\.js"), version=(r"content=[\"']Drupal\s+(\d+)",)),
    S("cms", "TYPO3", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']TYPO3", r"/typo3conf/", r"/typo3temp/")),
    S("cms", "Contao", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Contao",)),
    S("cms", "Umbraco", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Umbraco", r"/umbraco/(?:lib|assets)/")),
    S("cms", "Kentico", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Kentico", r"/CMSPages/GetResource\.ashx")),
    S("cms", "DotNetNuke (DNN)", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']DotNetNuke", r"/Portals/_default/")),
    S("cms", "Adobe Experience Manager", (r"/etc\.clientlibs/", r"/etc/designs/[a-z0-9_-]+/clientlibs")),
    S("cms", "Craft CMS", (r"/cpresources/",)),
    S("cms", "Ghost", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Ghost",)),
    S("cms", "Odoo", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Odoo", r"/web/static/src/")),
    S("cms", "Wix", (r"static\.wixstatic\.com", r"static\.parastorage\.com", r"X-Wix-Request-Id"), where="raw"),
    S("cms", "Wix", (r"^x-wix-request-id:",), where="header"),
    S("cms", "Squarespace", (r"static1\.squarespace\.com", r"squarespace-cdn\.com", r"Static\.SQUARESPACE_CONTEXT")),
    S("cms", "Webflow", (r"assets\.website-files\.com", r"\bdata-wf-(?:page|site)=", r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Webflow")),
    S("cms", "Jimdo", (r"assets\.jimstatic\.com", r"jimdofree\.com", r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Jimdo")),
    S("cms", "Weebly", (r"editmysite\.com", r"cdn\d*\.editmysite", r"weebly\.com/")),
    S("cms", "GoDaddy Website Builder", (r"img\d*\.wsimg\.com", r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Starfield")),
    S("cms", "HubSpot CMS", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']HubSpot",)),
    S("cms", "Gatsby", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Gatsby", r"___gatsby")),
    # --- e-commerce ---------------------------------------------------------------------------------------
    S("ecommerce_platform", "WooCommerce", (r"/wp-content/plugins/woocommerce/", r"\bwc-add-to-cart\b", r"<body[^>]+class=[\"'][^\"']*\bwoocommerce\b"),
      version=(r"woocommerce/[^\"']*\?ver=(\d+\.\d+(?:\.\d+)?)",)),
    S("ecommerce_platform", "Shopify", (r"cdn\.shopify\.com", r"Shopify\.theme\b")),
    S("ecommerce_platform", "Magento", (r"/static/version\d+/frontend/", r"\bMage\.Cookies\b", r"/skin/frontend/", r"mage/cookies\.js")),
    S("ecommerce_platform", "PrestaShop", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']PrestaShop", r"/modules/ps_[a-z]+/",
                                           r"var\s+prestashop\s*=")),
    S("ecommerce_platform", "BigCommerce", (r"cdn\d*\.bigcommerce\.com",)),
    S("ecommerce_platform", "OpenCart", (r"catalog/view/theme/", r"index\.php\?route=common/home")),
    S("ecommerce_platform", "Shopware", (r"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']Shopware", r"/bundles/storefront/")),
    S("ecommerce_platform", "Salesforce Commerce Cloud", (r"demandware\.(?:edgesuite|static)", r"/on/demandware\.store/")),
    S("ecommerce_platform", "SAP Commerce (Hybris)", (r"/_ui/(?:responsive|desktop)/", r"yacceleratorstorefront")),
    # --- front-end frameworks and libraries ---------------------------------------------------------------
    S("js_framework", "React", (r"\bdata-reactroot\b", r"\bdata-reactid\b", r"react(?:-dom)?(?:\.production)?(?:\.min)?\.js", r"__REACT_DEVTOOLS_GLOBAL_HOOK__")),
    S("js_framework", "Next.js", (r"/_next/static/", r"__NEXT_DATA__")),
    S("js_framework", "Nuxt", (r"/_nuxt/", r"window\.__NUXT__")),
    S("js_framework", "Vue.js", (r"(?<![a-z0-9])vue(?:\.runtime)?(?:\.global)?(?:\.prod)?(?:\.min)?\.js", r"\bdata-v-[0-9a-f]{8}\b", r"__VUE__")),
    S("js_framework", "Angular", (r"\bng-version=", r"<app-root\b")),
    S("js_framework", "AngularJS", (r"/angularjs/\d", r"(?<![a-z0-9-])angular(?:\.min)?\.js", r"\bng-app\s*=")),
    S("js_framework", "Svelte", (r"/_app/immutable/", r"\bsvelte-[a-z0-9]{6,8}\b")),
    S("js_framework", "jQuery", (r"(?<![a-z])jquery[-.]?\d*(?:\.\d+){0,2}(?:\.min)?\.js", r"ajax\.googleapis\.com/ajax/libs/jquery/", r"code\.jquery\.com/jquery"),
      version=(r"jquery[-./@]v?(\d+\.\d+(?:\.\d+)?)", r"jquery[^\"']*\?ver=(\d+\.\d+(?:\.\d+)?)")),
    S("js_framework", "Bootstrap", (r"bootstrap(?:[-.@/]v?\d+(?:\.\d+){0,2})?(?:\.bundle)?(?:\.min)?\.(?:css|js)\b",),
      version=(r"bootstrap[-.@/]v?(\d+\.\d+(?:\.\d+)?)", r"bootstrap/(\d+\.\d+(?:\.\d+)?)/")),
    # --- analytics and tag managers -----------------------------------------------------------------------
    S("analytics", "Google Analytics 4", (r"gtag/js\?id=G-[A-Z0-9]{6,}", r"gtag\(\s*[\"']config[\"']\s*,\s*[\"']G-[A-Z0-9]{6,}"),
      version=(r"(G-[A-Z0-9]{6,12})",)),
    S("analytics", "Google Universal Analytics", (r"[\"']UA-\d{4,10}-\d{1,3}[\"']", r"google-analytics\.com/(?:analytics|ga)\.js"),
      version=(r"(UA-\d{4,10}-\d{1,3})",)),
    S("analytics", "Matomo", (r"matomo\.js", r"piwik\.js", r"_paq\.push\(")),
    S("analytics", "Hotjar", (r"static\.hotjar\.com", r"\bhj\(['\"]|\bhjid\b")),
    S("analytics", "Microsoft Clarity", (r"clarity\.ms/tag/",)),
    S("analytics", "Plausible", (r"plausible\.io/js/",)),
    S("analytics", "Fathom", (r"cdn\.usefathom\.com",)),
    S("analytics", "Adobe Analytics", (r"omtrdc\.net", r"/s_code\.js", r"AppMeasurement")),
    S("analytics", "Yandex Metrica", (r"mc\.yandex\.ru/metrika",)),
    S("tag_manager", "Google Tag Manager", (r"googletagmanager\.com/(?:gtm\.js|ns\.html)\?id=GTM-[A-Z0-9]+",), version=(r"(GTM-[A-Z0-9]+)",)),
    S("tag_manager", "Adobe Launch", (r"assets\.adobedtm\.com",)),
    S("tag_manager", "Tealium", (r"tags\.tiqcdn\.com",)),
    S("tag_manager", "Segment", (r"cdn\.segment\.(?:com|io)/analytics\.js",)),
    S("advertising_tags", "Meta Pixel", (r"connect\.facebook\.net/[a-z_]+/fbevents\.js", r"\bfbq\(['\"]init['\"]")),
    S("advertising_tags", "LinkedIn Insight Tag", (r"snap\.licdn\.com/li\.lms-analytics", r"_linkedin_partner_id")),
    S("advertising_tags", "Google Ads", (r"googleadservices\.com", r"[\"']AW-\d{6,}[\"']")),
    S("advertising_tags", "X (Twitter) Pixel", (r"static\.ads-twitter\.com",)),
    S("advertising_tags", "TikTok Pixel", (r"analytics\.tiktok\.com",)),
    # --- B2B marketing, CRM and visitor identification -----------------------------------------------------
    S("marketing_automation", "HubSpot", (r"js\.hs-scripts\.com", r"js\.hsforms\.net", r"js\.hs-analytics\.net", r"js\.hs-banner\.com", r"forms\.hsforms\.com")),
    S("marketing_automation", "Pardot", (r"pi\.pardot\.com", r"go\.pardot\.com", r"\bpiAId\b")),
    S("marketing_automation", "Marketo", (r"munchkin\.marketo\.net", r"Munchkin\.init", r"\bmktoForms2\b")),
    S("marketing_automation", "ActiveCampaign", (r"trackcmp\.net", r"activehosted\.com")),
    S("marketing_automation", "Mailchimp", (r"list-manage\.com", r"chimpstatic\.com")),
    S("marketing_automation", "Brevo (Sendinblue)", (r"sibautomation\.com", r"sendinblue\.com", r"cdn\.brevo\.com")),
    S("marketing_automation", "Klaviyo", (r"static\.klaviyo\.com",)),
    S("marketing_automation", "Oracle Eloqua", (r"\belqCfg\b", r"img\.en25\.com", r"\belqTrack")),
    S("marketing_automation", "Mautic", (r"/mtc\.js", r"mautic\.net", r"/mautic/")),
    S("marketing_automation", "MailUp", (r"mailup\.com", r"mailupclient")),
    S("marketing_automation", "CleverReach", (r"cleverreach\.com",)),
    S("marketing_automation", "GetResponse", (r"getresponse\.com",)),
    S("marketing_automation", "Zoho Campaigns / Forms", (r"zohopublic\.(?:com|eu)", r"zfrmz\.(?:com|eu)", r"campaigns\.zoho\.")),
    S("marketing_automation", "SharpSpring", (r"sharpspring\.com",)),
    S("crm", "Salesforce", (r"webto\.salesforce\.com", r"[a-z0-9-]+\.force\.com", r"salesforceliveagent")),
    S("crm", "Zoho CRM / SalesIQ", (r"salesiq\.zoho\.", r"crm\.zoho\.", r"zohocdn\.com")),
    S("crm", "Pipedrive", (r"pipedrive\.com/(?:leadbooster|webforms)", r"leadbooster-chat\.pipedrive\.com")),
    S("crm", "Microsoft Dynamics 365", (r"crm\d*\.dynamics\.com", r"d365mktg", r"mktdplp\d+\.microsoftcrmportals\.com")),
    S("crm", "Freshsales", (r"freshsales\.io",)),
    S("lead_intelligence", "Leadfeeder (Dealfront)", (r"lftracker\.leadfeeder\.com", r"ws\.leadfeeder\.com")),
    S("lead_intelligence", "Albacross", (r"serve\.albacross\.com",)),
    S("lead_intelligence", "Leadinfo", (r"cdn\.leadinfo\.net",)),
    S("lead_intelligence", "Lead Forensics", (r"lead-?forensics",)),
    S("lead_intelligence", "ZoomInfo", (r"ws\.zoominfo\.com",)),
    S("lead_intelligence", "6sense", (r"j\.6sc\.co",)),
    S("lead_intelligence", "Clearbit", (r"tag\.clearbitscripts\.com",)),
    # --- customer contact ---------------------------------------------------------------------------------
    S("live_chat", "Intercom", (r"widget\.intercom\.io", r"\bintercomSettings\b")),
    S("live_chat", "Drift", (r"js\.driftt\.com",)),
    S("live_chat", "Tawk.to", (r"embed\.tawk\.to",)),
    S("live_chat", "Zendesk Chat", (r"static\.zdassets\.com", r"v2\.zopim\.com")),
    S("live_chat", "Freshchat", (r"wchat\.freshchat\.com",)),
    S("live_chat", "LiveChat", (r"cdn\.livechatinc\.com",)),
    S("live_chat", "Crisp", (r"client\.crisp\.chat",)),
    S("live_chat", "Tidio", (r"code\.tidio\.co",)),
    S("live_chat", "HubSpot Conversations", (r"js\.usemessages\.com",)),
    S("cookie_consent", "iubenda", (r"cdn\.iubenda\.com", r"iubenda\.com/cs/")),
    S("cookie_consent", "Cookiebot", (r"consent\.cookiebot\.com", r"cookiebot\.com/uc\.js")),
    S("cookie_consent", "OneTrust", (r"cdn\.cookielaw\.org", r"onetrust\.com/consent")),
    S("cookie_consent", "CookieYes / Cookie Law Info", (r"cdn-cookieyes\.com", r"cookie-law-info")),
    S("cookie_consent", "Complianz", (r"/plugins/complianz", r"\bcmplz[-_]")),
    S("cookie_consent", "Usercentrics", (r"app\.usercentrics\.eu", r"usercentrics\.eu/")),
    S("cookie_consent", "Osano", (r"cmp\.osano\.com",)),
    S("cookie_consent", "Termly", (r"app\.termly\.io",)),
    S("cookie_consent", "Borlabs Cookie", (r"borlabs-cookie",)),
    S("cookie_consent", "Cookie Script", (r"cdn\.cookie-script\.com",)),
    S("cookie_consent", "Axeptio", (r"static\.axept\.io",)),
    S("cookie_consent", "Cookie Notice", (r"/plugins/cookie-notice/",)),
    # --- industrial digital product channels --------------------------------------------------------------
    S("cad_portal", "CADENAS PARTcommunity / 3Dfindit", (r"partcommunity\.com", r"3dfindit\.com", r"cadenas\.de")),
    S("cad_portal", "TraceParts", (r"traceparts\.com",)),
    S("cad_portal", "3D ContentCentral", (r"3dcontentcentral\.(?:com|eu)",)),
    S("product_3d_viewer", "Sketchfab", (r"sketchfab\.com/(?:models|oembed)", r"sketchfab\.com/[a-z]+/[a-z0-9-]*embed")),
    S("product_3d_viewer", "model-viewer (3D)", (r"<model-viewer\b", r"model-viewer(?:\.min)?\.js")),
    S("product_3d_viewer", "Three.js (3D)", (r"(?<![a-z0-9])three(?:\.module)?(?:\.min)?\.js",)),
    S("configurator", "Tacton CPQ", (r"tacton\.com", r"tactoncpq")),
    S("configurator", "Configit", (r"configit\.com",)),
    S("configurator", "KBMax", (r"kbmax\.com",)),
    S("configurator", "Threekit", (r"threekit\.com",)),
    S("configurator", "Cincom CPQ", (r"cincom\.com",)),
    S("configurator", "CAMOS", (r"camos\.de",)),
    S("support_portal", "Zendesk Help Center", (r"[a-z0-9-]+\.zendesk\.com",)),
    S("support_portal", "Freshdesk", (r"[a-z0-9-]+\.freshdesk\.com",)),
    S("support_portal", "Help Scout Docs", (r"[a-z0-9-]+\.helpscoutdocs\.com",)),
    S("support_portal", "ServiceNow", (r"[a-z0-9-]+\.service-now\.com",)),
    S("support_portal", "Jira Service Management", (r"[a-z0-9-]+\.atlassian\.net/servicedesk",)),
    S("iot_platform", "PTC ThingWorx", (r"thingworx",)),
    S("iot_platform", "Siemens Insights Hub (MindSphere)", (r"mindsphere\.io", r"insights-hub")),
    S("iot_platform", "Azure IoT Hub", (r"[a-z0-9-]+\.azure-devices\.net",)),
    S("iot_platform", "AWS IoT", (r"iot\.[a-z0-9-]+\.amazonaws\.com",)),
    S("iot_platform", "Ubidots", (r"ubidots\.com",)),
    S("iot_platform", "ThingsBoard", (r"thingsboard",)),
)
_COMPILED: Tuple[Tuple[Sig, Tuple["re.Pattern[str]", ...], Tuple["re.Pattern[str]", ...]], ...] = tuple(
    (s, tuple(re.compile(p, re.I | re.M) for p in s.patterns), tuple(re.compile(v, re.I) for v in s.version))
    for s in SIGNATURES)

# link / anchor-text patterns for portals and configurators that have no vendor script to fingerprint
PORTAL_HREF = re.compile(
    r"<a\b[^>]*\bhref=[\"'](?P<href>[^\"']*(?:area-?riservata|reserved-?area|customer-?portal|portale-?clienti|partner-?portal|dealer-?portal|"
    r"extranet|kundenportal|haendlerbereich|h%C3%A4ndlerbereich|clienti/login|my-?account)[^\"']*)[\"'][^>]*>", re.I)
PORTAL_TEXT = re.compile(
    r"<a\b[^>]*>(?P<text>\s*(?:area\s+riservata|reserved\s+area|customer\s+portal|portale\s+clienti|partner\s+portal|dealer\s+portal|"
    r"extranet|kundenportal|h(?:ä|ae)ndlerbereich)\s*)</a>", re.I)
PORTAL_HOST = re.compile(
    r"<a\b[^>]*\bhref=[\"'](?P<href>https?://(?:portal|extranet|customer|clienti|partner|dealer|rivenditori|b2b|service|support|assistenza|ticket|"
    r"helpdesk|iot|cloud|connect|my)\.[a-z0-9.-]+[^\"']*)[\"'][^>]*>", re.I)
CONFIGURATOR_LINK = re.compile(
    r"<a\b[^>]*\bhref=[\"'][^\"']*(?:configurator|configuratore|konfigurator|product-?selector|selettore-?prodott)[^\"']*[\"'][^>]*>", re.I)
CONFIGURATOR_TEXT = re.compile(
    r"<a\b[^>]*>\s*[^<]{0,40}(?:configuratore|configurator|konfigurator)[^<]{0,40}</a>", re.I)
QUOTE_REQUEST = re.compile(
    r"<a\b[^>]*>\s*(?:richiedi\s+(?:un\s+)?preventivo|richiesta\s+(?:di\s+)?preventivo|request\s+(?:a\s+)?quote|get\s+(?:a\s+)?quote|"
    r"angebot\s+anfordern|anfrage\s+senden)\s*</a>", re.I)
AGENCY_CREDIT = re.compile(
    r"(?:realizzato|sviluppato|creato|progettato|web\s?design|sito\s+web|powered|developed|designed|created|made|website)\s*"
    r"(?:da|by|con|:)?\s*(?:<[^>]+>\s*)*<a\b[^>]*\bhref=[\"'](?P<href>https?://[^\"']+)[\"'][^>]*>\s*(?P<name>[^<]{2,40}?)\s*</a>", re.I)
PLATFORM_WORDS = re.compile(r"wordpress|joomla|drupal|elementor|wix|shopify|woocommerce|squarespace|webflow|typo3|prestashop|magento|"
                            r"bootstrap|themeforest|envato|mailchimp|cloudflare|google|facebook|linkedin", re.I)


def _snippet(raw: str, start: int, end: int, limit: int = 300) -> str:
    """A verbatim piece of the markup around a match: the enclosing tag when the match is inside one and the tag
    is short, otherwise 60 characters either side."""
    lt = raw.rfind("<", max(0, start - 400), start + 1)
    if lt != -1 and raw.find(">", lt, start) == -1:
        gt = raw.find(">", end, end + 400)
        if gt != -1 and gt + 1 - lt <= limit:
            return raw[lt:gt + 1]
    a, b = max(0, start - 60), min(len(raw), end + 60)
    return raw[a:b][:380]


@dataclass
class Detection:
    key: str
    value: str
    quote: str
    where: str            # "raw" or "header"


def _version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def detect_technologies(raw: str, header_text: str = "") -> List[Detection]:
    """Every technology the markup or headers announce, with the verbatim snippet that shows it. One detection per
    (key, value); jQuery and Bootstrap carry their version when the file name or query string has it."""
    found: Dict[Tuple[str, str], Detection] = {}
    for sig, pats, vers in _COMPILED:
        text = raw if sig.where == "raw" else header_text
        if not text:
            continue
        for p in pats:
            m = p.search(text)
            if not m:
                continue
            snip = _snippet(text, m.start(), m.end()) if sig.where == "raw" else _header_line(text, m.start())
            version = ""
            for v in vers:
                vm = v.search(snip) or v.search(text[max(0, m.start() - 200): m.end() + 200])
                if vm:
                    version = vm.group(1)
                    break
            value = f"{sig.name} {version}".strip() if version else sig.name
            found.setdefault((sig.key, value), Detection(sig.key, value, snip, sig.where))
            break
    # a versioned detection supersedes the bare one of the same product (jQuery vs jQuery 3.6.0)
    names = {(d.key, d.value) for d in found.values()}
    for (k, v) in list(found):
        if " " not in v or not re.search(r"\d", v.split(" ")[-1]):
            continue
        base = v.rsplit(" ", 1)[0]
        if (k, base) in names:
            found.pop((k, base), None)
    return sorted(found.values(), key=lambda d: (d.key, d.value))


def _header_line(header_text: str, pos: int) -> str:
    start = header_text.rfind("\n", 0, pos) + 1
    end = header_text.find("\n", pos)
    return header_text[start: end if end != -1 else len(header_text)][:380]


def outdated_from_detections(dets: Sequence[Detection], raw: str) -> List[Tuple[str, str, str]]:
    """[(value, quote, note)] for detected front-end software past its end of life or known-vulnerable."""
    out: List[Tuple[str, str, str]] = []
    for d in dets:
        m = re.match(r"^(jQuery|Bootstrap) (\d+(?:\.\d+){0,2})$", d.value)
        if m:
            name, ver = m.group(1), _version_tuple(m.group(2))
            if name == "jQuery" and ver < (3, 5, 0):
                out.append((f"jQuery {m.group(2)}: versions before 3.5.0 have known cross-site-scripting vulnerabilities",
                            d.quote, "jQuery 3.5.0 (April 2020) fixed CVE-2020-11022 and CVE-2020-11023."))
            if name == "Bootstrap" and ver and ver[0] in (3, 4):
                eol = "July 2019" if ver[0] == 3 else "January 2023"
                out.append((f"Bootstrap {m.group(2)}: the {ver[0]}.x line reached end of life in {eol}", d.quote,
                            "Bootstrap lifecycle as published on getbootstrap.com."))
        if d.value == "AngularJS":
            out.append(("AngularJS (1.x): long-term support ended on 2021-12-31", d.quote,
                        "AngularJS end of life announced by the Angular team (December 2021)."))
    ua = next((d for d in dets if d.value.startswith("Google Universal Analytics")), None)
    modern = any(d.value.startswith(("Google Analytics 4", "Google Tag Manager")) for d in dets)
    if ua and not modern:
        out.append((f"{ua.value}: Universal Analytics stopped processing data on 2023-07-01 (360 properties 2024-07-01)",
                    ua.quote, "Only a Universal Analytics tag is in the page; no GA4 ID and no tag manager that could carry one."))
    flash = re.search(r"[\w/.-]+\.swf\b|application/x-shockwave-flash|swfobject(?:\.js)?", raw, re.I)
    if flash:
        out.append(("Adobe Flash content (end of life 2020-12-31, no longer runs in any browser)",
                    _snippet(raw, flash.start(), flash.end()), "Flash end of life announced by Adobe."))
    return out


# ======================================================================================================
# Site quality
# ======================================================================================================

_MASK = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->|<(?:input|textarea|select|option)\b[^>]*>|<noscript\b.*?</noscript>",
                   re.I | re.S)

PLACEHOLDER_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"lorem ipsum(?: dolor)?", "lorem ipsum"),
    (r"\b(?:john|jane|jon|jack|monica|mary|richard)\s+doe\b", ""),
    (r"\byour\s+(?:company|business|name|title|text|content|logo)(?:\s+name)?\s+here\b", ""),
    (r"\b(?:insert|add|enter|write)\s+your\s+(?:text|content|title|description)\s+here\b", ""),
    (r"\bsample\s+text\s+goes\s+here\b", ""),
    (r"\btesto\s+di\s+(?:esempio|prova)\b", ""),
    (r"\binserisci\s+(?:qui\s+)?(?:il\s+)?(?:tuo\s+)?testo\b", ""),
    (r"\bil\s+tuo\s+testo\s+qui\b", ""),
    (r"\btitolo\s+(?:di\s+)?esempio\b", ""),
    (r"\bclick\s+here\s+to\s+edit\b|\bclicca\s+qui\s+per\s+modificare\b", ""),
    (r"\bjust\s+another\s+wordpress\s+site\b|\bun\s+altro\s+sito\s+wordpress\b", "default WordPress tagline"),
    (r">\s*hello\s+world!\s*<", "default WordPress post \"Hello world!\""),
    (r"\bsito\s+in\s+costruzione\b|\bsite\s+under\s+construction\b|\bwebsite\s+under\s+construction\b|\bpagina\s+in\s+costruzione\b", ""),
    (r"\bwelcome\s+to\s+nginx!", "default nginx server page"),
    (r"\bapache2\s+(?:ubuntu|debian)\s+default\s+page\b", "default Apache server page"),
    (r"<title>\s*(?:IIS\s+Windows\s+Server|IIS7|Welcome\s+to\s+IIS|Default\s+Web\s+Site(?:\s+Page)?)\s*</title>", "default IIS server page"),
    (r"<h1>\s*it\s+works!\s*</h1>", "default Apache server page"),
    (r"\bthis\s+domain\s+(?:name\s+)?(?:may\s+be|is)\s+for\s+sale\b|\bquesto\s+dominio\s+(?:è|e')\s+in\s+vendita\b|\bdomain\s+is\s+parked\b", "parked domain page"),
    (r"[\w.+-]+@(?:example\.(?:com|org|net)|domain\.(?:com|it)|yourdomain\.[a-z]+|yoursite\.[a-z]+)\b", "example e-mail address"),
    (r"\b123[\s.-]456[\s.-]7890\b", "example phone number"),
)
_PLACEHOLDER_RE = tuple((re.compile(p, re.I), label) for p, label in PLACEHOLDER_PATTERNS)
COUNTER_RE = re.compile(
    r"<(?P<tag>\w+)\b[^>]*\bdata-(?:to|count|target|end|number)=[\"']?(?P<n>\d[\d.,]*)[\"']?[^>]*>\s*0\s*</(?P=tag)\s*>", re.I)
COUNTER_LIBS = re.compile(r"counterup|countup|purecounter|odometer|data-plugin-counter|\bwaypoints?\b|jquery\.countto|numscroller", re.I)
DEMO_FILE = re.compile(r"(?:href|src)=[\"'](?P<u>[^\"']*/demos?/demo-[\w-]+\.(?:css|js))[\"']", re.I)
NOINDEX = re.compile(r"<meta\b[^>]*name=[\"']robots[\"'][^>]*content=[\"'][^\"']*noindex[^\"']*[\"'][^>]*>", re.I)
COPYRIGHT = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\(c\)\s*)?(?P<y1>(?:19|20)\d{2})(?:\s*[-–/]\s*(?P<y2>(?:19|20)\d{2}))?", re.I)
DYNAMIC_YEAR = re.compile(r"getFullYear|date\(['\"]Y['\"]\)|\{\{\s*year|now\(\)\.year|DateTime\.(?:Now|Today)\.Year", re.I)


def _masked_spans(raw: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MASK.finditer(raw)]


def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def find_placeholders(raw: str) -> List[Tuple[str, str, str]]:
    """[(what, where, snippet)] for template text left on a page. Scripts, styles, comments and form-field hints
    are ignored, so a demo string inside a script template or an input's placeholder attribute does not count."""
    spans = _masked_spans(raw)
    out: List[Tuple[str, str, str]] = []
    for rx, label in _PLACEHOLDER_RE:
        for m in rx.finditer(raw):
            if _in_spans(m.start(), spans):
                continue
            snip = _snippet(raw, m.start(), m.end())
            inside_tag = raw.rfind("<", 0, m.start()) > raw.rfind(">", 0, m.start())
            what = label or re.sub(r"\s+", " ", m.group(0)).strip(" <>")
            inside_tag = inside_tag and not m.group(0).startswith(">")
            out.append((what, "alt or title attribute" if inside_tag else "visible text", snip))
    return out


def find_stuck_counters(raw: str) -> Optional[Tuple[List[str], str]]:
    """(targets, snippet) when the page ships two or more statistic counters rendered as 0 although they carry a
    positive target, and no counter script or initialiser attribute is present in the page."""
    if COUNTER_LIBS.search(raw):
        return None
    hits = [m for m in COUNTER_RE.finditer(raw) if float(m.group("n").replace(",", "").replace(".", "") or 0) > 0]
    if len(hits) < MIN_COUNTERS_FOR_ISSUE:
        return None
    return [h.group("n") for h in hits], _snippet(raw, hits[0].start(), hits[0].end())


def find_stale_copyright(text: str, raw: str, now: datetime) -> Optional[Tuple[int, str]]:
    """(newest copyright year, quote) when the footer year is STALE_COPYRIGHT_YEARS or more behind; None when the
    year is written by a script (we cannot tell what it renders) or the page has no copyright line."""
    best = None
    for m in COPYRIGHT.finditer(text):
        year = int(m.group("y2") or m.group("y1"))
        if best is None or year > best[0]:
            best = (year, m.group(0))
    if best is None:
        return None
    if DYNAMIC_YEAR.search(raw):
        return None
    if best[0] > now.year - STALE_COPYRIGHT_YEARS:
        return None
    return best


STOPWORDS = {
    "it": "il lo gli dei delle degli della nel nella sono che per del alla più come anche questo questa nostra nostri nostre dal sul sulla tutti tra non",
    "en": "the and of to is for with that this are our from your by as at be or we you which their has have more",
    "de": "der die das und ist nicht mit für von auf den dem des ein eine wir sie auch sich bei nach über zu im werden sind unsere",
    "fr": "les des est une pour dans qui que sur avec nous vous pas du au aux sont ses cette",
    "es": "los las por para nuestro nuestra sus este esta más está también nuestros nuestras empresa",
}
_STOP = {lang: frozenset(words.split()) for lang, words in STOPWORDS.items()}


def language_profile(text: str) -> Dict[str, int]:
    """Stop-word hits per language (it, en, de, fr, es). A crude detector: it is used only with thresholds."""
    hits = {lang: 0 for lang in _STOP}
    for w in re.findall(r"[a-zàèéìòùäöüßñç]+", text.lower()):
        for lang, words in _STOP.items():
            if w in words:
                hits[lang] += 1
    return hits


def mixed_languages(text: str) -> Optional[Tuple[str, str, float]]:
    prof = language_profile(text)
    total = sum(prof.values())
    if total < MIXED_LANGUAGE_MIN_WORDS:
        return None
    (l1, h1), (l2, h2) = sorted(prof.items(), key=lambda kv: -kv[1])[:2]
    share = h2 / total
    return (l1, l2, share) if share >= MIXED_LANGUAGE_MIN_SHARE else None


# ======================================================================================================
# Social accounts
# ======================================================================================================

SOCIAL_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("linkedin", re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|school|showcase)/[^/?#\"'\s<>\\]+", re.I)),
    ("facebook", re.compile(r"https?://(?:www\.|[a-z]{2}-[a-z]{2}\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr[?/]|login|policies)"
                            r"(?:pages/[^/?#\"'\s<>\\]+/\d+|profile\.php\?id=\d+|[A-Za-z0-9.\-]{3,})", re.I)),
    ("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/(?!p/|explore|accounts|reel/|stories/)[A-Za-z0-9_.]{2,30}", re.I)),
    ("youtube", re.compile(r"https?://(?:www\.)?youtube\.com/(?:channel/UC[\w-]{20,}|c/[^/?#\"'\s<>\\]+|@[\w.\-]+|user/[^/?#\"'\s<>\\]+)", re.I)),
    ("x", re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|home|i/|search|hashtag)[A-Za-z0-9_]{2,15}(?=[\"'/?#\s<>\\])", re.I)),
    ("xing", re.compile(r"https?://(?:www\.)?xing\.com/(?:pages|companies)/[^/?#\"'\s<>\\]+", re.I)),
    ("tiktok", re.compile(r"https?://(?:www\.)?tiktok\.com/@[\w.]+", re.I)),
)
_SOCIAL_SKIP_FB = {"tr", "sharer.php", "share.php", "plugins", "dialog", "login.php", "policies"}


def find_social_accounts(raw: str) -> List[Tuple[str, str, str]]:
    """[(network, canonical url, snippet)] one per distinct account the markup links or declares (sameAs)."""
    out: Dict[Tuple[str, str], Tuple[str, str, str]] = {}
    for network, rx in SOCIAL_PATTERNS:
        for m in rx.finditer(raw):
            url = m.group(0).rstrip("/\\").split("?")[0] if "profile.php" not in m.group(0) else m.group(0)
            low = url.lower().rstrip("/")
            if network == "facebook" and low.rsplit("/", 1)[-1] in _SOCIAL_SKIP_FB:
                continue
            canon = re.sub(r"^https?://(?:www\.|[a-z]{2,3}\.|[a-z]{2}-[a-z]{2}\.)?", "https://", low)
            out.setdefault((network, canon), (network, url, _snippet(raw, m.start(), m.end())))
    return sorted(out.values())


# ======================================================================================================
# Internal pages
# ======================================================================================================

_SKIP_EXT = re.compile(r"\.(?:pdf|jpe?g|png|gif|svg|webp|ico|zip|rar|7z|docx?|xlsx?|pptx?|mp[34]|avi|mov|xml|css|js|json|txt)(?:$|\?)", re.I)
_PRIORITY = ("chi-siamo", "azienda", "company", "about", "storia", "history", "profilo", "contatt", "contact", "kontakt", "prodott",
             "product", "servizi", "service", "assistenza", "support", "download", "news", "notizie", "carrier", "career", "lavora",
             "qualit", "certific", "team", "applicaz", "application", "settori", "sector", "mercati", "referenz", "reference",
             "client", "customer")


def pick_internal_links(home: Page, limit: int) -> List[str]:
    """Internal links the homepage itself offers, the informative ones first, at most `limit`, no duplicates."""
    host = registrable_host(home.url)
    seen, scored = {urldefrag(home.url)[0].rstrip("/")}, []
    for order, link in enumerate(home.links):
        link = urldefrag(link)[0]
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or registrable_host(link) != host or _SKIP_EXT.search(parsed.path or ""):
            continue
        key = (parsed.path.rstrip("/") + ("?" + parsed.query if parsed.query else "")).lower() or "/"
        if key in seen or key == "/":
            continue
        seen.add(key)
        score = sum(1 for w in _PRIORITY if w in key)
        scored.append((-score, order, link))
    return [link for _, _, link in sorted(scored)[:limit]]


def _path(url: str) -> str:
    p = urlparse(url)
    return (p.path or "/") + (("?" + p.query) if p.query else "")


# ======================================================================================================
# Domain lookups
# ======================================================================================================

SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}


def registrable_domain(host: str) -> str:
    """The registrable domain of a host without a public-suffix list: the last two labels, three when the TLD is a
    two-letter country code behind co / com / org / net / gov / ac / edu (co.uk, com.au, com.tr ...)."""
    labels = [l for l in host.lower().split(".") if l]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


RDAP_REGISTRATION = re.compile(r"\{[^{}]*\"eventAction\"\s*:\s*\"registration\"[^{}]*\}", re.I)
RDAP_DATE = re.compile(r"\"eventDate\"\s*:\s*\"(\d{4}-\d{2}-\d{2})")


def site_mentions_company(company: Dict[str, object], text: str) -> bool:
    """True when the page names the company (legal name or a brand token) or carries its VAT number."""
    low = (text or "").lower()
    name = str(company.get("legal_name") or "").lower()
    vat = normalize_vat(str(company.get("vat") or company.get("registration_number") or ""))
    if name and name in low:
        return True
    if any(tok in low for tok in extract_brand_tokens(name)):
        return True
    return bool(vat and len(vat) >= 7 and vat in (text or ""))


# ======================================================================================================
# The collector
# ======================================================================================================

def run(ctx: CollectorContext, now: Optional[datetime] = None) -> CollectorResult:
    result = CollectorResult()
    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    home, asked_url = fetch_homepage(ctx, result)
    if home is None:
        return result
    if not home.ok:
        result.errors.append(f"homepage not readable ({home.note or 'HTTP ' + str(home.status)}); nothing to fingerprint")
        return result
    if not home.raw:
        result.errors.append("the fetcher returned no page markup (Page.raw is empty): markup fingerprints skipped")
        return result
    sink = FactSink(ctx, result, COLLECTOR_TAG, anchor_text=home.text or "")

    # ---- read the homepage plus a bounded sample of the pages it links to ----------------------------
    pages: List[Page] = [home]
    budget_pages = max(1, min(MAX_SAMPLED_PAGES + 1, ctx.budget.max_pages))
    for link in pick_internal_links(home, budget_pages - 1):
        if time.monotonic() - started > ctx.budget.deadline_seconds * 0.6:
            result.errors.append("time budget reached while reading internal pages; the rest were skipped")
            break
        sub = ctx.fetch(link)
        result.pages_fetched += 1
        pages.append(sub)
    readable = [p for p in pages if p.ok and p.raw]
    ctx.log(f"homepage {home.url}: read {len(readable)} page(s) of {len(pages)} requested")

    # ---- technologies ---------------------------------------------------------------------------------
    header_text = header_block(home.headers) if home.headers else ""
    all_dets: List[Tuple[Detection, Page]] = []
    for pg in readable:
        for d in detect_technologies(pg.raw, header_block(pg.headers) if pg.headers else ""):
            all_dets.append((d, pg))
    emitted_keys = set()
    first_for: Dict[Tuple[str, str], Tuple[Detection, Page]] = {}
    for d, pg in all_dets:
        first_for.setdefault((d.key, d.value), (d, pg))
    for (key, value), (d, pg) in sorted(first_for.items()):
        src = header_block(pg.headers) if d.where == "header" else pg.raw
        if sink.add(f"digital.{key}", value, quote=d.quote, source_text=src, url=pg.url, confidence=0.9,
                    content_hash=pg.content_hash, title="page markup" if d.where == "raw" else "HTTP response headers"):
            emitted_keys.add(f"digital.{key}")
    homepage_dets = [d for d, pg in all_dets if pg is home]
    for value, quote, note in outdated_from_detections(homepage_dets, home.raw):
        if sink.add("digital.outdated_software", value, quote=quote, source_text=home.raw, url=home.url, confidence=0.85,
                    notes=note, content_hash=home.content_hash, title="page markup"):
            emitted_keys.add("digital.outdated_software")

    # ---- portals, configurators, quote requests, web agency (link patterns) ---------------------------
    for key, label, rx in (("customer_portal", "reserved area or customer portal", PORTAL_HREF),
                           ("customer_portal", "reserved area or customer portal", PORTAL_TEXT),
                           ("customer_portal", "portal on its own sub-domain", PORTAL_HOST),
                           ("configurator", "product configurator link", CONFIGURATOR_LINK),
                           ("configurator", "product configurator link", CONFIGURATOR_TEXT),
                           ("quote_request", "request-a-quote link", QUOTE_REQUEST)):
        done = False
        for pg in readable:
            site_domain = registrable_domain(registrable_host(pg.url))
            for m in rx.finditer(pg.raw):
                ref = m.groupdict().get("href") or re.sub(r"<[^>]+>", "", m.group(0)).strip()
                if rx is PORTAL_HOST and registrable_domain((urlparse(ref).netloc or "").lower()) != site_domain:
                    continue   # support.google.com and the like are not the company's own portal
                ref = urldefrag(ref)[0][:120]
                if sink.add(f"digital.{key}", f"{label}: {ref}", quote=_snippet(pg.raw, m.start(), m.end()), source_text=pg.raw,
                            url=pg.url, confidence=0.8, content_hash=pg.content_hash, title="page markup"):
                    emitted_keys.add(f"digital.{key}")
                done = True
                break
            if done:
                break
    for pg in readable:
        author = re.search(r"<meta\b[^>]*name=[\"']author[\"'][^>]*content=[\"']([^\"']{2,60})[\"'][^>]*>", pg.raw, re.I)
        credit = AGENCY_CREDIT.search(pg.raw)
        added = None
        if author and not PLATFORM_WORDS.search(author.group(1)) and author.group(1).strip().lower() not in (
                str(ctx.company.get("legal_name") or "").lower()):
            added = sink.add("digital.web_agency", author.group(1).strip(), quote=_snippet(pg.raw, author.start(), author.end()),
                             source_text=pg.raw, url=pg.url, confidence=0.7, content_hash=pg.content_hash,
                             title="meta author tag", notes="From the page's author meta tag; may be a person or the company itself.")
        elif credit and not PLATFORM_WORDS.search(credit.group("name")):
            added = sink.add("digital.web_agency", credit.group("name").strip(), quote=_snippet(pg.raw, credit.start(), credit.end()),
                             source_text=pg.raw, url=pg.url, confidence=0.7, content_hash=pg.content_hash, title="footer credit")
        if added:
            emitted_keys.add("digital.web_agency")
            break

    # ---- site quality ---------------------------------------------------------------------------------
    issue_counts: Dict[str, int] = {}

    def issue(kind: str, value: str, pg: Page, quote: str, source: str, confidence: float, notes: Optional[str] = None) -> None:
        if issue_counts.get(kind, 0) >= MAX_ISSUE_FACTS_PER_KIND:
            return
        if sink.add("digital.quality_issue", value, quote=quote, source_text=source, url=pg.url, confidence=confidence,
                    notes=notes, content_hash=pg.content_hash, title="page markup"):
            issue_counts[kind] = issue_counts.get(kind, 0) + 1
            emitted_keys.add("digital.quality_issue")

    for pg in readable:
        ph = find_placeholders(pg.raw)
        if ph:
            labels = sorted({w for w, _, _ in ph})
            where = sorted({wh for _, wh, _ in ph})
            issue("placeholder", f"placeholder or template text on {_path(pg.url)}: " + ", ".join(f'"{l}"' for l in labels[:4])
                  + f" ({', '.join(where)})", pg, ph[0][2], pg.raw, 0.9,
                  "Leftover template text; an alt attribute is invisible to visitors but shows an unmanaged page.")
        stuck = find_stuck_counters(pg.raw)
        if stuck:
            targets, snip = stuck
            issue("counters", f"statistic counters ship as 0 on {_path(pg.url)} (targets {', '.join(targets[:4])}; "
                  "no counter script or initialiser attribute in the page)", pg, snip, pg.raw, 0.5,
                  "Static markup only: a counter driven by a script the page does not reveal would animate to its target. "
                  "Confirm visually.")
        demos = sorted({m.group("u") for m in DEMO_FILE.finditer(pg.raw)})
        if demos and pg is home:
            m = DEMO_FILE.search(pg.raw)
            issue("template", "template demo files still linked on the homepage: " + ", ".join(demos[:3]), pg,
                  _snippet(pg.raw, m.start(), m.end()), pg.raw, 0.75,
                  "Demo stylesheets of a purchased theme left in production suggest the site was installed, not designed.")
        if pg is home:
            ni = NOINDEX.search(pg.raw)
            if ni:
                issue("noindex", "homepage asks search engines not to index it (meta robots noindex)", pg, ni.group(0), pg.raw, 0.9)
        mixed = mixed_languages(pg.text or "")
        if mixed and pg.text:
            l1, l2, share = mixed
            issue("mixed_language", f"two languages mixed on {_path(pg.url)}: {l1} and {l2} ({share:.0%} of recognisable words)",
                  pg, quote_prefix_of(pg.text), pg.text, 0.6,
                  "Stop-word estimate; a deliberately bilingual page is possible.")
    stale = find_stale_copyright(home.text or "", home.raw, now)
    if stale:
        year, quote = stale
        issue("copyright", f"footer copyright year {year} on {_path(home.url)} ({now.year - year} years behind)", home, quote,
              home.text, 0.7, "Not conclusive alone: some sites never update the footer year.")
    broken = _broken_links(home, pages[1:])
    for url, status, snip in broken[:MAX_ISSUE_FACTS_PER_KIND]:
        issue("broken_link", f"broken internal link {_path(url)} (HTTP {status}) linked from {_path(home.url)}", home, snip,
              home.raw, 0.9)

    # ---- languages declared by the site --------------------------------------------------------------
    langs: Dict[str, str] = {}
    for m in re.finditer(r"<html\b[^>]*\blang=[\"']?([a-zA-Z]{2,3})(?:-[a-zA-Z]+)?[\"']?[^>]*>", home.raw, re.I):
        langs.setdefault(m.group(1).lower(), _snippet(home.raw, m.start(), m.end()))
    for m in re.finditer(r"<link\b[^>]*hreflang=[\"']([a-zA-Z]{2,3})(?:-[a-zA-Z]+)?[\"'][^>]*>", home.raw, re.I):
        langs.setdefault(m.group(1).lower(), _snippet(home.raw, m.start(), m.end()))
    for lang, snip in sorted(langs.items()):
        if lang != "x" and sink.add("digital.site_language", lang, quote=snip, source_text=home.raw, url=home.url,
                                    confidence=0.9, content_hash=home.content_hash, title="page markup"):
            emitted_keys.add("digital.site_language")

    # ---- social accounts ------------------------------------------------------------------------------
    socials: Dict[Tuple[str, str], Tuple[str, str, Page]] = {}
    for pg in readable:
        for network, url, snip in find_social_accounts(pg.raw):
            socials.setdefault((network, url.lower()), (url, snip, pg))
    for (network, _), (url, snip, pg) in sorted(socials.items()):
        if sink.add(f"digital.social_{network}", url, quote=snip, source_text=pg.raw, url=pg.url, confidence=0.9,
                    content_hash=pg.content_hash, title="page markup"):
            emitted_keys.add(f"digital.social_{network}")
    for network, url in sorted((n, u) for (n, _), (u, _, _) in socials.items() if n == "youtube"):
        _youtube_activity(ctx, sink, url, readable)
        break

    # ---- domain facts (only when the homepage really names this company) ------------------------------
    if site_mentions_company(ctx.company, home.text or ""):
        _domain_facts(ctx, sink, result, home)
    else:
        result.errors.append("the homepage does not name the company (name, brand or VAT): domain facts withheld")

    # ---- what was checked and not found ---------------------------------------------------------------
    for key in ("cms", "ecommerce_platform", "analytics", "tag_manager", "marketing_automation", "crm", "live_chat",
                "cookie_consent", "cad_portal", "configurator", "customer_portal", "quality_issue", "outdated_software",
                "social_linkedin", "social_facebook", "social_instagram", "social_youtube"):
        if f"digital.{key}" not in emitted_keys:
            result.checked_absent.append(f"digital.{key}")
    if sink.dropped:
        ctx.log(f"{sink.dropped} fact(s) dropped by the quote check or the verification gate")
    return result


def quote_prefix_of(text: str, limit: int = 300) -> str:
    """The first sentence-sized stretch of text, verbatim."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:limit]


def _broken_links(home: Page, subpages: Sequence[Page]) -> List[Tuple[str, int, str]]:
    """Internal links the homepage offers that answered 404 or 410 (a definite 'gone'); robots blocks, timeouts and
    403 are not broken links and are ignored. Returns [(url, status, anchor snippet)]."""
    out = []
    for sub in subpages:
        if sub is None or sub.ok or sub.status not in (404, 410):
            continue
        path = urlparse(sub.url).path
        m = re.search(r"<a\b[^>]*\bhref=[\"'][^\"']*" + re.escape(path.rstrip("/")) + r"/?[\"'][^>]*>", home.raw, re.I) if path else None
        if m:
            out.append((sub.url, sub.status, _snippet(home.raw, m.start(), m.end())))
    return out


def _domain_facts(ctx: CollectorContext, sink: FactSink, result: CollectorResult, home: Page) -> None:
    domain = registrable_domain(registrable_host(home.url))
    if not domain or ctx.fetch is None:
        return
    # registration date: RDAP through the public bootstrap redirector; only TLDs that publish RDAP answer
    rdap = ctx.fetch(f"https://rdap.org/domain/{domain}")
    result.pages_fetched += 1
    if rdap is not None and rdap.ok and rdap.raw:
        ev = RDAP_REGISTRATION.search(rdap.raw)
        d = RDAP_DATE.search(ev.group(0)) if ev else None
        if ev and d:
            sink.add("digital.domain_registered_on", d.group(1), quote=ev.group(0), source_text=rdap.raw, url=rdap.url,
                     grade="A", source_type="public_register", confidence=0.95, as_of=d.group(1),
                     content_hash=rdap.content_hash, title=f"RDAP record of {domain}")
    elif rdap is not None and rdap.status == 404:
        result.errors.append(f"no RDAP record for {domain} (the TLD publishes none, e.g. .it, .de, .eu, .ch, .at): "
                             "registration date unknown, not absent")
    # first capture of the site in the Internet Archive
    cdx = ctx.fetch(f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&limit=1&fl=timestamp&filter=statuscode:200")
    result.pages_fetched += 1
    if cdx is not None and cdx.ok and cdx.raw:
        m = re.search(r"\[\s*\"(\d{14})\"\s*\]", cdx.raw)
        if m:
            ts = m.group(1)
            first = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
            sink.add("digital.wayback_first_seen", first, quote=m.group(0), source_text=cdx.raw, url=cdx.url, grade="C",
                     source_type="public_register", confidence=0.9, as_of=first, content_hash=cdx.content_hash,
                     title=f"Internet Archive capture index of {domain}",
                     notes="Earliest capture with HTTP 200; the site existed no later than this date.")


def _youtube_activity(ctx: CollectorContext, sink: FactSink, channel_url: str, readable: Sequence[Page]) -> None:
    """Last public upload of the company's YouTube channel, from the channel's own public feed (no login)."""
    if ctx.fetch is None:
        return
    m = re.search(r"/channel/(UC[\w-]{20,})", channel_url)
    channel_id = m.group(1) if m else None
    if not channel_id:
        page = ctx.fetch(channel_url)
        if page is not None and page.ok and page.raw:
            cm = re.search(r"\"channelId\"\s*:\s*\"(UC[\w-]{20,})\"|channel/(UC[\w-]{20,})", page.raw)
            channel_id = (cm.group(1) or cm.group(2)) if cm else None
    if not channel_id:
        return
    feed = ctx.fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    if feed is None or not feed.ok or not feed.raw:
        return
    dates = sorted(re.findall(r"<published>(\d{4}-\d{2}-\d{2})T[^<]*</published>", feed.raw))
    if dates:
        last = dates[-1]
        quote = re.search(r"<published>" + re.escape(last) + r"T[^<]*</published>", feed.raw).group(0)
        sink.add("digital.social_youtube_last_upload", last, quote=quote, source_text=feed.raw, url=feed.url, grade="B",
                 source_type="social", confidence=0.9, as_of=last, content_hash=feed.content_hash,
                 title="YouTube channel feed (public)")


COLLECTOR = Collector(
    name="tech-stack", ticket="I18", produces=("digital.",), requires=("web",), refresh_days=120, cost_hint="moderate",
    description="Technology fingerprints (CMS, e-commerce, analytics, marketing and CRM tags, portals, configurators), "
                "outdated software, site-quality defects, social accounts and domain age, from the homepage, a bounded "
                "sample of its own internal pages and public domain lookups.",
    run=run,
)
