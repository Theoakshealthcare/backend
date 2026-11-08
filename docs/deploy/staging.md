# Staging deploy runbook (first deploy on a fresh Ubuntu VPS)

**Status: prepared, not run on a server.** Nothing here has touched a real VPS, domain, mail provider, Paystack, Dojah or Expo account. Section 16 lists exactly what was checked offline and what was not. Everything is written so the first deploy is a short checklist once the owner has a server.

The stack: **api** (FastAPI behind gunicorn), **worker** (ARQ: dispatch, payouts, refunds, KYC, voice notes), **Postgres 16**, **Redis 7**, plus **nginx + Let's Encrypt** on the host. The image is Python **3.13** (`.python-version`, `Dockerfile`): **do not bump it to 3.14**, `arq` 0.26.3 breaks there ("There is no current event loop").

## 0. What I need from the owner

| # | Decision / item | Recommendation | Needed by |
|---|---|---|---|
| 1 | **Domain** (and who controls its DNS) | A subdomain you already own, e.g. `api.staging.<yourdomain>` for the API. Tell me the domain; I replace `staging.example.com` everywhere. | before section 2 |
| 2 | **VPS provider and plan** | **2 vCPU / 4 GB RAM / 40+ GB SSD, Ubuntu 24.04 LTS.** For staging, take the cheapest reputable EU region (London or Frankfurt): the 20-40 ms you might gain from a Johannesburg or Lagos host does not matter for testing, and the price does. Roughly US$5-12 a month at the large EU providers (check current prices). Practical blocker in Nigeria: most providers want an international (dollar) card or PayPal; a virtual dollar card works. Do not pick a plan below 4 GB (see section 1). **Decide the production region later, with a measurement:** from Lagos run `curl -o /dev/null -s -w '%{time_connect} %{time_total}\n' https://api.<domain>/health` against staging and against a trial server in Johannesburg/Cape Town. | before section 3 |
| 3 | **SMTP provider** (sign-up codes, password-reset codes) | **Zoho Mail SMTP** (`smtp.zoho.com`, port **465**) for staging if the Oakly domain is already on Zoho: no new account. SES is the better production choice (cheap, high volume, but needs a verified domain and a request to leave the sandbox). Resend is the quickest to set up. **Hard requirement from the code: the provider must accept implicit TLS on port 465** (`app/services/email.py` uses `SMTP_SSL`; STARTTLS on 587 does not work). | before section 7 |
| 4 | **Sender address** | `noreply@<yourdomain>`, with SPF and DKIM set up at the provider or the codes land in spam. | with item 3 |
| 5 | **Support email** | one address, used as the Let's Encrypt contact and as `KYC_ALERT_EMAIL` | section 6 |
| 6 | **Paystack TEST keys** | Paystack dashboard, switch to **Test mode**, Settings > API Keys & Webhooks: `sk_test_...` and `pk_test_...` | section 7 |
| 7 | **Dojah sandbox values** | App id, public key, **secret** key, widget id (all `test_...`), and a webhook secret you choose | section 7 |
| 8 | **Gemini API key** | Google AI Studio key (FallGuard advice, later voice notes) | section 7 |
| 9 | **Cloudinary** (staging account or folder) | cloud name, API key, API secret (profile photos) | section 7 |
| 10 | **First price list** | `base_fee` (naira per hour), `platform_fee_percent`, optional per-service multipliers. **I will not invent prices.** | section 9 |
| 11 | **Super Admin** | the email for the first admin, and an authenticator app on your phone | section 9 |
| 12 | **Admin console on staging?** | Not part of this kit. Without it, caregivers can be approved only through API calls. Say whether to deploy `apps/admin` (Vercel or the same VPS) next. | later |
| 13 | **`FRONTEND_URL`** | after paying, Paystack sends the browser to `<FRONTEND_URL>/bookings/<id>/payment-complete`. No page exists there; a 404 is cosmetic (the webhook marks the booking paid). Pick a domain you own. | section 7 |

Never paste a key into chat, a ticket or git: you type them into the file on the server (section 5).

## 1. Sizing (estimate, not measured)

| Piece | Steady memory (estimate) |
|---|---|
| api: 4 gunicorn workers (`boot/docker-run.sh`) | 0.8-1.2 GB |
| worker (ARQ) | 0.2-0.3 GB |
| Postgres 16 (small data) | 0.3-0.5 GB |
| Redis (capped at 256 MB) | up to 0.25 GB |
| nginx, Docker, OS | 0.5-0.7 GB |
| **Total** | **about 2.5-3 GB** |

So **4 GB RAM** is comfortable and **2 GB is not** (and building the image on the server peaks near 1 GB on top). If you must start on 2 GB: add 2 GB swap (section 3) and expect slowness. Disk: images ~1.5 GB, Postgres and uploads small for a long time, backups 14 days; 40 GB is plenty.

```
phones ──https──> nginx :443 (host, Let's Encrypt) ──> 127.0.0.1:8000 api ─┬─> db (Postgres, no host port)
                                                                             └─> redis (no host port) <── worker (ARQ)
```
Only 22, 80 and 443 are open to the internet. Postgres and Redis have no published ports at all.

## 2. DNS

At the DNS host of the domain, add:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `api.staging` (full name `api.staging.<domain>`) | the VPS's public IPv4 | 300 |
| AAAA | same | the VPS's IPv6, **only if** you will use it | 300 |

Check from your laptop before section 6: `dig +short api.staging.<domain>` must print the VPS IP. Let's Encrypt fails if it does not.

## 3. First login: a non-root user, SSH keys, firewall, updates

As root on the new VPS (the provider gives you the IP and a root password or key):

```bash
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
# paste YOUR laptop's PUBLIC key (the .pub file) into this file:
nano /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys
echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/deploy && chmod 440 /etc/sudoers.d/deploy
```

From your laptop, confirm `ssh deploy@<ip>` works **before** the next step. Then, on the server (as `deploy`, with `sudo`):

```bash
# SSH: keys only, no root login
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl reload ssh

# Firewall: only SSH, HTTP, HTTPS. (Docker publishes only 127.0.0.1:8000, so it cannot open a hole around ufw.)
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
sudo ufw --force enable

# Updates, clock, tools
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y unattended-upgrades fail2ban git curl jq nginx certbot
sudo timedatectl set-timezone UTC

# Only on a 2 GB server: 2 GB swap
# sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Outbound mail: some providers block ports 25/465/587 on new accounts. If the smoke test's mail step fails and `docker logs` show a connection timeout, ask the provider to unblock **465**.

## 4. Docker

Official Docker repository (Ubuntu 24.04):

```bash
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
```
Log out and back in as `deploy`, then `docker run --rm hello-world` and `docker compose version` must work (compose v2.x; the stack needs `up --wait`, available since 2.1).

## 5. Code, env file and secrets on the server

**5.1 Get the repo (read-only deploy key).** On the server as `deploy`:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/oakly_deploy -C "oakly-staging-deploy"
cat ~/.ssh/oakly_deploy.pub     # copy this PUBLIC key
```
GitHub > `Theoakshealthcare/backend` > Settings > Deploy keys > **Add deploy key**, paste it, leave **"Allow write access" unticked**. Then:

```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/oakly_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
sudo install -d -o deploy -g deploy /opt/oakly
git clone --branch staging git@github.com:Theoakshealthcare/backend.git /opt/oakly
```
The `staging` branch is what gets deployed. Whoever pushes it (the lead) sets it to the commit to test: `git push oakly <sha>:refs/heads/staging`.

**5.2 Create the env file and fill the secrets without printing them.**

```bash
sudo install -d -o deploy -g deploy -m 750 /etc/oakly /var/backups/oakly
cp /opt/oakly/deploy/staging.env.example /etc/oakly/staging.env && chmod 600 /etc/oakly/staging.env
cd /etc/oakly
set_key() { sed -i.bak "s|^$1=.*|$1=$2|" staging.env; }
set_key POSTGRES_PASSWORD      "$(openssl rand -hex 24)"
set_key REDIS_PASSWORD         "$(openssl rand -hex 24)"
set_key SECRET_KEY             "$(openssl rand -hex 32)"
set_key VITALS_ENCRYPTION_KEY  "$(openssl rand -base64 32 | tr '+/' '-_')"
set_key KYC_FINGERPRINT_KEY    "$(openssl rand -hex 32)"
set_key DOJAH_WEBHOOK_SECRET   "$(openssl rand -hex 32)"   # also paste this one into the Dojah dashboard (section 9.3)
sed -i.bak 's/staging\.example\.com/staging.<yourdomain>/g' staging.env   # becomes api.staging.<yourdomain>, admin.staging..., noreply@staging...
rm -f staging.env.bak
nano staging.env     # fill every remaining CHANGE_ME: SMTP_*, PAYSTACK_*, DOJAH_*, GEMINI_API_KEY, CLOUDINARY_*; set FRONTEND_URL; check EMAILS_FROM_EMAIL
grep -E '^[A-Z_]+=.*CHANGE_ME' staging.env | cut -d= -f1    # names only: must print nothing when you are done
```

- **`VITALS_ENCRYPTION_KEY` is the one you cannot lose**: it encrypts health data and voice-note transcripts. Copy it into the team password manager now (`grep ^VITALS /etc/oakly/staging.env` shows it in your own terminal only).
- `deploy/staging.env.example` names **every** setting the backend declares (an unknown key stops the app booting), grouped, with what each needs. `ENVIRONMENT=staging` boots without Dojah; switch to `production` for the pre-launch rehearsal.
- **`GEMINI_MODEL` is not a setting on `main`** (the model is fixed in code; voice notes use `VOICE_NOTE_MODEL`). Setting it breaks boot.
- `DATABASE_URL` and `REDIS_URL` are built by the compose file from the two passwords; do not add them.

## 6. nginx and Let's Encrypt

Two steps, because nginx cannot load a certificate that does not exist yet. `__API_DOMAIN__` is replaced with `api.staging.<yourdomain>`.

```bash
export API_DOMAIN=api.staging.<yourdomain>
sudo rm -f /etc/nginx/sites-enabled/default            # the stock site would clash with ours on port 80
sudo mkdir -p /var/www/certbot

# Step 1: HTTP only, for the certificate challenge
sed "s/__API_DOMAIN__/$API_DOMAIN/g" /opt/oakly/deploy/nginx/oakly-api.bootstrap.conf | sudo tee /etc/nginx/sites-available/oakly-api >/dev/null
sudo ln -sf /etc/nginx/sites-available/oakly-api /etc/nginx/sites-enabled/oakly-api
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d "$API_DOMAIN" -m <support-email> --agree-tos --no-eff-email

# Step 2: the real server block (TLS, proxy to 127.0.0.1:8000, rate limit, security headers)
sed "s/__API_DOMAIN__/$API_DOMAIN/g" /opt/oakly/deploy/nginx/oakly-api.conf | sudo tee /etc/nginx/sites-available/oakly-api >/dev/null
sudo nginx -t && sudo systemctl reload nginx

# Renewal: certbot installs a systemd timer; make nginx pick up renewed certificates
printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo certbot renew --dry-run
```

Until the API is up, `https://$API_DOMAIN/health` answers 502: that is expected here. The server block files are in `deploy/nginx/`; both passed `nginx -t` (1.24, the Ubuntu 24.04 version, and the current stable) with throwaway certificates. What they do: redirect HTTP to HTTPS, serve the ACME challenge, refuse any other hostname, TLS 1.2/1.3, 16 MB bodies (voice notes cap at 12 MB), a generous per-IP rate limit (mobile carriers put many phones behind one IP), and a **set** (not appended) `X-Forwarded-For` so the app's `TRUSTED_PROXIES` logic (`app/core/client_ip.py`) sees the real client.

## 7. First deploy (build on the server)

```bash
cd /opt/oakly
deploy/deploy.sh --build --skip-backup oaklycare-backend:$(git rev-parse --short HEAD)
```
`--skip-backup` is only for this very first run (the database is empty). What `deploy/deploy.sh` does, in order: records the image in `/etc/oakly/image.env` (the previous one goes to `image.env.prev`); builds (or pulls); starts db and redis and waits until healthy; backs up; runs **`alembic upgrade head` in a one-off container** (a bad migration stops the deploy before the new API serves traffic; skipped on `--rollback`, which is image-only); starts api and worker; waits for the API's health check (up to 240 s). It stops at the first failure and tells you the rollback command.

Then confirm, before anything else:

```bash
deploy/compose.sh ps                       # api, worker, db, redis: all "healthy"
curl -s https://api.staging.<yourdomain>/health          # {"status":"healthy"}
deploy/compose.sh run --rm --no-deps api python -c "from app.core.config import settings; print('settings OK', settings.ENVIRONMENT)"
deploy/compose.sh logs --tail 40 api worker               # no tracebacks; the worker logs its cron jobs
```
Common first-run failures: `Fernet key must be 32 url-safe base64-encoded bytes` (bad `VITALS_ENCRYPTION_KEY`); a pydantic "extra fields not permitted" (a key `Settings` does not declare); the API unhealthy because the database password in the volume differs from the file (you changed `POSTGRES_PASSWORD` after the first `up`: `deploy/compose.sh down -v` on a brand new server only).

## 8. Backups (Postgres dump + uploads) and the restore test

`deploy/backup-db.sh` writes `oakly-<UTC stamp>.dump` (Postgres custom format) and `private_uploads-<stamp>.tgz` (caregiver documents live in a Docker volume, not in Postgres) to `/var/backups/oakly`, refuses to keep a dump it cannot list, and deletes files older than 14 days. `deploy.sh` runs it before every migration.

```bash
# nightly at 02:17 UTC, as the deploy user
( crontab -l 2>/dev/null; echo '17 2 * * * /opt/oakly/deploy/backup-db.sh >> /var/backups/oakly/backup.log 2>&1' ) | crontab -
/opt/oakly/deploy/backup-db.sh          # run it once now
/opt/oakly/deploy/restore-test.sh       # RESTORE TEST: loads the newest dump into a scratch database, checks it, drops it
```
**A backup is only real once `restore-test.sh` printed `restore test PASSED`.** Repeat it after the first real data and after any migration that changes tables. Disk-only backups die with the server: for anything beyond staging, copy `/var/backups/oakly` off the machine (provider snapshots or `rclone` to a bucket; follow-up).

## 9. First-run steps (once per environment)

### 9.1 Create the first Super Admin

```bash
/opt/oakly/deploy/compose.sh exec api python -m scripts.create_super_admin --email <owner-email>
```
It asks for the password at a hidden prompt (at least 12 characters, at least 5 different characters, not containing the part of your email before the `@`). Exit codes: 0 created, 1 the email already exists, 2 bad input. The account is created with MFA **not** enrolled: the first sign-in enrols it (9.2).

### 9.2 Publish the one-time pricing row (`booking_settings`), only with the owner's numbers

A fresh database has no pricing row. Until one exists every booking preview and create answers `503` with `{"detail": {"code": "booking_settings_not_configured", ...}}`. `POST /api/v1/booking-settings/` creates the first version and needs an admin with `pricing.write` (a Super Admin has it). **Do not guess prices.** Ask the owner for:

| Field | Meaning | Rule (from the code) |
|---|---|---|
| `base_fee` | naira **per hour** | 0.01 to 1,000,000, at most 2 decimals |
| `platform_fee_percent` | Oakly's cut | 0 to 100 |
| `multipliers` | optional, per service type: `nurse`, `doctor`, `physiotherapist`, `caregiver`, `nutritionist` | each above 0, at most 10 |
| `surge_pricing` / `surge_multiplier` | surge on/off, 1 to 5 | default off, 1 |
| `discount` | percent | 0 to 100, default 0 |
| `reason` | why (kept in the audit log) | required, not blank |

Run this from **your laptop** in the backend repo (needs only Python 3, no install). It signs the admin in (hidden password prompt, authenticator code prompt), refuses to change anything if pricing already exists, and shows the numbers for a yes/no before it sends:

```bash
python3 -m scripts.staging_set_pricing https://api.staging.<yourdomain> --admin-email <owner-email> \
  --base-fee <naira> --platform-fee-percent <percent> --multiplier nurse=<x> --multiplier doctor=<x> \
  --reason "Opening staging price list"
```
On the **first** sign-in it prints the authenticator setup key once: add it in your authenticator app ("enter a setup key"), type the 6-digit code. The raw request, for reference (from the live capture `docs/fe-captures/admin/145-pricing-create.json`; the helper sends the same body):

```json
{"base_fee": 5000, "platform_fee_percent": 10, "multipliers": {"nurse": 1.0, "doctor": 2.0},
 "surge_pricing": false, "surge_multiplier": 1.0, "discount": 0, "reason": "Opening price list"}
```
(Those numbers are the capture's test values, **not** a recommendation.) Later edits are `PUT /api/v1/booking-settings/{latest id}`: every save is a new version.

### 9.3 Register the webhooks (click by click)

**Paystack** (TEST mode): dashboard, make sure the **Test mode** switch at the top is on, then Settings > **API Keys & Webhooks**. In **Test Webhook URL** paste

`https://api.staging.<yourdomain>/api/v1/payment/paystack/webhook`

and Save. (Paystack signs webhooks with your secret key; there is no separate secret to enter, and `PAYSTACK_WEBHOOK_SECRET` in the env file is unused.) The same page shows the `sk_test_`/`pk_test_` keys for the env file. A booking, a wallet deposit, a refund and a payout transfer all arrive on this one URL.

**Dojah** (sandbox): dashboard > your app > Webhooks (wording may vary): URL `https://api.staging.<yourdomain>/api/v1/kyc/dojah/webhook`, and as **Secret** the exact `DOJAH_WEBHOOK_SECRET` value from the env file (not the API secret key).

**WhatsApp** is parked (blocked on Meta business verification); its env keys stay empty and the webhook refuses every call.

## 10. How to know staging works (mirrors `scripts/staging_smoke.py`)

Run from your laptop (Python 3 only):

```bash
python3 -m scripts.staging_smoke https://api.staging.<yourdomain> --email <a-real-mailbox-you-can-read> --wait-payment 180
```
Each line prints PASS / FAIL / SKIP; any FAIL gives exit code 1 and says what to check.

| # | Check | What proves it | If it fails |
|---|---|---|---|
| 1 | `GET /health` over HTTPS, plus the OpenAPI document | `200 {"status":"healthy"}`; a valid certificate (the script verifies TLS) | DNS (section 2), firewall, nginx (`sudo nginx -t`), `compose ps` |
| 2 | Both webhooks reachable and signed-only | Paystack path answers `400 Invalid webhook signature`, Dojah path `401 Invalid signature` to an unsigned call | the URL is exactly what is registered in 9.3; nginx proxies `/api/` |
| 3 | Pricing row exists | `POST /booking/preview` is `200` | `503 booking_settings_not_configured`: do 9.2 |
| 4 | **Sign-up code arrives by email** | the script registers a throwaway patient with your address, then you read the 5-digit code in that mailbox and type it; `verify-email` answers 200 | no mail: `deploy/compose.sh logs api \| grep -i smtp`; SMTP host/port **465**, credentials, SPF/DKIM, provider port blocking |
| 5 | Login | `200` with a token pair | |
| 6 | **Paystack payment link** | `POST /patient/wallet/deposit` returns a `https://checkout.paystack.com/...` URL; open it, pay with a Paystack **test** card; within 180 s the wallet balance becomes 100 | link fails: `PAYSTACK_SECRET_KEY` (test key?); link works but balance stays 0: the webhook URL (9.3), or the key in the env file is not the one of the dashboard in Test mode |
| 7 | **Push token round trip** | `POST /devices/push-token` 200, `DELETE` 204 | auth or DB error in the logs |
| 8 | Optional: **real push chain** | `--expo-token 'ExponentPushToken[...]'` from a phone: Expo accepts it and the receipt says delivered | `InvalidCredentials`: the Firebase Admin SDK key was not uploaded to Expo (section 11) |

Extra by hand: `deploy/compose.sh logs --tail 100 worker` shows cron jobs running every minute with no tracebacks, and `curl -I http://api.staging.<yourdomain>/health` answers a `301` to HTTPS. A user's address in the API logs should be the phone's public IP, not `172.29.0.1` (that proves `TRUSTED_PROXIES` matches the compose subnet): check `deploy/compose.sh logs api | tail`.

## 11. Rebuild both apps against staging, and test push on a real phone

The apps read the server origin from `EXPO_PUBLIC_API_URL` (`apps/*/src/api/config.ts`: origin only, **no** `/api/v1`; the default is `http://localhost:8000`). Today neither `eas.json` sets it, so the existing preview APKs point at `localhost`. **Proposed change, not applied** (the monorepo is `/Users/adebayovictor/Documents/dev/oakly/caregiver`; one small PR, same edit in both files):

```diff
--- a/apps/caregiver/eas.json     (and the identical change in apps/patient/eas.json)
+++ b/apps/caregiver/eas.json
@@
     "preview": {
-      "distribution": "internal"
+      "distribution": "internal",
+      "env": {
+        "EXPO_PUBLIC_API_URL": "https://api.staging.<yourdomain>"
+      }
     },
```
Keep the value in **one** place: do not also create an EAS environment variable named `EXPO_PUBLIC_API_URL` for `preview` (check with `eas env:list preview`). It is public by design (it ends up inside the app). It must be **https**: Android 9 and later block plain-http connections by default. When `production` builds start, give that profile its own `env` with the production origin.

Rebuild (Android preview APKs; iOS needs an Apple Developer account, which the owner does not have). From the monorepo root:

```bash
cd /Users/adebayovictor/Documents/dev/oakly/caregiver
git pull && pnpm install --frozen-lockfile          # first: EAS fails to resolve expo-notifications on a stale node_modules
cd apps/caregiver && npx -y eas-cli@latest env:list preview      # GOOGLE_SERVICES_JSON must be listed (file vars print as *****)
npx -y eas-cli@latest build --platform android --profile preview --non-interactive
cd ../patient && npx -y eas-cli@latest env:list preview
npx -y eas-cli@latest build --platform android --profile preview --non-interactive
```
- `GOOGLE_SERVICES_JSON` is an EAS **file** environment variable (secret) set for `preview` and `production` in each project (`oaklycare-caregiver`, `oakly-patient`); `app.config.js` reads it. Firebase files are gitignored and are **not** uploaded with the code.
- **Push needs one more credential**: the Firebase **Admin SDK** key (a service-account JSON) must be uploaded to Expo, not to this server. It stays in `~/oakly-secrets/` (outside any repo, mode 600). In each app folder run `npx -y eas-cli@latest credentials`, choose **Android**, the **preview** profile, then the **Google Service Account Key for Push Notifications (FCM V1)** entry and upload the file (menu wording varies slightly by CLI version). Do it for both projects. The patient app folder currently has an Admin SDK key file inside the repo folder: move it to `~/oakly-secrets/` first.
- When the builds finish, open the build page on the phone (link or QR from expo.dev), download the APK, allow "install unknown apps" for the browser, install both apps.

**Push test on a real phone** (this is the part nobody has ever seen work):
1. Log in to the **caregiver** app on the phone and allow notifications when asked. The app then calls `POST /devices/push-token`.
2. Prove the token reached the server (token shortened on purpose): `deploy/compose.sh exec db psql -U oakly -d oakly -c "select platform, left(token,24) as token_start, last_seen_at from push_tokens order by last_seen_at desc limit 5"`.
3. Prove Expo and Firebase accept it: copy the full token from the database once (`select token from push_tokens ...`) and run the smoke script with `--expo-token '<token>'`. The phone must buzz and the script must print `Expo delivered the push to Firebase`.
4. Prove the **backend** sends one: with a test caregiver approved and online, make a booking from the patient app (pay with a Paystack test card); dispatch notifies the caregiver, whose phone should buzz. If step 3 worked and step 4 did not, read `deploy/compose.sh logs worker api | grep -i push`.

## 12. Rollback and restore

| Situation | Do |
|---|---|
| New image misbehaves, schema still compatible | `deploy/deploy.sh --rollback` (image back to the previous deploy; takes the image from `/etc/oakly/image.env.prev`). Most migrations here only **add** things, so the previous image keeps working on the new schema. |
| A migration broke data or an old image cannot run on the new schema | restore the dump `deploy.sh` took just before the migration (below), then roll the image back |
| Wrong env value | edit `/etc/oakly/staging.env`, then `deploy/compose.sh up -d --force-recreate api worker` |
| Everything wrong on a brand-new, empty server | `deploy/compose.sh down -v` and redo section 7 (**`-v` deletes the database**; never on a server with data) |

Restore over the live database (stops the app, replaces the database with a dump, starts the app; tested on a throwaway Postgres):

```bash
cd /opt/oakly
deploy/compose.sh stop api worker
deploy/compose.sh exec -T db psql -U oakly -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE oakly WITH (FORCE)" -c "CREATE DATABASE oakly"
deploy/compose.sh exec -T db pg_restore -U oakly -d oakly --no-owner --exit-on-error < /var/backups/oakly/oakly-<stamp>.dump
deploy/deploy.sh --rollback        # image back as well (skip if the image is fine)
deploy/compose.sh up -d api worker
```
Uploads: `docker run --rm -v oakly-staging_private_uploads:/data -v /var/backups/oakly:/in alpine:3.20 sh -c "cd /data && tar xzf /in/private_uploads-<stamp>.tgz"`.

## 13. Turning voice notes on (after push works)

`VOICE_NOTES_ENABLED=false` for the first deploy. Turn it on only after (a) section 11's push test passed, (b) `GEMINI_API_KEY` is a **paid, non-training tier** key, (c) the owner has set the daily audio budget (`VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP`) and the retention days (`VOICE_NOTE_RETENTION_DAYS`, 365 is only a proposal). Edit `/etc/oakly/staging.env`, `deploy/compose.sh up -d --force-recreate api worker`, record a real voice note from the phone. The real Gemini call has **never** run: whether it accepts the phone's m4a is unknown (`docs/sop/2026-11-08-ai-note-transcription.md`). It is also the kill switch: set it back to `false` and recreate.

## 14. CI deploy workflow (draft, disabled)

`.github/workflows/deploy-staging.yml` builds the image from a chosen ref, pushes it to GHCR as `ghcr.io/theoakshealthcare/backend:<git sha>`, then over SSH checks out that sha on the server, runs `deploy/deploy.sh <image>` (pull, backup, `alembic upgrade head`, restart) and smoke-tests. It is **manual only** (`workflow_dispatch`, never on push or PR), and **off even then** until the repository variable `STAGING_DEPLOY_ENABLED=true` exists. Turn it on after sections 1-9 are done by hand once:

1. GitHub > repo Settings > Environments > New environment `staging`; add the owner as a required reviewer.
2. Secrets (in that environment): `STAGING_SSH_HOST`, `STAGING_SSH_USER` (`deploy`), `STAGING_SSH_KEY` (a **new** private key for CI only, public half in `/home/deploy/.ssh/authorized_keys`), `STAGING_SSH_KNOWN_HOSTS` (`ssh-keyscan -t ed25519 <ip>` from a trusted network), `STAGING_API_URL`, `STAGING_GHCR_USER`, `STAGING_GHCR_PULL_TOKEN` (a token with **only** `read:packages`).
3. Settings > Secrets and variables > Actions > **Variables**: `STAGING_DEPLOY_ENABLED` = `true`.
4. Run it: `gh workflow run deploy-staging.yml --ref main -f ref=staging` (a dispatch workflow must exist on the default branch to be listed).

Not run anywhere; checked with `actionlint` only. Not included yet: an image scan (Trivy) and a Semgrep run before deploy (follow-up).

## 15. Operate: cheat sheet

```bash
deploy/compose.sh ps                                  # state and health
deploy/compose.sh logs -f --tail 100 api worker       # logs (rotated: 10 MB x 5 per container)
deploy/compose.sh restart worker                      # restart one service
deploy/compose.sh exec db psql -U oakly -d oakly      # a SQL prompt
deploy/compose.sh exec api alembic current            # which migration the database is on
df -h / && docker system df                           # disk; `docker image prune -f` after many deploys
sudo certbot certificates                             # certificate expiry
```
The `staging` URL structure the rest of the system needs: API `https://api.staging.<yourdomain>`; Paystack webhook `/api/v1/payment/paystack/webhook`; Dojah webhook `/api/v1/kyc/dojah/webhook`; `KYC_WIDGET_PUBLIC_BASE_URL` = the API origin.

## 16. What was verified offline, and what was not

| Piece | Status |
|---|---|
| `docker-compose.staging.yml` | **Verified offline:** `docker compose config` parses (standalone, and combined with `docker-compose.yml`, which you must not do); a missing password is refused; the render shows only `127.0.0.1:8000` published and no host port for db or Redis. **Live (throwaway project, then removed):** the `db` service becomes healthy; the `redis` service enforces the password and `noeviction`. **Not run:** the `api` and `worker` services from this file (no image was built; a full stack was deliberately not started). |
| `arq --check` worker healthcheck | **Verified live** against a throwaway Redis with the real worker: exit 1 before it starts, 0 while it runs |
| `deploy/backup-db.sh`, `restore-test.sh`, restore-over-live sequence | **Verified live** against a throwaway Postgres 16 with a fake schema (dump listed, scratch restore passed, live DB untouched, disaster-and-restore recovered). **Not verified:** against the real schema and data volume |
| `deploy/deploy.sh`, `compose.sh` | `shellcheck` clean; argument handling read-checked. **Not run end to end** (needs an image and a server) |
| `deploy/nginx/*.conf` | `nginx -t` passes on nginx 1.24 and current stable with throwaway certificates. **Not run:** with real certificates, a real domain, real traffic; `TRUSTED_PROXIES` matching the real bridge address is unproven until section 10's last note |
| `deploy/staging.env.example` | **Verified by test:** names every one of the backend's 140 settings (plus the compose-only `POSTGRES_PASSWORD`), nothing Settings would refuse, loads as a Settings env file, trusted-proxy CIDR equals the compose subnet; the secret-fill snippet was run on a copy; the Fernet command output is a valid key |
| `scripts/staging_smoke.py`, `scripts/staging_set_pricing.py` | **Verified against a fake server** whose shapes come from the live captures and route code (success, missing pricing, wrong code, nothing listening, no email). **Not run against a real server.** The Expo receipt step and Paystack checkout were not exercised against the real services |
| Deploy workflow | `actionlint` clean. **Never run** |
| Mobile section | The `eas.json` diff is **proposed, not applied or built**; `eas credentials` menu wording is from memory of the CLI |
| Provider recommendations, memory sizing, prices | **Estimates**, not measured |

## 17. Follow-ups and known gaps

1. **Run it once on a real server** and fix whatever the first run finds; then update section 16.
2. **Admin console on staging** (open decision 12 in section 0).
3. **Off-server backups** (bucket or provider snapshots) and a failure alert on the backup cron (e.g. a healthchecks.io ping).
4. **Image scan and Semgrep before deploy** in the deploy workflow; pin the third-party action versions by SHA if the repo requires it.
5. `/docs` and `/api/v1/openapi.json` are public on staging; put basic auth in front in nginx if that matters.
6. **Prescription note files** go to `/app/uploaded_notes` (a volume now). The legacy `/app/uploads/profile_images` mount is not persisted (profile photos use Cloudinary).
7. `FCM_SERVER_KEY`, `APNS_*` and `PAYSTACK_WEBHOOK_SECRET` are declared in `Settings` but read by nothing; removing them is a separate cleanup.
8. Pin `requires-python` below 3.14 (or fix the worker start) so a fresh environment can never pick the interpreter `arq` breaks on.
9. Paystack: run the live test-mode **transfer** and **refund** (the owner disables OTP for API transfers first); Dojah live K8 pass; real push; voice-notes Gemini smoke (handoff 1.1 item 8).
