# GitHub Actions setup: Oakly API

This is the owner's click-by-click guide (work package WP6). Nothing here can be done from code: it is GitHub settings and a few commands on the server.

The pipeline is four workflows and one shared action in `.github/`:

| File | Trigger | What it does |
|---|---|---|
| `workflows/ci.yml` | PR to `develop`, push to `develop` | Lint and static checks, the test suite in 4 shards, Semgrep; on PRs also builds and scans the image (not pushed). One required check, `CI gate`. |
| `workflows/cd-staging.yml` | push to `develop`, manual | Build once, push to GHCR, deploy staging by digest, smoke test, mark the image verified. |
| `workflows/cd-production.yml` | push to `main`, manual (rollback) | Deploy the verified image. Never builds. |
| `workflows/registry-cleanup.yml` | weekly; manual (dry run by default) | Prunes old GHCR image versions, keeping the rollback window. |
| `actions/deploy-stack` | called by both CD workflows | Decrypt the env file, ship it, run `scripts/remote_deploy.sh` on the VPS (pull, backup, migrate, start, health, rollback). |

How they fit together: `CICD_PIPELINE.md`. How to release: `BRANCHING.md`.

**The CD workflows are switched off.** Every job in `cd-staging.yml` needs the repository variable `STAGING_DEPLOY_ENABLED=true`, and `cd-production.yml` needs `PRODUCTION_DEPLOY_ENABLED=true`. Until you set them, merging to `develop` or `main` deploys nothing. Set the staging one in step 6 below, once the VPS and the secrets exist; set the production one only at the first release (decision DEP-5: after the clinician sign-off and the live Meta, Paystack and Dojah passes).

## Before you start

You need: the VPS (8 GB RAM, 4 vCPU) with Docker and nginx set up by `docs/deploy/staging.md` sections 1-4, two DNS names (staging and production API), and the encrypted env files. **Do not create `.env.staging.enc` or `.env.production.enc` until the leaked keys are rotated** (Paystack, Zoho, Gemini, Cloudinary, the pasted Prembly key): ciphertext in git history stays there for good. Runbook `docs/sop/2026-10-05-secrets-and-rotation.md` has the rotation steps.

## Step 1. SSH key for GitHub Actions to the VPS

On your laptop:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/oakly_deploy -C "gha-oakly"
ssh-copy-id -i ~/.ssh/oakly_deploy.pub deploy@<vps-ip>
ssh -i ~/.ssh/oakly_deploy deploy@<vps-ip> 'echo ok'
```

The last command must print `ok`. A key with no passphrase is simplest; if you set one, add the secret `VPS_SSH_PASSPHRASE` too.

Then take the server's **host key fingerprint**, so GitHub Actions talks only to your server and never to an impostor. Run it from a network you trust (your own laptop at home, not a café):

```bash
ssh-keyscan -t ed25519 <vps-ip> 2>/dev/null | ssh-keygen -lf - | awk '{print $2}'
```

It prints one line starting `SHA256:`. That is the secret `VPS_SSH_FINGERPRINT` in step 4. To double-check, run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server itself: the two must match.

Use a **separate keypair for each Environment** if you can (repeat the three commands with `oakly_deploy_staging` and `oakly_deploy_production`, both public keys in the `deploy` user's `authorized_keys`): a leaked staging secret then does not hand over production's key.

## Step 2. The two stack directories on the VPS

Staging and production are two stacks on the one server, kept apart by directory, port, network and project name. On the VPS, as `deploy`:

```bash
sudo install -d -o deploy -g deploy /opt/oakly-staging /opt/oakly-production
```

No `git clone` is needed on the server any more: the deploy copies the compose file, the `.env` and three scripts into these directories, and pulls the image from GitHub's registry.

## Step 3. GitHub Environments

In the repo `Theoakshealthcare/backend`: **Settings > Environments > New environment**. Create `staging`, then `production`.

For each: **Deployment branches and tags > Selected branches and tags > Add deployment branch or tag rule**:

| Environment | Rule | Why |
|---|---|---|
| `staging` | `develop` | Only workflows running on `develop` can read staging secrets. |
| `production` | `main` | Only workflows running on `main` can read production secrets. |

Without this, anyone who can push a branch could change a workflow and run it with the environment's secrets. This setting is the real control; the `github.ref` checks in the workflows only stop accidental runs.

On `production` also tick **Required reviewers** and add yourself: every production deploy then waits for your click.

## Step 4. Secrets and variables, per Environment

Open each Environment and add (**Add environment secret** / **Add environment variable**):

| Secret | `staging` | `production` |
|---|---|---|
| `VPS_HOST` | the VPS IP or hostname | the same |
| `VPS_USERNAME` | `deploy` | `deploy` |
| `VPS_SSH_KEY` | the whole contents of the staging private key (including the BEGIN and END lines) | the production key (or the same one) |
| `VPS_SSH_FINGERPRINT` | the `SHA256:...` line from step 1 | the same (one server) |
| `VPS_SSH_PASSPHRASE` | only if the key has one | same |
| `DEPLOY_PATH` | exactly `/opt/oakly-staging` | exactly `/opt/oakly-production` |
| `ENV_ENCRYPTION_KEY` | key for `.env.staging.enc` | a DIFFERENT key for `.env.production.enc` |

The deploy refuses any `DEPLOY_PATH` other than those two, and refuses an env file whose `COMPOSE_PROJECT_NAME` is not `oakly-<environment>`, so the two stacks cannot be mixed up.

| Variable | `staging` | `production` |
|---|---|---|
| `APP_URL` | `https://api.staging.<yourdomain>` | `https://api.<yourdomain>` |
| `VPS_PORT` | `22` (optional) | `22` (optional) |

Generate each key on your laptop with `./scripts/env.sh generate-key staging` (then `production`); it prints the key once. Paste it into the GitHub secret and your password manager. Never paste it into chat.

There is **no registry secret to create**: the VPS pulls the image with the workflow run's own short-lived token.

## Step 5. Branch rules

**Settings > Rules > Rulesets > New branch ruleset** (or **Settings > Branches**):

| Branch | Setting | Why |
|---|---|---|
| `develop` | Require a pull request before merging, and require the status check **`CI gate`** | Every change is tested before it reaches staging. The check is selectable only after CI has run once on a PR: open the adoption PR first. |
| `main` | **Require linear history** | Only fast-forwards can land, so production runs a commit staging tested. |
| `main` | Block force pushes and deletions | Keeps production history intact. |
| `main` | Do **not** require a pull request | Releasing is a direct fast-forward push (`BRANCHING.md`). |

Then **Settings > General > Default branch**: switch it to `develop`, so new pull requests target `develop` by default. **This matters**: CI runs on pull requests to `develop` only. A pull request opened against `main` gets no checks at all.

## Step 6. Switch staging on

Only when the VPS, the DNS names, nginx and the encrypted staging env file all exist (`docs/deploy/staging.md` sections 1-9):

**Settings > Secrets and variables > Actions > Variables > New repository variable**: name `STAGING_DEPLOY_ENABLED`, value `true`.

The next push to `develop` builds, deploys and tests staging. Leave `PRODUCTION_DEPLOY_ENABLED` unset.

## Verifying a deploy on the VPS

```bash
cd /opt/oakly-staging            # or /opt/oakly-production
docker compose -f docker-compose.prod.yml --env-file .env ps
docker compose -f docker-compose.prod.yml --env-file .env logs --tail=100 api
curl -sS http://127.0.0.1:8000/health     # staging 8000, production 8001
grep '^API_IMAGE=' .env                   # the exact digest running (an image name, not a secret)
```

## Rollback

Automatic on a failed deploy; manual by redeploying an earlier verified tag. See `ROLLBACK.md`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Empty deploy input(s): ...` | The named secret is unset in that GitHub Environment. |
| `DEPLOY_PATH must be /opt/oakly-staging or /opt/oakly-production` | The secret holds another value. |
| `sets COMPOSE_PROJECT_NAME='...', expected 'oakly-staging'` | The wrong env file was encrypted for that environment. |
| `Permission denied (publickey)` | The key is not on the VPS: rerun `ssh-copy-id` (step 1). |
| `VPS_SSH_FINGERPRINT must look like SHA256:...` or a host-key mismatch | The secret is missing or wrong, or the server was rebuilt (a new server has a new host key): take the fingerprint again (step 1). A mismatch you did not cause is a reason to stop and check the server. |
| scp or `mkdir` permission error on `/opt/...` | Step 2 skipped: the `deploy` user must own both directories. |
| `denied` / `unauthorized` pulling the image | The job needs `permissions: packages: read` (the workflows set it). If the package belongs to another repo, link it under the package's settings. |
| All CD jobs show as skipped | The variable `STAGING_DEPLOY_ENABLED` (or `PRODUCTION_DEPLOY_ENABLED`) is not `true`: that is the off-switch working. |
| Production: "No image for this commit" | `main` was not a fast-forward of `develop`. See `BRANCHING.md`. |
| Production: "Not verified on staging" | The staging run for that commit failed, was cancelled, or is still running. |
| Smoke test fails on "Pricing row exists" with 503 | Normal on a fresh staging: publish the one-time pricing row (`docs/deploy/staging.md` section 9.2), then re-run the failed workflow. |
| Health check never returns 200 | `docker compose ... logs --tail=200 api` on the VPS. Most common cause: a missing variable in the env file. |
| `Could not decrypt .env.<env>.enc` | Wrong `ENV_ENCRYPTION_KEY` for that environment. |
| Registry cleanup: `FAILED to delete ...` / 403 | Give the repository the **Admin** role on the package: Package settings > Manage Actions access. |

## Where each secret is used

| Secret / variable | Used in |
|---|---|
| `VPS_*` (including `VPS_SSH_FINGERPRINT`), `DEPLOY_PATH`, `ENV_ENCRYPTION_KEY` | `actions/deploy-stack` (called by both CD workflows) |
| `APP_URL` | `cd-staging.yml` smoke-test target; shown as the Environment URL |
| `STAGING_DEPLOY_ENABLED`, `PRODUCTION_DEPLOY_ENABLED` (repository variables) | the off-switches in `cd-staging.yml` and `cd-production.yml` |
| `GITHUB_TOKEN` (automatic) | GHCR push in `cd-staging.yml`; GHCR pull on the VPS |

The CI test job needs no secrets: it uses a throwaway key written in `ci.yml` and a throwaway Postgres from testcontainers.
