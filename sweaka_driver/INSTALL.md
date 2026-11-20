# Sweaka Driver Connect for Odoo — install & exposure guide (T9.6.9)

Supported: Odoo **17, 18, 19, 20** Community or Enterprise, self-hosted or **Odoo.sh**. Not possible on **Odoo Online**
(SaaS forbids custom modules) → use the Sweaka Bridge (`odoo_rpc` adapter, T9.8.1) instead.

## 1. Install the module
1. Get the zip for your Odoo series: `sweaka_driver-<series>.0.<version>.zip` (Apps store or direct download).
   Direct downloads come with `SHA256SUMS`, one `.sig` per file and `sweaka-release-pub.pem`; check before installing:
   `openssl pkeyutl -verify -pubin -inkey sweaka-release-pub.pem -rawin -in <zip> -sigfile <zip>.sig`.
   From source: `python plugins/odoo/build.py --series 20 --out dist --zip`.
2. Unzip into an addons path (Odoo.sh: commit it into your repository).
3. Restart Odoo, *Apps → Update Apps List*, install **Sweaka Driver Connect** (needs Sales + Inventory + Invoicing).
4. Nothing to pip-install: the Sweaka core is bundled in `sweaka_driver/lib/`. The server QR code uses `qrcode`,
   already an Odoo dependency (without it the code is shown as text).

## 2. Configure
- **Company** (*Settings → Companies* → company → tab *Sweaka Driver*):
  - *Drivers may sell on credit*, *Max driver discount (%)*, *Driver must record GPS*, *Drivers may sign out*;
  - *Delivery proof* (none / photo / signature / both) and *Keep delivery proofs (days)* (default 365, purged daily);
  - *Driver app: scan to connect*: the QR code drivers scan (needs a public `https://` URL, see §3; otherwise the
    problem is shown in its place);
  - *Sweaka licence* (administrators): company code + plugin secret from the Sweaka licensing portal. The driver list
    is pushed daily and whenever a driver is added or removed; *Sync drivers now* forces it.
- **Drivers** (*Settings → Users* → user → tab *Sweaka Driver*): tick *Sweaka driver* and choose the **van stock
  location** (an internal location, one per van). The user's company = tenant shown in the app. The tab also shows the
  driver's **handover code**.
- **Prices**: drivers sell at the product sale price; a fixed-price item of a customer's pricelist overrides it for that
  customer (pricelist = customer category in the app). Customer *tags* = zones.
- **Payments**: needs a *Cash* journal (else a Bank journal) in the company; otherwise sales with cash collected and
  payments answer 503 `not_configured`.
- **GPS retention**: system parameter `sweaka_driver.gps_retention_days` (default 60 days); a daily job purges older
  points.

## 3. Expose the API to phones (HTTPS required)
The app talks to `https://<your-odoo>/sweaka/driver/v1/...`; the **server URL** to give drivers (QR / manual entry) is
`https://<your-odoo>/sweaka/driver`. The QR code is built from `web.base.url`: make sure it is the public https URL
(set it and `web.base.url.freeze = True` in *System Parameters* when Odoo sits behind a proxy).
- **Odoo.sh / public HTTPS Odoo**: nothing else to do.
- **Self-hosted behind nginx / Caddy**: keep TLS at the proxy, start Odoo with `--proxy-mode`, and make sure the
  proxy forwards `/sweaka/driver/` (no auth wall, no caching, body size ≥ 6 MB: delivery photos are up to 5 MB). Example (nginx):
  `location /sweaka/driver/ { proxy_pass http://odoo:8069; proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header X-Forwarded-Host $host; proxy_read_timeout 60s; client_max_body_size 10m; }`
- **Odoo on a LAN only**: publish it through a tunnel (Cloudflare Tunnel, Tailscale Funnel) or a VPN the phones join.
  Never expose plain HTTP: tokens would travel in clear.
- **Multi-database servers**: set `dbfilter` so the host resolves to one database (`--db-filter=^%d$`); the
  routes are `auth="none"` and need a single database per host.

## 4. Check
`curl https://<your-odoo>/sweaka/driver/v1/meta` → JSON with `"server_kind": "odoo"` and the capability flags.
Then log in from the app with the driver's Odoo login + password (or scan the QR code first).

## 5. Daily back office (*Inventory → Sweaka Driver*)
| Menu | Who | What |
|---|---|---|
| Drivers | Inventory / Accounting managers | van location + **handover code** to give face to face; the driver types it to validate a van load or close the day; it changes after each use, 5 wrong tries in 15 min lock it for a while |
| Stock losses | Inventory managers | driver-declared losses (*To review*): **Validate** takes the goods out of the van (scrap; Odoo 20: move to the inventory-loss location), **Reject** leaves stock untouched |
| Cash handovers | Accounting managers | cash the driver hands over: **Validate** = received (leaves the driver's cash), **Reject** otherwise |
| Day closes | Inventory managers | read-only snapshot of the van stock when the driver closed the day |
| GPS points | Inventory managers | driver tracks (purged after the retention days) |

Other driver documents are ordinary Odoo documents:
- **Van loads** = internal transfers from the warehouse into the driver's van location: confirm them; the driver
  validates on arrival with the handover code (the app is notified when one is ready).
- **Orders** = quotations tagged as driver orders: *Confirm* = approved (the driver is notified), *Cancel* = cancelled;
  their deliveries appear in the driver's route and are validated from the app (no backorder; proofs attached).
- **Van sales / payments / returns** = confirmed sale orders + posted invoices, posted payments, posted credit notes.

## Scheduled jobs (daily)
*Sweaka driver: purge old GPS points*, *purge old delivery proofs*, *sync drivers to Sweaka licensing*.

## Odoo Online limitation
No custom modules, no custom HTTP routes → this module cannot run there. The Sweaka Bridge connects to Odoo Online with
an API key (XML-RPC/JSON-RPC) and serves the same driver API (T9.8.1).
