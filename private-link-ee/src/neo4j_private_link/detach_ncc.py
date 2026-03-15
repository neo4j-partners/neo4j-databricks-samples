"""
Remove the Neo4j private endpoint rule and NCC from a Databricks workspace.

Detaches the NCC from the workspace (by swapping in an empty placeholder
NCC), deletes all private endpoint rules on the original NCC, then deletes
the NCC itself. Requires DATABRICKS_ACCOUNT_ID and NCC_ID in .env.

The Databricks API does not support unsetting the NCC on a workspace, so
this script creates a temporary empty NCC as a replacement. The empty NCC
is left in place and can be reused or deleted manually.

Usage:
    uv run detach-ncc --profile <databricks-cli-profile>
    uv run detach-ncc
"""

import json
import subprocess
import sys

from neo4j_private_link.helpers import load_env, optional_env, require_env


def get_token_from_profile(profile: str) -> str:
    """Get a token from a Databricks CLI profile."""
    result = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"  ERROR: Failed to get token from profile '{profile}'")
        print(f"  {result.stderr.strip()}")
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
        return data["access_token"]
    except (json.JSONDecodeError, KeyError):
        print(f"  ERROR: Unexpected output from databricks auth token: {result.stdout}")
        sys.exit(1)


def parse_args() -> str | None:
    """Parse --profile argument. Returns profile name or None."""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--profile" and i + 1 < len(args):
            return args[i + 1]
    return None


BASE_URL = "https://accounts.azuredatabricks.net/api/2.0/accounts"


def api_request(method: str, url: str, token: str, data: dict | None = None) -> dict | list:
    """Make an authenticated request to the Databricks Account API."""
    cmd = [
        "curl", "--silent", "--show-error", "--location",
        "--request", method, url,
        "--header", "Content-Type: application/json",
        "--header", f"Authorization: Bearer {token}",
    ]
    if data is not None:
        cmd.extend(["--data", json.dumps(data)])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        sys.exit(1)

    if not result.stdout.strip():
        return {}

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  ERROR: Unexpected response: {result.stdout}")
        sys.exit(1)

    if "error_code" in response or "error" in response:
        error = response.get("message", response.get("error", result.stdout))
        print(f"  ERROR: {error}")
        sys.exit(1)

    return response


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def get_workspace(account_id: str, workspace_id: str, token: str) -> dict:
    """Get workspace details."""
    url = f"{BASE_URL}/{account_id}/workspaces/{workspace_id}"
    return api_request("GET", url, token)


def update_workspace_ncc(account_id: str, workspace_id: str, ncc_id: str, token: str) -> dict:
    """Attach a different NCC to the workspace."""
    url = f"{BASE_URL}/{account_id}/workspaces/{workspace_id}"
    return api_request("PATCH", url, token, {"network_connectivity_config_id": ncc_id})


# ---------------------------------------------------------------------------
# NCC helpers
# ---------------------------------------------------------------------------

def get_ncc(account_id: str, ncc_id: str, token: str) -> dict:
    """Get NCC details."""
    url = f"{BASE_URL}/{account_id}/network-connectivity-configs/{ncc_id}"
    return api_request("GET", url, token)


def list_nccs(account_id: str, token: str) -> list[dict]:
    """List all NCCs in the account."""
    url = f"{BASE_URL}/{account_id}/network-connectivity-configs"
    response = api_request("GET", url, token)
    return response.get("items", [])


def create_ncc(account_id: str, name: str, region: str, token: str) -> dict:
    """Create a new (empty) NCC."""
    url = f"{BASE_URL}/{account_id}/network-connectivity-configs"
    return api_request("POST", url, token, {"name": name, "region": region})


PLACEHOLDER_NAME = "neo4j-ncc-placeholder"


def find_or_create_placeholder_ncc(account_id: str, region: str, token: str) -> str:
    """Find an existing placeholder NCC in the region, or create one.

    Returns the NCC ID of the placeholder.
    """
    nccs = list_nccs(account_id, token)
    for ncc in nccs:
        if ncc.get("name") == PLACEHOLDER_NAME and ncc.get("region") == region:
            ncc_id = ncc.get("network_connectivity_config_id", "")
            if ncc_id:
                print(f"  Reusing existing placeholder NCC: {ncc_id}")
                return ncc_id

    print(f"  Creating placeholder NCC in {region}...")
    placeholder = create_ncc(account_id, PLACEHOLDER_NAME, region, token)
    placeholder_id = placeholder.get("network_connectivity_config_id", "")
    print(f"  Created placeholder NCC: {placeholder_id}")
    return placeholder_id


def delete_ncc(account_id: str, ncc_id: str, token: str) -> dict:
    """Delete an NCC."""
    url = f"{BASE_URL}/{account_id}/network-connectivity-configs/{ncc_id}"
    return api_request("DELETE", url, token)


# ---------------------------------------------------------------------------
# Private endpoint rule helpers
# ---------------------------------------------------------------------------

def list_rules(account_id: str, ncc_id: str, token: str) -> list[dict]:
    """List all private endpoint rules on the NCC."""
    url = f"{BASE_URL}/{account_id}/network-connectivity-configs/{ncc_id}/private-endpoint-rules"
    response = api_request("GET", url, token)
    return response.get("items", [])


def delete_rule(account_id: str, ncc_id: str, rule_id: str, token: str) -> dict:
    """Delete a private endpoint rule from the NCC."""
    url = (
        f"{BASE_URL}/{account_id}/network-connectivity-configs/"
        f"{ncc_id}/private-endpoint-rules/{rule_id}"
    )
    return api_request("DELETE", url, token)


def print_rule(i: int, rule: dict):
    """Print a single rule in a readable format."""
    rule_id = rule.get("rule_id", "unknown")
    resource_id = rule.get("resource_id", "unknown")
    group_id = rule.get("group_id", "")
    status = rule.get("connection_state", "unknown")
    deactivated = rule.get("deactivated", False)
    domains = rule.get("domain_names", [])

    label = f"  [{i}] {rule_id}"
    if deactivated:
        label += " (deactivated)"
    print(label)
    print(f"      Resource:   {resource_id}")
    if group_id:
        print(f"      Group:      {group_id}")
    if domains:
        print(f"      Domains:    {', '.join(domains)}")
    print(f"      Status:     {status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    profile = parse_args()

    load_env()
    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Set NCC_ID in .env")

    print("=" * 60)
    print("DETACH NCC AND REMOVE PRIVATE ENDPOINT RULES")
    print("=" * 60)
    print()
    print(f"  Account ID: {account_id}")
    print(f"  NCC ID:     {ncc_id}")
    print()

    # Get workspace ID
    workspace_id = optional_env("DATABRICKS_WORKSPACE_ID")
    if not workspace_id:
        workspace_id = input("Databricks workspace ID: ").strip()
    if not workspace_id:
        print("ERROR: Workspace ID is required.")
        sys.exit(1)

    # Get token
    if profile:
        print(f"  Using Databricks CLI profile: {profile}")
        token = get_token_from_profile(profile)
    else:
        token = optional_env("DATABRICKS_ACCOUNT_TOKEN")
        if not token:
            import getpass
            token = getpass.getpass("Databricks account admin token: ")
    if not token:
        print("ERROR: Token is required.")
        sys.exit(1)

    # ---------------------------------------------------------------
    # Step 1: Get workspace and NCC details
    # ---------------------------------------------------------------
    print("Step 1: Get workspace and NCC details")
    workspace = get_workspace(account_id, workspace_id, token)
    workspace_name = workspace.get("workspace_name", workspace_id)
    workspace_region = workspace.get("location", workspace.get("azure_workspace_info", {}).get("region", ""))
    current_ncc = workspace.get("network_connectivity_config_id", "")
    print(f"  Workspace:  {workspace_name}")
    print(f"  Region:     {workspace_region}")
    print(f"  Current NCC: {current_ncc}")

    if current_ncc and current_ncc != ncc_id:
        print(f"\n  WARNING: Workspace is attached to NCC {current_ncc},")
        print(f"  not the NCC in .env ({ncc_id}).")
        choice = input("  Continue anyway? [y/N]: ").strip().lower()
        if choice != "y":
            print("Cancelled.")
            sys.exit(0)

    ncc = get_ncc(account_id, ncc_id, token)
    ncc_name = ncc.get("name", ncc_id)
    ncc_region = ncc.get("region", "")
    print(f"  NCC name:   {ncc_name}")
    print(f"  NCC region: {ncc_region}")

    # ---------------------------------------------------------------
    # Step 2: Detach NCC from workspace
    # ---------------------------------------------------------------
    print(f"\nStep 2: Detach NCC from workspace")

    if current_ncc == ncc_id:
        # The Databricks API does not support unsetting the NCC on a
        # workspace. The workaround is to swap in an empty placeholder
        # NCC. We reuse an existing one if available.
        region = ncc_region or workspace_region
        if not region:
            region = input("  Azure region for placeholder NCC: ").strip()
            if not region:
                print("ERROR: Region is required.")
                sys.exit(1)

        placeholder_id = find_or_create_placeholder_ncc(account_id, region, token)

        print(f"  Swapping workspace to placeholder NCC...")
        update_workspace_ncc(account_id, workspace_id, placeholder_id, token)
        print(f"  Workspace now uses placeholder NCC.")
    elif not current_ncc:
        print("  Workspace has no NCC attached. Skipping detach.")
    else:
        print(f"  Workspace uses a different NCC ({current_ncc}). Skipping detach.")

    # ---------------------------------------------------------------
    # Step 3: Delete private endpoint rules
    # ---------------------------------------------------------------
    print(f"\nStep 3: Delete private endpoint rules from NCC")

    rules = list_rules(account_id, ncc_id, token)
    had_established = False

    if not rules:
        print("  No private endpoint rules found.")
    else:
        print(f"  Found {len(rules)} rule(s):\n")
        for i, rule in enumerate(rules, 1):
            print_rule(i, rule)
            print()

        for rule in rules:
            rule_id = rule.get("rule_id", "")
            status = rule.get("connection_state", "")
            if status in ("ESTABLISHED", "REJECTED", "DISCONNECTED"):
                had_established = True
            print(f"  Deleting {rule_id}...")
            delete_rule(account_id, ncc_id, rule_id, token)
            print(f"  Deleted.")

    # ---------------------------------------------------------------
    # Step 4: Delete the NCC
    # ---------------------------------------------------------------
    print(f"\nStep 4: Delete NCC ({ncc_name})")
    delete_ncc(account_id, ncc_id, token)
    print("  NCC deleted.")

    # ---------------------------------------------------------------
    # Done
    # ---------------------------------------------------------------
    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  NCC {ncc_name} has been removed from workspace {workspace_name}")
    print(f"  and deleted along with its private endpoint rules.")
    print()

    if had_established:
        print("  Note: Rules that were in ESTABLISHED, REJECTED, or DISCONNECTED")
        print("  state may be retained by Databricks for up to 7 days before")
        print("  the private endpoint is permanently removed from your Azure")
        print("  resource.")
        print()

    print("  A placeholder NCC (neo4j-ncc-placeholder) was attached to the")
    print("  workspace. You can leave it in place or remove it from the")
    print("  account console if no longer needed.")
    print()


if __name__ == "__main__":
    main()
