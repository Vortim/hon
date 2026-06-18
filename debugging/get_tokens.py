"""Fetch hOn API tokens once and cache them to .tokens.env for probe.sh.

Auth is the slow part (full Salesforce OAuth). Do it once here; then iterate
fast with probe.sh, which reuses the cached tokens (valid ~8h) via plain curl.

Run from the repo root:

    HON_EMAIL='you@example.com' HON_PASSWORD='secret' \
        uv run --with 'pyhOn==0.17.5' python forks/hon/debugging/get_tokens.py

Optional: HON_APP_VERSION='3.x.y' to log in claiming a newer app version.

Writes forks/hon/debugging/.tokens.env (gitignored). Re-run when probe.sh
starts returning 401/403 (token expired).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _apply_hon_scheme_patch() -> None:
    """Parse tokens from the hon:// OAuth callback instead of GETting it (aiohttp rejects it)."""
    from pyhon import exceptions
    from pyhon.connection.auth import HonAuth

    if getattr(HonAuth, "_custom_scheme_patched", False):
        return
    _original = HonAuth._manual_redirect

    async def _manual_redirect(self, url):  # type: ignore[no-untyped-def]
        if isinstance(url, str) and "oauth/done#access_token=" in url:
            self._parse_token_data(url)
            await self._api_auth()
            raise exceptions.HonNoAuthenticationNeeded()
        return await _original(self, url)

    HonAuth._manual_redirect = _manual_redirect  # type: ignore[method-assign]
    HonAuth._custom_scheme_patched = True


async def _main() -> int:
    email = os.environ.get("HON_EMAIL", "")
    password = os.environ.get("HON_PASSWORD", "")
    if not email or not password:
        print("error: set HON_EMAIL and HON_PASSWORD env vars", file=sys.stderr)
        return 2

    import aiohttp
    from pyhon import const
    from pyhon.connection.auth import HonAuth
    from pyhon.connection.device import HonDevice

    if app_version := os.environ.get("HON_APP_VERSION", ""):
        const.APP_VERSION = app_version

    _apply_hon_scheme_patch()

    async with aiohttp.ClientSession() as session:
        auth = HonAuth(session, email, password, HonDevice(const.MOBILE_ID))
        await auth.authenticate()
        if not (auth.cognito_token and auth.id_token):
            print("error: authenticated but tokens missing", file=sys.stderr)
            return 1
        out = Path(__file__).with_name(".tokens.env")
        out.write_text(
            f"export HON_COGNITO_TOKEN='{auth.cognito_token}'\n"
            f"export HON_ID_TOKEN='{auth.id_token}'\n"
            f"export HON_API_URL='{const.API_URL}'\n"
            f"export HON_APP_VERSION='{const.APP_VERSION}'\n"
        )
        print(f"wrote {out}  (cognito + id tokens cached; valid ~8h)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
