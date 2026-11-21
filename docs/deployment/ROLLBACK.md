# Rollback — Oakly API

There are two kinds of rollback. Try the automatic one's log first, then use the manual one.

## 1. Automatic (during a deploy)

If a deploy fails (pull, backup, migration, or a service not becoming healthy), `scripts/remote_deploy.sh` puts the previous image back for `api` and `worker`, waits for the API to answer, prints the logs and the newest backups, and fails the run. You do nothing; check the run log to see why it failed.

What it does **not** undo is the database. Migrations are not reversed, so keep every migration backward-compatible for one release (add columns as nullable first, drop old ones in a later release). Then the previous image still works on the new schema.

The schema escape hatch is the **pre-migration backup** every deploy takes first: `<DEPLOY_PATH>/backups/oakly-<stamp>.dump` (restore steps and a restore test: `BACKUPS_GUIDE.md`). A first deploy has nothing to roll back to: the stack is left as it is for inspection.

## 2. Manual (a bad release is already live)

Redeploy an earlier image. It must be one that was staging-verified; production refuses anything else. Your rollback window is the newest 10 verified images; older ones are removed by the weekly registry cleanup (`CICD_PIPELINE.md`).

1. Find the tag: the repository's **Packages** page, or the *Summary* of an earlier **CD (production)** run. Tags look like `sha-a1b2c3d`.
2. **Actions → CD (production) → Run workflow** → enter the tag in `image_tag` → Run.
3. The workflow looks up the image's digest, confirms a `verified-sha256-…` tag exists for those exact bytes, and deploys it (with approval, if the environment requires it).

Rollback to **staging** works the same way from **CD (staging) → Run workflow → `image_tag`**. It deploys the existing image and does not build or mark anything verified.

## "Production refuses: never staging-verified"

The image was built before this pipeline existed, or its staging run never passed. Options:

- Pick a different, verified tag (the normal answer).
- Re-deploy the commit through `develop` so it is built and verified (the safe answer).

There is intentionally no bypass switch.

## After a rollback

`main` still points at the bad commit. Fix forward on `develop`, wait for staging-verified, then fast-forward `main` (see `BRANCHING.md`). Do not push anything else to `main` until then.
