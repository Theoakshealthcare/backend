# Before you push: local checks, then let CI do the rest

CI runs the heavy work on every push (the full test suite in shards, the image build and scan, the smoke test on staging). Locally, run only the cheap checks. **Never run the full test suite, a whole shard, e2e, coverage or an image scan on the Mac** (owner rule, 2026-11-09): CI does it, and a local full run only wears the machine.

## Every push

From the repository root, with the project's own toolchain (`poetry run`, never a global tool):

```bash
poetry check --lock
poetry run ruff check --select E9,F63,F7,F82 .
poetry run python -m scripts.ci_smoke
poetry run pytest app/tests/<the test files for what you changed> -q -p no:warnings
```

The same commands are in `.github/workflows/ci.yml`; if one changes there, change it here. When you edited a generated FE guide or its captures, also run the `render_fe_guide ... --check` line for that guide (the eight of them are in `ci.yml`).

## When you touch the deployment files

| You changed | Run |
|---|---|
| `.github/workflows/*.yml` | `actionlint .github/workflows/*.yml` |
| `scripts/*.sh`, `boot/docker-run.sh` | `shellcheck` on the files you changed |
| `docker-compose.prod.yml` or `deploy/env.example` | `poetry run pytest app/tests/test_staging_env_example.py -q`; `docker compose -f docker-compose.prod.yml --env-file <a scratch env> config -q` |
| `deploy/nginx/*.conf` | render both sites (the sed in `docs/deploy/staging.md` section 6) and run `nginx -t` in a throwaway `nginx:alpine` container |
| `scripts/remote_deploy.sh` | exercise it against a scratch stack (local Docker, a local `registry:2`, throwaway keys), then `docker compose down -v` and remove the registry; the SOP `docs/sop/2026-11-21-deployment-adoption.md` lists the cases |
| a migration | one Alembic head, and an upgrade/downgrade round trip on a scratch database |

## Pull requests go to `develop`

CI runs on pull requests to `develop` only. A pull request against `main` gets no checks. Open yours with `gh pr create -R Theoakshealthcare/backend --base develop`.

## When CI goes red

Read CI's own output, reproduce only the failing step locally, fix, and push once. Do not run the whole pipeline locally to find it.
