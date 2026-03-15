#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "python-dotenv",
# ]
# ///
"""Discover OAuth endpoints for a Neo4j Aura Agent MCP server.

Walks the MCP OAuth discovery chain (RFC 9728 → RFC 8414 → RFC 7591)
and optionally registers an OAuth client via dynamic registration.
Outputs the values needed to configure a Databricks external MCP connection.

Usage:
    uv run discover-oauth.py                              # Discover + register client
    uv run discover-oauth.py --no-register                # Discovery only
    uv run discover-oauth.py --redirect-uri https://...   # Custom redirect URI
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


def load_config() -> str:
    """Load MCP_SERVER_URL from .env."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"[ERROR] .env file not found at {env_path}")
        print("Copy .env.sample to .env and fill in your MCP server URL:")
        print("  cp .env.sample .env")
        sys.exit(1)

    load_dotenv(env_path)

    mcp_url = os.getenv("MCP_SERVER_URL")
    if not mcp_url:
        print("[ERROR] MCP_SERVER_URL is not set in .env")
        print("Set it to the MCP server endpoint from the Aura Console:")
        print("  Agents → ... → Copy MCP server endpoint")
        sys.exit(1)

    return mcp_url


# ── Step 1: Probe MCP Server ─────────────────────────────────────

def probe_mcp_server(client: httpx.Client, mcp_url: str) -> str:
    """Send an unauthenticated request and extract resource_metadata URL from the 401."""
    print("=" * 60)
    print("STEP 1: Probe MCP Server")
    print("=" * 60)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "discover-oauth", "version": "1.0.0"},
        },
    }

    resp = client.post(mcp_url, json=payload)
    print(f"\n  POST {mcp_url}")
    print(f"  Status: {resp.status_code}")

    if resp.status_code != 401:
        print(f"  [ERROR] Expected 401, got {resp.status_code}")
        print(f"  Response: {resp.text[:300]}")
        sys.exit(1)

    www_auth = resp.headers.get("www-authenticate", "")
    print(f"  www-authenticate: {www_auth}")

    match = re.search(r'resource_metadata=(\S+)', www_auth)
    if not match:
        print("\n  [ERROR] No resource_metadata found in www-authenticate header")
        sys.exit(1)

    resource_metadata_url = match.group(1).strip(",")
    print(f"\n  Resource metadata URL extracted.")
    return resource_metadata_url


# ── Step 2: Fetch Resource Metadata ──────────────────────────────

def fetch_resource_metadata(client: httpx.Client, url: str) -> str:
    """Fetch OAuth Protected Resource Metadata (RFC 9728)."""
    print("\n" + "=" * 60)
    print("STEP 2: Fetch Resource Metadata (RFC 9728)")
    print("=" * 60)

    resp = client.get(url)
    print(f"\n  GET {url}")
    print(f"  Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"  [ERROR] {resp.text[:300]}")
        sys.exit(1)

    data = resp.json()
    servers = data.get("authorization_servers", [])
    scopes = data.get("scopes_supported", [])
    print(f"  Authorization servers: {servers}")
    print(f"  Scopes supported: {scopes}")

    if not servers:
        print("\n  [ERROR] No authorization_servers in resource metadata")
        sys.exit(1)

    return servers[0]


# ── Step 3: Fetch Authorization Server Metadata ──────────────────

def fetch_auth_server_metadata(client: httpx.Client, url: str) -> dict:
    """Fetch OAuth Authorization Server Metadata (RFC 8414)."""
    print("\n" + "=" * 60)
    print("STEP 3: Fetch Authorization Server Metadata (RFC 8414)")
    print("=" * 60)

    # The URL from resource metadata may be the issuer or a .well-known URL
    if "/.well-known/" not in url:
        well_known_url = f"{url.rstrip('/')}/.well-known/oauth-authorization-server"
    else:
        well_known_url = url

    resp = client.get(well_known_url)
    print(f"\n  GET {well_known_url}")
    print(f"  Status: {resp.status_code}")

    if resp.status_code != 200:
        print(f"  [ERROR] {resp.text[:300]}")
        sys.exit(1)

    data = resp.json()

    fields = [
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
        "scopes_supported",
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
    ]
    print()
    for field in fields:
        value = data.get(field)
        if value is not None:
            if isinstance(value, list):
                print(f"  {field}: {', '.join(str(v) for v in value)}")
            else:
                print(f"  {field}: {value}")

    required = ["authorization_endpoint", "token_endpoint"]
    for field in required:
        if field not in data:
            print(f"\n  [ERROR] Missing required field: {field}")
            sys.exit(1)

    return data


# ── Step 4: Register OAuth Client ────────────────────────────────

def register_client(client: httpx.Client, registration_endpoint: str, redirect_uri: str) -> tuple[str, str]:
    """Register an OAuth client via dynamic registration (RFC 7591)."""
    print("\n" + "=" * 60)
    print("STEP 4: Register OAuth Client (RFC 7591)")
    print("=" * 60)

    payload = {
        "client_name": "neo4j-mcp-databricks",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    print(f"\n  POST {registration_endpoint}")
    print(f"  Redirect URI: {redirect_uri}")

    resp = client.post(registration_endpoint, json=payload)

    if resp.status_code not in (200, 201):
        print(f"  [ERROR] Registration failed: {resp.status_code}")
        print(f"  {resp.text[:300]}")
        sys.exit(1)

    data = resp.json()
    client_id = data["client_id"]
    client_secret = data.get("client_secret", "")

    print(f"  Client registered successfully.\n")
    print(f"  OAUTH_CLIENT_ID={client_id}")
    print(f"  OAUTH_CLIENT_SECRET={client_secret}")
    print(f"\n  Add these to your .env file.")

    return client_id, client_secret


# ── Output: Databricks Configuration ─────────────────────────────

def print_databricks_config(
    auth_metadata: dict,
    mcp_url: str,
    client_id: str | None = None,
    client_secret: str | None = None,
):
    """Print Databricks external MCP connection settings."""
    parsed = urlparse(mcp_url)

    print("\n" + "=" * 60)
    print("DATABRICKS EXTERNAL MCP CONNECTION SETTINGS")
    print("=" * 60)

    print("\n  Step 1 — Connection basics:")
    print(f"    Connection name:     (your choice)")
    print(f"    Connection type:     HTTP")
    print(f"    Auth type:           OAuth User to Machine Per User")
    print(f"    OAuth provider:      Manual configuration")

    print(f"\n  Step 2 — Authentication:")
    print(f"    Host:                {parsed.scheme}://{parsed.hostname}")
    print(f"    Port:                {parsed.port or 443}")
    print(f"    OAuth scope:         openid profile email")
    if client_id:
        print(f"    Client ID:           {client_id}")
    else:
        print(f"    Client ID:           (run with --register or use discover-oauth.py)")
    if client_secret:
        print(f"    Client secret:       {client_secret}")
    else:
        print(f"    Client secret:       (from registration)")
    print(f"    Authorization endpoint: {auth_metadata['authorization_endpoint']}")
    print(f"    Token endpoint:      {auth_metadata['token_endpoint']}")

    print(f"\n  MCP Server URL:")
    print(f"    {mcp_url}")

    print(f"\n  IMPORTANT: Do NOT set an audience parameter.")
    print(f"  Auth0 defaults to audience=https://console.neo4j.io when")
    print(f"  omitted, which is what the MCP server validates against.")
    print(f"  Setting any explicit audience produces a 401.")


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Discover OAuth endpoints for a Neo4j Aura Agent MCP server"
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Skip dynamic client registration",
    )
    parser.add_argument(
        "--redirect-uri",
        default=None,
        help="Redirect URI for client registration (default: from .env or http://localhost:8400/callback)",
    )
    args = parser.parse_args()

    mcp_url = load_config()
    redirect_uri = args.redirect_uri or os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8400/callback")

    print(f"\nConfiguration loaded from .env:")
    print(f"  MCP Server URL: {mcp_url}")
    print()

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # Step 1: Probe
        resource_metadata_url = probe_mcp_server(client, mcp_url)

        # Step 2: Resource metadata
        auth_server_url = fetch_resource_metadata(client, resource_metadata_url)

        # Step 3: Auth server metadata
        auth_metadata = fetch_auth_server_metadata(client, auth_server_url)

        # Step 4: Register client (optional)
        client_id, client_secret = None, None
        if not args.no_register:
            reg_endpoint = auth_metadata.get("registration_endpoint")
            if not reg_endpoint:
                print("\n  [WARN] No registration_endpoint in metadata, skipping registration")
            else:
                client_id, client_secret = register_client(client, reg_endpoint, redirect_uri)
        else:
            print("\n" + "=" * 60)
            print("STEP 4: Skipped (--no-register)")
            print("=" * 60)

        # Output Databricks config
        print_databricks_config(auth_metadata, mcp_url, client_id, client_secret)

    # Summary
    print("\n" + "=" * 60)
    print("DISCOVERY COMPLETE")
    print("=" * 60)
    if client_id:
        print(f"\n  Next steps:")
        print(f"  1. Add OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET to .env")
        print(f"  2. Test the flow: uv run test-mcp-auth.py")
    else:
        print(f"\n  Next steps:")
        print(f"  1. Run again without --no-register to get client credentials")
        print(f"  2. Or register manually with: POST {auth_metadata.get('registration_endpoint', '(not available)')}")
    print()


if __name__ == "__main__":
    main()
