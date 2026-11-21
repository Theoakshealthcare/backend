# Branching and Releasing — Oakly API

Two long-lived branches:

| Branch | Meaning | What a push does |
|---|---|---|
| `develop` | Integration. Everything lands here through a PR. | `cd-staging.yml` builds the image once, deploys it to staging, runs the smoke test, and marks the image **staging-verified**. |
| `main` | Production. Always a commit that was verified on staging. | `cd-production.yml` finds the verified image for that commit and deploys it. It never builds. |

## The release rule: fast-forward `main` to `develop`

A fast-forward moves the `main` pointer to a commit that already exists on `develop`. No new commit is created, so `main` has **the same commit SHA** that staging tested, and production can find the image built for it.

A squash merge, a merge commit, or "rebase and merge" all create a **new SHA**. No image was built for it, so production refuses to deploy (it says so in the log).

## How to release

0. **The first release also needs the owner's sign-off** (decision DEP-5: the clinician sign-off and the live Meta, Paystack and Dojah passes), and the repository variable `PRODUCTION_DEPLOY_ENABLED=true`. Without the variable a push to `main` deploys nothing.
1. Open the **Actions** tab. On the `develop` tip, the **CD (staging)** run must be green, including the *Mark image as staging-verified* job.
2. Fast-forward `main`:

```bash
git fetch oakly
git push oakly oakly/develop:main
```

That one command pushes the remote `develop` commit to `main`. Git refuses it if it is not a fast-forward, which is the safety you want.

**The remote is named `oakly`, not `origin`.** In this checkout `origin` is the old `innocent98/backend` remote: never push there.

Equivalent, if you prefer to see it locally:

```bash
git checkout main
git pull --ff-only oakly main
git merge --ff-only oakly/develop     # fails if main has commits develop lacks
git push oakly main
```

3. Watch **CD (production)**. It resolves the verified image, asks for approval if the `production` environment has required reviewers, deploys, and rolls back on a failed health check.

> The GitHub **Merge** button for a develop→main PR cannot fast-forward. Do not open a PR from `develop` to `main`; push as above.

## One-time repo settings

**Settings → Branches (or Rulesets):**

| Branch | Setting | Why |
|---|---|---|
| `main` | **Require linear history** | Blocks merge commits, so only fast-forwards can land. |
| `main` | Restrict who can push (maintainers only) | A direct push is how you fast-forward. Do **not** also require a pull request on `main`, or your own fast-forward push will be rejected. |
| `main` | Block force pushes and deletion | Keeps production history intact. |
| `develop` | Require a pull request and the **CI gate** check | Every change is tested before it reaches staging. Also make `develop` the repository's default branch: CI runs on pull requests to `develop` only, and a pull request against `main` gets no checks. |
| Environment `production` | Deployment branches: `main` only | Production secrets are unreachable from any other branch. |
| Environment `staging` | Deployment branches: `develop` only | Same, for staging. |

## Hotfixes

Do not push a fix straight to `main`: no image exists for it, so production refuses it. Hotfix through `develop` (PR, merge, wait for staging-verified, fast-forward). If production is on fire, roll back first (see `ROLLBACK.md`), then fix forward.

## If `main` and `develop` have diverged

Someone merged into `main` by another route. Do not force-push. Reset the situation on `develop`:

```bash
git checkout develop
git merge oakly/main        # bring main's extra commits into develop
git push oakly develop      # staging builds and verifies the result
```

When staging is verified, `develop` contains everything on `main`, so the fast-forward works again.

## Tagging a release (optional)

A version tag is a label on a commit production already runs. It builds and deploys nothing:

```bash
git tag -a v1.4.0 -m "v1.4.0" && git push oakly v1.4.0
```
