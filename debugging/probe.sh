#!/usr/bin/env bash
# Fast hOn appliance-list API prober.
#
# Reuses cached tokens (from get_tokens.py) — no Home Assistant, no restart, no
# re-auth. Edit the PROBES below and re-run for instant feedback. Goal: find a
# request that returns appliance_count > 0 (pyhon's /commands/v1/appliance comes
# back empty against Haier's current API).
#
#   bash forks/hon/debugging/probe.sh
#
# Token source: forks/hon/debugging/.tokens.env (written by get_tokens.py), or
# export HON_COGNITO_TOKEN / HON_ID_TOKEN yourself.

set -uo pipefail
cd "$(dirname "$0")"

[ -f .tokens.env ] && source ./.tokens.env
: "${HON_COGNITO_TOKEN:?missing — run: uv run --with 'pyhOn==0.17.5' python get_tokens.py}"
: "${HON_ID_TOKEN:?missing — run get_tokens.py first}"
API_URL="${HON_API_URL:-https://api-iot.he.services}"
APP_VERSION="${HON_APP_VERSION:-2.6.5}"
UA="Chrome/999.999.999.999"
API_KEY='GRCqFhC6Gk@ikWXm1RmnSmX1cm,MxY-configuration'

echo "API_URL=$API_URL  APP_VERSION=$APP_VERSION"

# probe <label> <path> [extra curl -H args...]
probe() {
  local label="$1" path="$2"; shift 2
  local url="$API_URL$path" out status body count="?"
  out=$(curl -sS -m 20 -w $'\n%{http_code}' \
    -H "cognito-token: $HON_COGNITO_TOKEN" \
    -H "id-token: $HON_ID_TOKEN" \
    -H "user-agent: $UA" \
    -H "Content-Type: application/json" \
    "$@" "$url" 2>&1)
  status=$(printf '%s' "$out" | tail -n1)
  body=$(printf '%s' "$out" | sed '$d')
  if command -v jq >/dev/null 2>&1; then
    count=$(printf '%s' "$body" | jq -r \
      '(.payload.appliances // .appliances // .modules.applianceList.payload.appliances) | if type=="array" then length else "n/a" end' \
      2>/dev/null || echo "?")
  fi
  printf '\n[%s] status=%s appliance_count=%s\n  %s\n  body: %.500s\n' \
    "$label" "$status" "$count" "$url" "$body"
}

# ── PROBES ── add / edit freely ─────────────────────────────────────────────
probe "v1 baseline"          "/commands/v1/appliance"
probe "v1 +appVersion hdr"   "/commands/v1/appliance" -H "appVersion: $APP_VERSION" -H "appversion: $APP_VERSION"
probe "v1 +os/mobile hdrs"   "/commands/v1/appliance" -H "os: android" -H "appVersion: $APP_VERSION" -H "mobileId: pyhOn"
probe "v1 +x-api-key"        "/commands/v1/appliance" -H "x-api-key: $API_KEY"
probe "v2 appliance"         "/commands/v2/appliance"
probe "v1 appliance/ trail"  "/commands/v1/appliance/"
probe "auth introspection"   "/auth/v1/introspection"
# NEW unified-api endpoint (gvigroux/hon) — the one the current app uses:
probe "unified appliance-list" "/unified-api/v1/view/appliance-list" \
      -X POST --data-raw '{"deviceId":"homeassistant"}'
# ────────────────────────────────────────────────────────────────────────────
