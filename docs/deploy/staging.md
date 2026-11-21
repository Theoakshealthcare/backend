# Staging deploy runbook (first deploy on a fresh Ubuntu VPS)

**Updated 2026-11-21 (deployment adoption):** staging and production are now two stacks on ONE server, deployed by the build-once pipeline (`docs/deployment/`), not by hand from a git clone. Sections 5-8, 12-15 describe that flow; the map is `docs/deployment/DEPLOYMENT_GUIDE.md` and the owner's GitHub clicks are `docs/deployment/GITHUB_ACTIONS_SETUP.md`. Where a section says "staging" the same steps apply to production with the production names (directory `/opt/oakly-production`, port 8101, domain `api.<domain>`).

**Status: prepared, not run on a server.** Nothing here has touched a real VPS, domain, mail provider, Paystack, Dojah or Expo account. Section 16 lists exactly what was checked offline and what was not. Everything is written so the first deploy is a short checklist once the owner has a server.

The stack: **api** (FastAPI behind gunicorn), **worker** (ARQ: dispatch, payouts, refunds, KYC, voice notes), **Postgres 16**, **Redis 7**, plus **nginx + Let's Encrypt** on the host. The image is Python **3.13** (`.python-version`, `Dockerfile`): **do not bump it to 3.14**, `arq` 0.26.3 breaks there ("There is no current event loop").

## 0. What I need from the owner

| # | Decision / item | Recommendation | Needed by |
|---|---|---|---|
| 1 | **Domain** (and who controls its DNS) | A subdomain you already own, e.g. `staging-api.<yourdomain>` for the API. Tell me the domain; I replace `staging.example.com` everywhere. | before section 2 |
| 2 | **VPS provider and plan** | **DECIDED (2026-11-21): one server for both environments, 8 GB RAM / 4 vCPU / 80+ GB SSD, Ubuntu 24.04 LTS** (DEP-4: staging and production share it and so share its failure modes; see `docs/deployment/DEPLOYMENT_GUIDE.md`). Pick the cheapest reputable EU region (London or Frankfurt) for now; decide a production region later with the latency measurement: from Lagos run `curl -o /dev/null -s -w '%{time_connect} %{time_total}\n' https://staging-api.<domain>/health` against staging and against a trial server in Johannesburg/Cape Town. Practical blocker in Nigeria: most providers want an international (dollar) card or PayPal; a virtual dollar card works. | before section 3 |
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

Never paste a key into chat, a ticket or git: you put them into the env file on your own laptop and encrypt it (section 5.2).

## 1. Sizing (estimate, not measured)

One **8 GB RAM / 4 vCPU** server runs both stacks: production about 2.5-3 GB (api with 4 gunicorn workers, worker, Postgres, Redis), staging about 1.8-2.2 GB (`GUNICORN_WORKERS=2`), the host about 0.7 GB, so **5-6 GB in total**. Do not go below 8 GB: 4 GB fits one stack only. The table, the layout and the trade-off of sharing a machine are in `docs/deployment/DEPLOYMENT_GUIDE.md`. Disk: images ~1.5 GB each, Postgres and uploads small for a long time, backups 14 days per stack; 80 GB is plenty. Measure with `docker stats --no-stream` after the first week.

```
phones ──https──> nginx :443 (host, Let's Encrypt)
                    ├─ staging-api.<domain> ──> 127.0.0.1:8100  oakly-staging    api ─┬─> db (Postgres, no host port)
                    │                                                               └─> redis (no host port) <── worker (ARQ)
                    └─ api.<domain>         ──> 127.0.0.1:8101  oakly-production   (same four services, own network and volumes)
```
Only 22, 80 and 443 are open to the internet. Postgres and Redis have no published ports at all.

## 2. DNS

At the DNS host of the domain, add:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `staging-api` (full name `staging-api.<domain>`) | the VPS's public IPv4 | 300 |
| A | `api` (full name `api.<domain>`), when production exists | the same IPv4 | 300 |
| AAAA | same | the VPS's IPv6, **only if** you will use it | 300 |

Check from your laptop before section 6: `dig +short staging-api.<domain>` must print the VPS IP. Let's Encrypt fails if it does not.

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

# Firewall: only SSH, HTTP, HTTPS. (Docker publishes the APIs only on 127.0.0.1, so it cannot open a hole around ufw.)
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

## 5. Stack directories, env files and secrets

There is **no git clone on the server any more** and no `/etc/oakly` directory: the deploy copies the compose file, the `.env` and three scripts into one directory per environment, and pulls the image from GitHub's registry by digest.

**5.1 The two stack directories.** On the server as `deploy`:

```bash
sudo install -d -o deploy -g deploy /opt/oakly-staging /opt/oakly-production
```

The SSH key GitHub Actions uses to reach the server is created in `docs/deployment/GITHUB_ACTIONS_SETUP.md` step 1.

**5.2 The env files (on YOUR laptop, never on the server, never in chat).** The secrets travel as **encrypted files in git** (`.env.staging.enc`, `.env.production.enc`, decision DEP-2), encrypted with one shared key (the same value is the `ENV_ENCRYPTION_KEY` secret of both GitHub Environments). **Do not create or commit either file until the leaked keys are rotated** (Paystack, Zoho, Gemini, Cloudinary and the pasted Prembly key: `docs/sop/2026-10-05-secrets-and-rotation.md`): ciphertext in git history stays for good.

```bash
cd <your backend checkout>
cp deploy/env.example .env.staging && chmod 600 .env.staging
set_key() { sed -i.bak "s|^$1=.*|$1=$2|" .env.staging; }     # on macOS sed needs the -i.bak form, as written
set_key POSTGRES_PASSWORD      "$(openssl rand -hex 24)"
set_key REDIS_PASSWORD         "$(openssl rand -hex 24)"
set_key SECRET_KEY             "$(openssl rand -hex 32)"
set_key VITALS_ENCRYPTION_KEY  "$(openssl rand -base64 32 | tr '+/' '-_')"
set_key KYC_FINGERPRINT_KEY    "$(openssl rand -hex 32)"
set_key DOJAH_WEBHOOK_SECRET   "$(openssl rand -hex 32)"   # also paste this one into the Dojah dashboard (section 9.3)
sed -i.bak 's/staging\.example\.com/staging.<yourdomain>/g' .env.staging   # becomes staging-api.<yourdomain>, admin.staging..., noreply@staging...
rm -f .env.staging.bak
nano .env.staging    # fill every remaining CHANGE_ME: SMTP_*, PAYSTACK_*, DOJAH_*, GEMINI_API_KEY, CLOUDINARY_*; set FRONTEND_URL
grep -E '^[A-Z_]+=.*CHANGE_ME' .env.staging | cut -d= -f1    # names only: must print nothing when you are done
./scripts/env.sh generate-key                                 # once, for both environments: prints the key ONCE: password manager + the GitHub secret of both Environments
echo '<the key>' > .env.key                                   # gitignored
./scripts/env.sh encrypt staging && git add .env.staging.enc  # commit ONLY the .enc file
```

For production repeat with `.env.production` (same key); production differs in `ENVIRONMENT=production`, `COMPOSE_PROJECT_NAME=oakly-production`, `API_PORT=8001`, `DOCKER_SUBNET` and `TRUSTED_PROXIES` both `172.29.1.0/24`, its domain, and the live provider keys (the table in `docs/deployment/DEPLOYMENT_GUIDE.md`). Never reuse a secret between the two files.

- **`VITALS_ENCRYPTION_KEY` is the one you cannot lose**: it encrypts health data and voice-note transcripts, and a restored backup needs it. Copy it into the team password manager at once.
- `deploy/env.example` names **every** setting the backend declares (an unknown key stops the app booting), grouped by family, with no comments (the commands that generate each secret are in this section). `ENVIRONMENT=staging` boots without Dojah; production refuses to boot without the live provider keys.
- `GEMINI_MODEL` (FallGuard advice) and `VOICE_NOTE_MODEL` (voice notes) are both settings; the example carries the code defaults.
- `DATABASE_URL` and `REDIS_URL` are built by the compose file from the two passwords; do not add them. `API_IMAGE` is written by the deploy; do not add it either.
- Printing is the risk: use `./scripts/env.sh diff` (names only) to compare the two files, and never `cat` a decrypted file into a shared terminal or chat.

## 6. nginx and Let's Encrypt (two sites)

The templates are in the repo (`deploy/nginx/`) and the server has no clone, so copy them over from your laptop once: `scp deploy/nginx/*.conf deploy@<ip>:/tmp/`. Two steps per site, because nginx cannot load a certificate that does not exist yet. `__API_DOMAIN__`, `__API_PORT__` and `__ENV__` are placeholders.

```bash
sudo rm -f /etc/nginx/sites-enabled/default            # the stock site would clash with ours on port 80
sudo mkdir -p /var/www/certbot
sudo cp /tmp/oakly-common.conf /etc/nginx/conf.d/oakly-common.conf     # the two catch-all servers, once for both sites

# a function so the two sites use identical steps:  site <env> <port> <domain>
site() {
  local env="$1" port="$2" domain="$3"
  # Step 1: HTTP only, for the certificate challenge
  sed "s/__API_DOMAIN__/$domain/g" /tmp/oakly-api.bootstrap.conf | sudo tee /etc/nginx/sites-available/oakly-api-$env >/dev/null
  sudo ln -sf /etc/nginx/sites-available/oakly-api-$env /etc/nginx/sites-enabled/oakly-api-$env
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot certonly --webroot -w /var/www/certbot -d "$domain" -m <support-email> --agree-tos --no-eff-email
  # Step 2: the real server block (TLS, proxy to 127.0.0.1:<port>, rate limit, security headers)
  sed "s/__ENV__/$env/g; s/__API_PORT__/$port/g; s/__API_DOMAIN__/$domain/g" /tmp/oakly-api.conf | sudo tee /etc/nginx/sites-available/oakly-api-$env >/dev/null
  sudo nginx -t && sudo systemctl reload nginx
}
site staging    8100 staging-api.<yourdomain>
site production 8101 api.<yourdomain>      # later, when production exists (DEP-5)

# Renewal: certbot installs a systemd timer; make nginx pick up renewed certificates
printf '#!/bin/sh\nsystemctl reload nginx\n' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo certbot renew --dry-run
```

Until the API is up, `https://<domain>/health` answers 502: that is expected here. The server block files are in `deploy/nginx/`; with both sites rendered they pass `nginx -t` on current stable nginx with throwaway certificates (one harmless warning each about the deprecated `listen ... http2` form, which Ubuntu 24.04's nginx 1.24 needs). What they do: redirect HTTP to HTTPS, serve the ACME challenge, refuse any other hostname (`oakly-common.conf`), TLS 1.2/1.3, 16 MB bodies (voice notes cap at 12 MB), a generous per-IP rate limit (mobile carriers put many phones behind one IP), and a **set** (not appended) `X-Forwarded-For` so the app's `TRUSTED_PROXIES` logic (`app/core/client_ip.py`) sees the real client. Each site proxies to its own stack's port (staging 8100, production 8101).

## 7. First deploy (through the pipeline)

With sections 1-6 done, the secrets and variables from `docs/deployment/GITHUB_ACTIONS_SETUP.md` in place, and `.env.staging.enc` committed, set the repository variable `STAGING_DEPLOY_ENABLED=true` (the pipeline is off until then) and merge to `develop`. The run **CD (staging)** then: builds the image once and pushes it to GHCR, copies the compose file, `.env` and scripts to `/opt/oakly-staging`, and runs `scripts/remote_deploy.sh` there: record the running image, pull by digest, start `db` and `redis`, **back up**, `alembic upgrade head` in a one-off container (a bad migration stops the deploy before the new API serves traffic), start `api` and `worker` and wait until all four services are healthy, probe the port. On any failure it puts the previous image back and fails the run. Then the smoke test runs against `APP_URL`; the image is marked staging-verified only if it passes.

**Expect the first run to stop at the smoke test's pricing check** (503 `booking_settings_not_configured`): that is correct on an empty database. Do sections 9.1 and 9.2, then re-run the failed workflow.

Confirm on the server, before anything else:

```bash
cd /opt/oakly-staging
C="docker compose -f docker-compose.prod.yml --env-file .env"
$C ps                                       # api, worker, db, redis: all "healthy"
curl -s https://staging-api.<yourdomain>/health          # {"status":"healthy"}
$C run --rm --no-deps api python -c "from app.core.config import settings; print('settings OK', settings.ENVIRONMENT)"
$C logs --tail 40 api worker                # no tracebacks; the worker logs its cron jobs
```
Common first-run failures: `Fernet key must be 32 url-safe base64-encoded bytes` (bad `VITALS_ENCRYPTION_KEY`); a pydantic "extra fields not permitted" (a key `Settings` does not declare); the API unhealthy because the database password in the volume differs from the file (you changed `POSTGRES_PASSWORD` after the first deploy: `$C down -v` on a brand new server only); `permission denied` on `/opt/oakly-staging` (section 5.1 skipped).

To debug a first deploy by hand instead of waiting for the pipeline, copy the compose file, a decrypted `.env` and `scripts/remote_deploy.sh`, `backup_cron.sh` to the stack directory and run `IMAGE_REF=<image@digest> DEPLOY_PATH=/opt/oakly-staging STACK_ENV=staging INSTALL_CRON=0 bash scripts/remote_deploy.sh` (`GHCR_USER` and `GHCR_TOKEN` too if the image is private). Delete the plaintext `.env` afterwards.

## 8. Backups (Postgres dump + uploads) and the restore test

Every deploy takes a backup before the migration, and the first successful deploy installs a daily cron (staging 02:15 UTC, production 02:00 UTC). The files are `oakly-<UTC stamp>.dump` (Postgres custom format) and `private_uploads-<stamp>.tgz` (caregiver documents live in a Docker volume, not in Postgres) in `<stack directory>/backups`, kept 14 days. Full guide, restore steps and the restore test: `docs/deployment/BACKUPS_GUIDE.md`.

```bash
/opt/oakly-staging/scripts/backup_cron.sh      # run it once now
/opt/oakly-staging/scripts/restore_test.sh     # RESTORE TEST: loads the newest dump into a scratch database, checks it, drops it
```
**A backup is only real once `restore_test.sh` printed `restore test PASSED`.** Repeat it after the first real data and after any migration that changes tables. **The backups are on the same disk as the database**: for anything beyond staging, copy `backups/` off the machine (`rclone` to a bucket or provider snapshots; open follow-up).

## 9. First-run steps (once per environment)

### 9.1 Create the first Super Admin

```bash
cd /opt/oakly-staging && docker compose -f docker-compose.prod.yml --env-file .env exec api python -m scripts.create_super_admin --email <owner-email>
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
python3 -m scripts.staging_set_pricing https://staging-api.<yourdomain> --admin-email <owner-email> \
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

`https://staging-api.<yourdomain>/api/v1/payment/paystack/webhook`

and Save. (Paystack signs webhooks with your secret key; there is no separate secret to enter.) The same page shows the `sk_test_`/`pk_test_` keys for the env file. A booking, a wallet deposit, a refund and a payout transfer all arrive on this one URL.

**Dojah** (sandbox): dashboard > your app > Webhooks (wording may vary): URL `https://staging-api.<yourdomain>/api/v1/kyc/dojah/webhook`, and as **Secret** the exact `DOJAH_WEBHOOK_SECRET` value from the env file (not the API secret key).

**WhatsApp** is parked (blocked on Meta business verification); its env keys stay empty and the webhook refuses every call.

## 10. How to know staging works (mirrors `scripts/staging_smoke.py`)

Run from your laptop (Python 3 only):

```bash
python3 -m scripts.staging_smoke https://staging-api.<yourdomain> --email <a-real-mailbox-you-can-read> --wait-payment 180
```
Each line prints PASS / FAIL / SKIP; any FAIL gives exit code 1 and says what to check.

| # | Check | What proves it | If it fails |
|---|---|---|---|
| 1 | `GET /health` over HTTPS, plus the OpenAPI document | `200 {"status":"healthy"}`; a valid certificate (the script verifies TLS) | DNS (section 2), firewall, nginx (`sudo nginx -t`), `compose ps` |
| 2 | Both webhooks reachable and signed-only | Paystack path answers `400 Invalid webhook signature`, Dojah path `401 Invalid signature` to an unsigned call | the URL is exactly what is registered in 9.3; nginx proxies `/api/` |
| 3 | Pricing row exists | `POST /booking/preview` is `200` | `503 booking_settings_not_configured`: do 9.2 |
| 4 | **Sign-up code arrives by email** | the script registers a throwaway patient with your address, then you read the 5-digit code in that mailbox and type it; `verify-email` answers 200 | no mail: `$C logs api \| grep -i smtp`; SMTP host/port **465**, credentials, SPF/DKIM, provider port blocking |
| 5 | Login | `200` with a token pair | |
| 6 | **Paystack payment link** | `POST /patient/wallet/deposit` returns a `https://checkout.paystack.com/...` URL; open it, pay with a Paystack **test** card; within 180 s the wallet balance becomes 100 | link fails: `PAYSTACK_SECRET_KEY` (test key?); link works but balance stays 0: the webhook URL (9.3), or the key in the env file is not the one of the dashboard in Test mode |
| 7 | **Push token round trip** | `POST /devices/push-token` 200, `DELETE` 204 | auth or DB error in the logs |
| 8 | Optional: **real push chain** | `--expo-token 'ExponentPushToken[...]'` from a phone: Expo accepts it and the receipt says delivered | `InvalidCredentials`: the Firebase Admin SDK key was not uploaded to Expo (section 11) |

Extra by hand: `$C logs --tail 100 worker` (from `/opt/oakly-staging`, with `C` set as in section 7) shows cron jobs running every minute with no tracebacks, and `curl -I http://staging-api.<yourdomain>/health` answers a `301` to HTTPS. A user's address in the API logs should be the phone's public IP, not `172.29.0.1` (that proves `TRUSTED_PROXIES` matches the compose subnet): check `deploy/compose.sh logs api | tail`.

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
+        "EXPO_PUBLIC_API_URL": "https://staging-api.<yourdomain>"
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
2. Prove the token reached the server (token shortened on purpose): `$C exec db psql -U oakly -d oakly -c "select platform, left(token,24) as token_start, last_seen_at from push_tokens order by last_seen_at desc limit 5"`.
3. Prove Expo and Firebase accept it: copy the full token from the database once (`select token from push_tokens ...`) and run the smoke script with `--expo-token '<token>'`. The phone must buzz and the script must print `Expo delivered the push to Firebase`.
4. Prove the **backend** sends one: with a test caregiver approved and online, make a booking from the patient app (pay with a Paystack test card); dispatch notifies the caregiver, whose phone should buzz. If step 3 worked and step 4 did not, read `$C logs worker api | grep -i push`.

## 12. Rollback and restore

| Situation | Do |
|---|---|
| A deploy fails (pull, backup, migration, a service unhealthy) | Nothing: `remote_deploy.sh` already restored the previous image for `api` and `worker` and failed the run. Read the log. |
| New image misbehaves after a green deploy, schema still compatible | **Actions > CD (staging) > Run workflow** with `image_tag` set to an earlier tag (`docs/deployment/ROLLBACK.md`). Most migrations here only **add** things, so the previous image keeps working on the new schema. |
| A migration broke data or an old image cannot run on the new schema | restore the dump the deploy took just before the migration (`docs/deployment/BACKUPS_GUIDE.md`, "Restore for real"), then roll the image back |
| Wrong env value | decrypt, edit, re-encrypt the env file in git, then deploy (`docs/deployment/ENV_ENCRYPTION.md`). Do not edit `.env` on the server: the next deploy overwrites it. |
| Everything wrong on a brand-new, empty server | `cd /opt/oakly-staging && docker compose -f docker-compose.prod.yml --env-file .env down -v` and re-run the deploy (**`-v` deletes the database**; never on a server with data) |

## 13. Turning voice notes on (after push works)

`VOICE_NOTES_ENABLED=false` for the first deploy. Turn it on only after (a) section 11's push test passed, (b) `GEMINI_API_KEY` is a **paid, non-training tier** key, (c) the owner has set the daily audio budget (`VOICE_NOTE_DAILY_AUDIO_SECONDS_CAP`) and the retention days (`VOICE_NOTE_RETENTION_DAYS`, 365 is only a proposal). Decrypt, edit and re-encrypt `.env.staging` (`docs/deployment/ENV_ENCRYPTION.md`), deploy, record a real voice note from the phone. A synthetic-speech smoke on 2026-11-08 showed `gemini-3.5-flash` accepts m4a and mp3 (`docs/sop/2026-11-08-ai-note-transcription.md`); a real phone recording and a real Pidgin sample are still unproven. It is also the kill switch: set it back to `false` and recreate.

## 14. The deploy pipeline (replaces the 2026-11 draft workflow)

The draft `deploy-staging.yml` (manual, build-on-dispatch, per-environment key pair, git checkout on the server) is gone. Its job is done by `cd-staging.yml` and `cd-production.yml` (build once on `develop`, deploy by digest, smoke test, then promote the same image to `main`). One-time GitHub setup, step by step for the owner: `docs/deployment/GITHUB_ACTIONS_SETUP.md`. How it works: `docs/deployment/CICD_PIPELINE.md`. The pipeline is **off** until the repository variable `STAGING_DEPLOY_ENABLED=true` exists (and `PRODUCTION_DEPLOY_ENABLED=true` for production, only after the owner's sign-off).

Not run on a real server yet (section 16). The actions are pinned to commit SHAs; `.github/dependabot.yml` proposes bumps weekly.

## 15. Operate: cheat sheet

```bash
cd /opt/oakly-staging        # production: /opt/oakly-production (port 8101)
C="docker compose -f docker-compose.prod.yml --env-file .env"
$C ps                                  # state and health
$C logs -f --tail 100 api worker       # logs (rotated: 10 MB x 5 per container)
$C restart worker                      # restart one service
$C exec db psql -U oakly -d oakly      # a SQL prompt
$C exec api alembic current            # which migration the database is on
grep '^API_IMAGE=' .env                # the image digest running
df -h / && docker system df            # disk; `docker image prune -f` after many deploys
sudo certbot certificates              # certificate expiry
```
The `staging` URL structure the rest of the system needs: API `https://staging-api.<yourdomain>`; Paystack webhook `/api/v1/payment/paystack/webhook`; Dojah webhook `/api/v1/kyc/dojah/webhook`; `KYC_WIDGET_PUBLIC_BASE_URL` = the API origin.

## 16. What was verified offline, and what was not

Updated 2026-11-21 for the pipeline adoption (`docs/sop/2026-11-21-deployment-adoption.md` has the evidence).

| Piece | Status |
|---|---|
| `docker-compose.prod.yml` (one file, both stacks) | **Verified live on a scratch stack** (local Docker, throwaway keys, then removed): `docker compose config` parses; the stack boots, migrates to the Alembic head (0027) and all four services become healthy; the project name, port and subnet come from `.env`. **Not run:** with two stacks side by side on one host (the port, subnet and name differences are tested only by the example-file tests). |
| `scripts/remote_deploy.sh` | **Verified live on the scratch stack:** first deploy; redeploy of the same digest (previous image found by compose labels); a **deliberately broken image** (exits at start): the deploy failed, `API_IMAGE` went back to the previous digest, the API answered 200 again, exit code 1. `shellcheck` clean. **Not run:** over SSH from GitHub Actions, against GHCR with the run's token, or with the crontab step. |
| `scripts/backup_cron.sh`, `scripts/restore_test.sh` | **Verified live on the scratch stack:** a dump and an uploads archive were written, the dump listed, and the restore test loaded it into a scratch database (70 tables, revision 0027) and dropped it. **Not verified:** the restore-over-live sequence in `docs/deployment/BACKUPS_GUIDE.md` was not re-run after the move; the same commands were exercised on 2026-11-10 against a throwaway Postgres with a fake schema. |
| `deploy/nginx/*.conf` | **`nginx -t` passes** on current stable nginx with both sites rendered and throwaway certificates (two harmless `http2` warnings). **Not run:** with real certificates, a real domain, real traffic; `TRUSTED_PROXIES` matching the real bridge address is unproven until section 10's last note |
| `deploy/env.example` | **Verified by test:** names every one of the backend's settings plus the compose-only keys, nothing Settings would refuse, loads as a Settings env file, `TRUSTED_PROXIES` equals `DOCKER_SUBNET`, and the compose file is free of environment-specific values |
| `scripts/env.sh` | **Verified on throwaway data:** encrypt, verify, decrypt, a wrong key refused, `diff` printing names only |
| `scripts/staging_smoke.py` | **Verified against the scratch stack:** 5 checks pass (health, OpenAPI, both webhooks, pricing) with a fake pricing row, and the pricing check fails with 503 on an empty database as designed; the email, payment and push steps still need a person. **Not run against a real server over TLS.** |
| Workflows | `actionlint` clean; every pinned action SHA exists upstream. **Never run on GitHub**: the first run is the adoption pull request's CI (tests in 4 shards, Semgrep, Trivy on the image), whose result is the authority for those three. The CD workflows are switched off until the repository variables are set. |
| `arq --check` worker healthcheck | **Verified live** earlier against a throwaway Redis, and again as part of the scratch stack's `--wait` (the worker became healthy) |
| Mobile section | The `eas.json` diff is **proposed, not applied or built**; `eas credentials` menu wording is from memory of the CLI |
| Provider recommendations, memory sizing, prices | **Estimates**, not measured |

## 17. Follow-ups and known gaps

1. **Run it once on a real server** and fix whatever the first run finds; then update section 16.
2. **Admin console on staging** (open decision 12 in section 0).
3. **Off-server backups** (bucket or provider snapshots) and a failure alert on the backup cron (e.g. a healthchecks.io ping).
4. ~~Image scan and Semgrep before deploy~~: now in CI (`ci.yml`: Semgrep job, Trivy on PRs). Their first results come with the adoption PR.
5. `/docs` and `/api/v1/openapi.json` are public on staging; put basic auth in front in nginx if that matters.
6. **Prescription note files** go to `/app/uploaded_notes` (a volume now). The legacy `/app/uploads/profile_images` mount is not persisted (profile photos use Cloudinary).
7. ~~Unused settings~~ removed 2026-11-21 (`FCM_SERVER_KEY`, `APNS_*`, `PAYSTACK_WEBHOOK_SECRET`, `SMTP_TLS`, `OTP_LENGTH`, `KYC_LIVENESS_FLOOR`, `PAYMENT_CURRENCY`, the deposit and withdrawal limits); `Settings` now ignores unknown keys.
8. Pin `requires-python` below 3.14 (or fix the worker start) so a fresh environment can never pick the interpreter `arq` breaks on.
9. Paystack: run the live test-mode **transfer** and **refund** (the owner disables OTP for API transfers first); Dojah live K8 pass; real push; voice-notes Gemini smoke (handoff 1.1 item 8).
