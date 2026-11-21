#!/usr/bin/env bash
#
# Delete old image versions from a GHCR package, keeping what a rollback needs.
#
#   scripts/ghcr_prune.sh
#
# Configured by environment variables (the workflow sets them):
#
#   GH_TOKEN        token with packages: write (in Actions: github.token)
#   OWNER           GitHub user or organisation that owns the package
#   PACKAGE         package name (the repository name, lowercase)
#   DRY_RUN         "true" (default) only lists what would be deleted
#   KEEP_NEWEST     keep this many newest versions of any kind        (default 20)
#   KEEP_VERIFIED   keep this many newest staging-verified versions   (default 10)
#   MIN_AGE_DAYS    never delete a version younger than this          (default 14)
#   PROTECT_TAGS    space-separated tags to keep, e.g. "sha-a1b2c3d sha-d4e5f6a"
#                   (the workflow passes the tips of main and develop)
#
# A version is deleted only if ALL of these are true:
#   - it is not among the newest KEEP_NEWEST versions
#   - it is not among the newest KEEP_VERIFIED staging-verified versions
#     (tag staging-verified-* or verified-sha256-*; these are rollback targets)
#   - it carries none of PROTECT_TAGS
#   - it is older than MIN_AGE_DAYS
#
# Test hooks (no network needed):
#   VERSIONS_FILE   read the version list from this JSON file instead of the API
#   NOW_EPOCH       pretend "now" is this Unix time
#
set -euo pipefail

DRY_RUN="${DRY_RUN:-true}"
KEEP_NEWEST="${KEEP_NEWEST:-20}"
KEEP_VERIFIED="${KEEP_VERIFIED:-10}"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-14}"
PROTECT_TAGS="${PROTECT_TAGS:-}"
NOW_EPOCH="${NOW_EPOCH:-$(date +%s)}"

: "${OWNER:?OWNER is required}"
: "${PACKAGE:?PACKAGE is required}"

for n in KEEP_NEWEST KEEP_VERIFIED MIN_AGE_DAYS NOW_EPOCH; do
  [[ "${!n}" =~ ^[0-9]+$ ]] || { echo "ghcr_prune: $n must be a number (got '${!n}')" >&2; exit 1; }
done
[ "${KEEP_NEWEST}" -ge 1 ] || { echo "ghcr_prune: KEEP_NEWEST must be at least 1" >&2; exit 1; }

# --- 1. The version list ----------------------------------------------------- #
if [ -n "${VERSIONS_FILE:-}" ]; then
  VERSIONS="$(cat "${VERSIONS_FILE}")"
  KIND="${KIND:-users}"
else
  # Users and organisations use different API paths.
  OWNER_TYPE="$(gh api "users/${OWNER}" --jq .type)"
  if [ "${OWNER_TYPE}" = "Organization" ]; then KIND=orgs; else KIND=users; fi
  # --paginate prints one JSON array per page; `jq -s add` joins them.
  VERSIONS="$(gh api --paginate "/${KIND}/${OWNER}/packages/container/${PACKAGE}/versions?per_page=100" | jq -s 'add // []')"
fi

TOTAL="$(printf '%s' "${VERSIONS}" | jq 'length')"
echo "Package ${OWNER}/${PACKAGE}: ${TOTAL} versions"

# --- 2. Pick the ones to delete ---------------------------------------------- #
CANDIDATES="$(printf '%s' "${VERSIONS}" | jq -r \
  --argjson kn "${KEEP_NEWEST}" --argjson kv "${KEEP_VERIFIED}" \
  --argjson age "${MIN_AGE_DAYS}" --argjson now "${NOW_EPOCH}" \
  --arg protect "${PROTECT_TAGS}" '
  def tags: (.metadata.container.tags // []);
  def isverified: tags | any(startswith("staging-verified-") or startswith("verified-sha256-"));
  ($protect | split(" ") | map(select(length > 0))) as $prot
  | (sort_by(.created_at) | reverse) as $v
  | ($v | map(select(isverified)) | .[0:$kv] | map(.id)) as $keep_verified
  | ($v | .[0:$kn] | map(.id)) as $keep_newest
  | $v
  | map(select(
      ((.id as $i | $keep_verified | index($i)) == null)
      and ((.id as $i | $keep_newest | index($i)) == null)
      and ((tags | any(. as $t | $prot | index($t) != null)) | not)
      and (((.created_at | fromdateiso8601) + ($age * 86400)) < $now)
    ))
  | .[] | [.id, .created_at, (tags | if length == 0 then "<untagged>" else join(",") end)] | @tsv')"

if [ -z "${CANDIDATES}" ]; then
  echo "Nothing to delete."
  exit 0
fi

COUNT="$(printf '%s\n' "${CANDIDATES}" | wc -l | tr -d ' ')"
echo "${COUNT} version(s) eligible for deletion (keeping ${KEEP_NEWEST} newest, ${KEEP_VERIFIED} newest verified, protected tags, and anything younger than ${MIN_AGE_DAYS} days):"
printf '%s\n' "${CANDIDATES}" | awk -F'\t' '{ printf "  %s  %s  %s\n", $1, $2, $3 }'

if [ "${DRY_RUN}" != "false" ]; then
  echo "DRY RUN: nothing deleted. Run with DRY_RUN=false to delete."
  exit 0
fi

# --- 3. Delete --------------------------------------------------------------- #
FAILED=0
while IFS=$'\t' read -r id _created _tags; do
  if gh api -X DELETE "/${KIND}/${OWNER}/packages/container/${PACKAGE}/versions/${id}" >/dev/null 2>&1; then
    echo "deleted ${id}"
  else
    echo "FAILED to delete ${id}" >&2
    FAILED=$((FAILED + 1))
  fi
done <<< "${CANDIDATES}"

if [ "${FAILED}" -gt 0 ]; then
  echo "ghcr_prune: ${FAILED} deletion(s) failed. If these are 403s, give this repository the Admin role on the package (Package settings -> Manage Actions access)." >&2
  exit 1
fi
echo "Deleted ${COUNT} version(s)."
