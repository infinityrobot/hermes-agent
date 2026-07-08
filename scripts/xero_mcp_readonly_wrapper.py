#!/usr/bin/env python3
"""Launch the upstream Xero MCP server with simple profile-local auth.

Supported exactly as the upstream Xero MCP README documents:

1. Bearer-token mode: set XERO_CLIENT_BEARER_TOKEN.
2. Custom Connection mode: set XERO_CLIENT_ID and XERO_CLIENT_SECRET.
   XERO_SCOPES is optional; if absent, upstream defaults are used.

This wrapper intentionally does not implement token refresh, tenant-id handling,
or custom OAuth behavior. Its only jobs are to:

- strip unresolved Hermes placeholders such as ${XERO_CLIENT_SECRET};
- prefer bearer-token mode when a bearer token is present;
- launch the pinned commit of the Xero MCP server that fixes Custom Connection scope filtering.

Read-only behavior is enforced separately by Hermes MCP `tools.include`
filtering in each profile config.
"""
from __future__ import annotations

import os
import shutil
import sys

PLACEHOLDER_PREFIXES = ("${", "[REDACTED", "your_")
AUTH_ENV_KEYS = (
    "XERO_CLIENT_BEARER_TOKEN",
    "XERO_CLIENT_ID",
    "XERO_CLIENT_SECRET",
    "XERO_SCOPES",
)


def _real_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    lowered = value.lower()
    if value.startswith(PLACEHOLDER_PREFIXES) or lowered in {"redacted", "placeholder", "none", "null"}:
        return None
    return value


def main() -> int:
    env = os.environ.copy()

    bearer = _real_env("XERO_CLIENT_BEARER_TOKEN")
    client_id = _real_env("XERO_CLIENT_ID")
    client_secret = _real_env("XERO_CLIENT_SECRET")
    scopes = _real_env("XERO_SCOPES")

    for key in AUTH_ENV_KEYS:
        env.pop(key, None)

    if bearer:
        auth_mode = "bearer"
        env["XERO_CLIENT_BEARER_TOKEN"] = bearer
    elif client_id and client_secret:
        auth_mode = "custom_connection"
        env["XERO_CLIENT_ID"] = client_id
        env["XERO_CLIENT_SECRET"] = client_secret
        if scopes:
            env["XERO_SCOPES"] = scopes
    else:
        sys.stderr.write(
            "Xero MCP credentials are not configured. Set either "
            "XERO_CLIENT_BEARER_TOKEN, or XERO_CLIENT_ID plus "
            "XERO_CLIENT_SECRET, in this Hermes profile's .env.\n"
        )
        return 1

    if _real_env("XERO_MCP_WRAPPER_DRY_RUN"):
        print(f"auth_mode={auth_mode}")
        print(f"scopes_present={bool(scopes)}")
        print("launch=npx -y github:XeroAPI/xero-mcp-server#093449680c124d4cea4f40b5c5d2583e70db5ca2")
        print("secrets=redacted")
        return 0

    npx = shutil.which("npx")
    if not npx:
        sys.stderr.write("Could not find npx on PATH; install Node.js/npm or add npx to PATH.\n")
        return 127

    # Pin to the commit that fixes Custom Connection scope filtering (bug #175)
    # https://github.com/XeroAPI/xero-mcp-server/commit/093449680c124d4cea4f40b5c5d2583e70db5ca2
    os.execvpe(npx, [npx, "-y", "github:XeroAPI/xero-mcp-server#093449680c124d4cea4f40b5c5d2583e70db5ca2"], env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
