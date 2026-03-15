#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "python-dotenv",
# ]
# ///
"""Test the full OAuth + MCP flow for a Neo4j Aura Agent MCP server.

Performs OAuth Authorization Code + PKCE, then sends authenticated
MCP requests to verify the token works. Opens a browser for the
Auth0 login step.

Prerequisites:
    Run discover-oauth.py first to populate OAUTH_CLIENT_ID and
    OAUTH_CLIENT_SECRET in .env.

Usage:
    uv run test-mcp-auth.py
"""

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv


# ── Configuration ─────────────────────────────────────────────────

def load_config() -> dict:
    """Load and validate configuration from .env."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(f"[ERROR] .env file not found at {env_path}")
        print("Copy .env.sample to .env and fill in values:")
        print("  cp .env.sample .env")
        sys.exit(1)

    load_dotenv(env_path)

    config = {
        "mcp_url": os.getenv("MCP_SERVER_URL"),
        "client_id": os.getenv("OAUTH_CLIENT_ID"),
        "client_secret": os.getenv("OAUTH_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8400/callback"),
    }

    missing = [k for k in ("mcp_url", "client_id") if not config.get(k)]
    if missing:
        env_names = {"mcp_url": "MCP_SERVER_URL", "client_id": "OAUTH_CLIENT_ID"}
        print(f"[ERROR] Missing required variables in .env: {', '.join(env_names[k] for k in missing)}")
        print("\nRun discover-oauth.py first to register an OAuth client:")
        print("  uv run discover-oauth.py")
        sys.exit(1)

    return config


# ── OAuth Discovery ───────────────────────────────────────────────

def discover_endpoints(client: httpx.Client, mcp_url: str) -> dict:
    """Walk the MCP OAuth discovery chain to find auth endpoints."""
    # Probe for resource_metadata
    resp = client.post(
        mcp_url,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "test-mcp-auth", "version": "1.0.0"}},
        },
    )

    if resp.status_code != 401:
        print(f"  [ERROR] Expected 401, got {resp.status_code}")
        sys.exit(1)

    www_auth = resp.headers.get("www-authenticate", "")
    match = re.search(r'resource_metadata=(\S+)', www_auth)
    if not match:
        print("  [ERROR] No resource_metadata in www-authenticate header")
        sys.exit(1)

    resource_url = match.group(1).strip(",")

    # Fetch resource metadata
    data = client.get(resource_url).json()
    auth_server = data["authorization_servers"][0]

    # Fetch auth server metadata
    if "/.well-known/" not in auth_server:
        auth_server = f"{auth_server.rstrip('/')}/.well-known/oauth-authorization-server"

    metadata = client.get(auth_server).json()

    return {
        "authorization_endpoint": metadata["authorization_endpoint"],
        "token_endpoint": metadata["token_endpoint"],
    }


# ── PKCE ──────────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(43)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ── OAuth Browser Flow ────────────────────────────────────────────

def run_oauth_flow(
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> str:
    """Run Authorization Code + PKCE flow with a local callback server.

    IMPORTANT: The audience parameter is intentionally omitted.
    Auth0 defaults to audience=https://console.neo4j.io, which is
    what the Neo4j MCP server validates. Setting any explicit
    audience produces a 401.
    """
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    parsed_redirect = urlparse(redirect_uri)
    port = parsed_redirect.port or 8400

    # Build authorization URL — NO audience parameter
    auth_params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    auth_url = f"{authorization_endpoint}?{auth_params}"

    # Local callback server
    result = {"code": None, "error": None}
    received = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                result["code"] = params["code"][0]
            else:
                result["error"] = params.get("error", ["unknown"])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result["code"]:
                self.wfile.write(
                    b"<h2>Authorization successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                )
            else:
                self.wfile.write(f"<h2>Error: {result['error']}</h2>".encode())
            received.set()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("localhost", port), CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    print(f"  Callback server listening on http://localhost:{port}")
    print(f"  Opening browser for Neo4j Aura login ...\n")
    webbrowser.open(auth_url)

    print(f"  Waiting for authorization (timeout: 120s) ...")
    received.wait(timeout=120)
    server.server_close()

    if result["error"]:
        print(f"  [ERROR] OAuth error: {result['error']}")
        sys.exit(1)
    if not result["code"]:
        print(f"  [ERROR] Timed out waiting for authorization code")
        sys.exit(1)

    print(f"  Authorization code received. Exchanging for token ...")

    # Exchange code for token
    token_data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            token_endpoint,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code != 200:
        print(f"  [ERROR] Token exchange failed: {resp.status_code}")
        print(f"  {resp.text[:300]}")
        sys.exit(1)

    body = resp.json()
    token = body["access_token"]
    expires = body.get("expires_in", "unknown")

    print(f"  Access token received (expires in {expires}s)")

    # Show token audience for debugging
    if token.startswith("eyJ"):
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload_b64))
            print(f"  Token audience: {decoded.get('aud')}")
        except Exception:
            pass

    return token


# ── MCP Tests ─────────────────────────────────────────────────────

def test_mcp_initialize(client: httpx.Client, mcp_url: str, token: str) -> bool:
    """Send MCP initialize request."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-mcp-auth", "version": "1.0.0"},
        },
    }

    resp = client.post(
        mcp_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )

    if resp.status_code != 200:
        print(f"  [FAIL] HTTP {resp.status_code}")
        print(f"  {resp.text[:300]}")
        return False

    # Parse SSE response if needed
    body = resp.text
    if body.startswith("event:") or body.startswith("data:"):
        for line in body.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                break
        else:
            print(f"  [FAIL] No data in SSE response")
            return False
    else:
        data = resp.json()

    result = data.get("result", {})
    server_info = result.get("serverInfo", {})
    protocol = result.get("protocolVersion", "unknown")

    print(f"  Server: {server_info.get('name', 'unknown')} {server_info.get('version', '')}")
    print(f"  Protocol: {protocol}")
    print(f"  Capabilities: {list(result.get('capabilities', {}).keys())}")
    return True


def test_mcp_tools_list(client: httpx.Client, mcp_url: str, token: str, session_id: str | None) -> bool:
    """Send MCP tools/list request."""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    resp = client.post(mcp_url, json=payload, headers=headers)

    if resp.status_code != 200:
        print(f"  [FAIL] HTTP {resp.status_code}")
        print(f"  {resp.text[:300]}")
        return False

    # Parse SSE response if needed
    body = resp.text
    if body.startswith("event:") or body.startswith("data:"):
        for line in body.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                break
        else:
            print(f"  [FAIL] No data in SSE response")
            return False
    else:
        data = resp.json()

    tools = data.get("result", {}).get("tools", [])
    print(f"  Available tools: {len(tools)}")
    for tool in tools:
        desc = tool.get("description", "")
        if len(desc) > 60:
            desc = desc[:57] + "..."
        print(f"    - {tool['name']}: {desc}")

    return True


# ── Main ──────────────────────────────────────────────────────────

def main():
    config = load_config()

    print(f"\nConfiguration loaded from .env:")
    print(f"  MCP Server URL: {config['mcp_url']}")
    print(f"  Client ID:      {config['client_id']}")
    print(f"  Redirect URI:   {config['redirect_uri']}")
    print()

    results = {}

    # Step 1: Discover endpoints
    print("=" * 60)
    print("STEP 1: OAuth Endpoint Discovery")
    print("=" * 60)

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        endpoints = discover_endpoints(client, config["mcp_url"])

    print(f"\n  Authorization: {endpoints['authorization_endpoint']}")
    print(f"  Token:         {endpoints['token_endpoint']}")
    results["discovery"] = True

    # Step 2: Browser authorization
    print("\n" + "=" * 60)
    print("STEP 2: Browser Authorization (OAuth + PKCE)")
    print("=" * 60)
    print()

    token = run_oauth_flow(
        authorization_endpoint=endpoints["authorization_endpoint"],
        token_endpoint=endpoints["token_endpoint"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["redirect_uri"],
    )
    results["auth"] = True

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # Step 3: MCP initialize
        print("\n" + "=" * 60)
        print("STEP 3: MCP Initialize")
        print("=" * 60)
        print(f"\n  POST {config['mcp_url']}")

        results["initialize"] = test_mcp_initialize(client, config["mcp_url"], token)

        # Step 4: List tools (requires a fresh session)
        print("\n" + "=" * 60)
        print("STEP 4: MCP Tools List")
        print("=" * 60)
        print()

        # Initialize a new session to get the session ID for tools/list
        init_resp = client.post(
            config["mcp_url"],
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test-mcp-auth", "version": "1.0.0"}},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
        session_id = init_resp.headers.get("mcp-session-id")
        results["tools"] = test_mcp_tools_list(client, config["mcp_url"], token, session_id)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  OAuth Discovery:    {'PASS' if results.get('discovery') else 'FAIL'}")
    print(f"  Browser Auth:       {'PASS' if results.get('auth') else 'FAIL'}")
    print(f"  MCP Initialize:     {'PASS' if results.get('initialize') else 'FAIL'}")
    print(f"  MCP Tools List:     {'PASS' if results.get('tools') else 'FAIL'}")
    print()

    if all(results.values()):
        print("  The full OAuth + MCP pipeline is working.")
        print("  This configuration should work for Databricks external MCP connections.")
    else:
        print("  Some steps failed. Check the output above for details.")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
