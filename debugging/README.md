# hOn API debugging

Fast loop for reverse-engineering Haier's hOn cloud API without touching Home
Assistant. Auth (slow, ~seconds) happens once; probing (fast) is plain `curl`.

## Why

pyhon 0.17.5 logs in fine but `GET /commands/v1/appliance` returns
`{"payload":{"appliances":[]}}` against Haier's current API (the app still sees
the devices). These tools let you find the request that returns the appliances
— different endpoint, header, or app version — so the integration can be patched
to match.

## Use

1. Fetch tokens once (writes `.tokens.env`, gitignored, valid ~8h):

   ```bash
   HON_EMAIL='you@example.com' HON_PASSWORD='secret' \
     uv run --with 'pyhOn==0.17.5' python forks/hon/debugging/get_tokens.py
   ```

   Optional `HON_APP_VERSION='3.x.y'` to log in as a newer app version.

2. Probe — edit the `probe …` lines in `probe.sh`, then re-run as often as you
   like (no re-auth, no restart):

   ```bash
   bash forks/hon/debugging/probe.sh
   ```

   Look for `appliance_count > 0`. `jq` gives nicer counts if installed.

3. When `probe.sh` starts returning `status=401/403`, the tokens expired — re-run
   step 1.

## Notes

- `.tokens.env` holds live `cognito-token` / `id-token`. It's gitignored; rotate
  your hOn password when you're done debugging.
- The hOn login redirects to a custom-scheme `hon://…/oauth/done#access_token=…`
  URL that modern aiohttp rejects; `get_tokens.py` monkey-patches pyhon to parse
  it (the same fix carried in `custom_components/hon/__init__.py`).
