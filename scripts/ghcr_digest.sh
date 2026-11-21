#!/usr/bin/env bash
#
# Resolve a GHCR reference (tag or digest) to its immutable manifest digest.
#
#   scripts/ghcr_digest.sh <repository-path> <reference>
#
#   <repository-path>  lowercase owner/name, e.g. my-org/my-api
#   <reference>        a tag (sha-a1b2c3d) or a digest (sha256:...)
#
# Prints the digest (sha256:...) on stdout. Everything else goes to stderr, so
# the caller can safely do  DIGEST="$(scripts/ghcr_digest.sh ...)".
#
# EXIT CODES - the caller distinguishes these, so they are part of the contract:
#   0  resolved; the digest is on stdout
#   2  the reference does not exist in the registry (HTTP 404)
#   1  anything else (auth failure, network, malformed response)
#
# Exit 2 is separate from exit 1 ON PURPOSE. "this image was never built" and
# "the registry rejected our credentials" demand completely different responses
# from a deploy pipeline, and collapsing them into one non-zero exit is how a
# credentials outage gets misdiagnosed as an un-promotable commit.
#
# AUTHENTICATION
#   Set GHCR_USERNAME and GHCR_PASSWORD for a private package (in Actions:
#   GHCR_USERNAME=${{ github.actor }}, GHCR_PASSWORD=${{ secrets.GITHUB_TOKEN }}
#   with `permissions: packages: read`). With neither set, an anonymous pull
#   token is requested, which works for public packages only.
#
# WHY THE RAW REGISTRY API AND NOT `docker buildx imagetools inspect`
#   imagetools collapses "not found" and "not authorised" into a generic
#   non-zero exit with prose on stderr, so the pipeline would have to pattern
#   match English error text to tell them apart. A HEAD against the manifest
#   endpoint returns an unambiguous status code, needs no docker daemon, and
#   costs one HTTP round trip.
#
set -euo pipefail

REPO="${1:?usage: ghcr_digest.sh <repository-path> <reference>}"
REF="${2:?usage: ghcr_digest.sh <repository-path> <reference>}"

REGISTRY_HOST="${GHCR_REGISTRY_HOST:-ghcr.io}"

# The registry only returns a manifest for a media type the client accepts.
# BuildKit pushes an OCI image INDEX here (the image plus its SBOM and SLSA
# provenance attestations), so `application/vnd.oci.image.index.v1+json` must be
# in this list or the index is invisible and the lookup 404s on an image that
# plainly exists. The Docker v2 types are listed too so this keeps working if
# the build ever stops producing attestations.
ACCEPT='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'

# --- 1. Pull token ---------------------------------------------------------- #
token_url="https://${REGISTRY_HOST}/token?scope=repository:${REPO}:pull&service=${REGISTRY_HOST}"

if [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_PASSWORD:-}" ]; then
  # -u keeps the credential out of the URL and therefore out of any proxy log.
  token_json="$(curl -sS --fail-with-body --max-time 30 \
    -u "${GHCR_USERNAME}:${GHCR_PASSWORD}" "${token_url}")" || {
    echo "ghcr_digest: could not obtain a pull token for ${REPO} (check GHCR_USERNAME/GHCR_PASSWORD and the packages:read permission)" >&2
    exit 1
  }
else
  token_json="$(curl -sS --fail-with-body --max-time 30 "${token_url}")" || {
    echo "ghcr_digest: could not obtain an anonymous pull token for ${REPO}" >&2
    exit 1
  }
fi

TOKEN="$(printf '%s' "${token_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
if [ -z "${TOKEN}" ]; then
  echo "ghcr_digest: the token endpoint returned no token for ${REPO}" >&2
  exit 1
fi

# --- 2. HEAD the manifest --------------------------------------------------- #
# HEAD, not GET: the digest travels in a response header, so there is no reason
# to transfer (and buffer) a manifest body that is then thrown away.
headers="$(curl -sS -I --max-time 30 \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: ${ACCEPT}" \
  "https://${REGISTRY_HOST}/v2/${REPO}/manifests/${REF}")" || {
  echo "ghcr_digest: the manifest request for ${REPO}:${REF} failed at the transport level" >&2
  exit 1
}

# Strip CR so the header values are usable in shell comparisons.
headers="$(printf '%s' "${headers}" | tr -d '\r')"

status="$(printf '%s\n' "${headers}" | sed -n 's|^HTTP/[0-9.]* \([0-9]\{3\}\).*|\1|p' | tail -n 1)"

case "${status}" in
  200)
    ;;
  404)
    echo "ghcr_digest: ${REGISTRY_HOST}/${REPO}:${REF} does not exist" >&2
    exit 2
    ;;
  401 | 403)
    echo "ghcr_digest: ${REGISTRY_HOST}/${REPO}:${REF} returned HTTP ${status} - authenticated as '${GHCR_USERNAME:-<anonymous>}' but not authorised to read this package" >&2
    exit 1
    ;;
  *)
    echo "ghcr_digest: unexpected HTTP ${status:-<none>} resolving ${REGISTRY_HOST}/${REPO}:${REF}" >&2
    exit 1
    ;;
esac

DIGEST="$(printf '%s\n' "${headers}" | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p' | tail -n 1)"

# A 200 with no digest header would mean the registry answered but told us
# nothing usable. Deploying on an empty string is never right, so refuse.
if ! printf '%s' "${DIGEST}" | grep -qE '^sha256:[a-f0-9]{64}$'; then
  echo "ghcr_digest: HTTP 200 for ${REPO}:${REF} but no usable Docker-Content-Digest header" >&2
  exit 1
fi

printf '%s\n' "${DIGEST}"
