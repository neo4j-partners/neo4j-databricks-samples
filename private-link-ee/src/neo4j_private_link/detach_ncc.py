"""
Remove the Neo4j private endpoint rule from a Databricks NCC.

Lists private endpoint rules on the NCC specified in .env
(DATABRICKS_ACCOUNT_ID, NCC_ID), lets you select which to delete,
and removes it via the Databricks Account API.

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


def list_rules(account_id: str, ncc_id: str, token: str) -> list[dict]:
    """List all private endpoint rules on the NCC."""
    url = (
        f"https://accounts.azuredatabricks.net/api/2.0/accounts/"
        f"{account_id}/network-connectivity-configs/{ncc_id}/private-endpoint-rules"
    )
    response = api_request("GET", url, token)
    return response.get("items", [])


def delete_rule(account_id: str, ncc_id: str, rule_id: str, token: str) -> dict:
    """Delete a private endpoint rule from the NCC."""
    url = (
        f"https://accounts.azuredatabricks.net/api/2.0/accounts/"
        f"{account_id}/network-connectivity-configs/{ncc_id}/"
        f"private-endpoint-rules/{rule_id}"
    )
    return api_request("DELETE", url, token)


def print_rule(i: int, rule: dict):
    """Print a single rule in a readable format."""
    rule_id = rule.get("private_endpoint_rule_id", "unknown")
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


def main():
    profile = parse_args()

    load_env()
    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Set NCC_ID in .env")

    print("=" * 60)
    print("REMOVE PRIVATE ENDPOINT RULE FROM NCC")
    print("=" * 60)
    print()
    print(f"  Account ID: {account_id}")
    print(f"  NCC ID:     {ncc_id}")
    print()

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

    # List existing rules
    print("Fetching private endpoint rules...")
    rules = list_rules(account_id, ncc_id, token)

    if not rules:
        print("\n  No private endpoint rules found on this NCC.")
        sys.exit(0)

    print(f"\n  Found {len(rules)} private endpoint rule(s):\n")
    for i, rule in enumerate(rules, 1):
        print_rule(i, rule)
        print()

    # Select rule to delete
    if len(rules) == 1:
        choice = input("Delete this rule? [y/N]: ").strip().lower()
        if choice != "y":
            print("Cancelled.")
            sys.exit(0)
        selected = rules[0]
    else:
        raw = input(f"Enter rule number to delete [1-{len(rules)}], or 'q' to quit: ").strip()
        if raw.lower() == "q" or not raw:
            print("Cancelled.")
            sys.exit(0)
        try:
            idx = int(raw)
            if idx < 1 or idx > len(rules):
                raise ValueError
        except ValueError:
            print("ERROR: Invalid selection.")
            sys.exit(1)
        selected = rules[idx - 1]

    rule_id = selected["private_endpoint_rule_id"]
    status = selected.get("connection_state", "unknown")
    print(f"\nDeleting private endpoint rule {rule_id}...")
    delete_rule(account_id, ncc_id, rule_id, token)

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  Private endpoint rule {rule_id} has been deleted.")
    print()

    if status in ("ESTABLISHED", "REJECTED", "DISCONNECTED"):
        print("  Note: Because the rule was in ESTABLISHED/REJECTED/DISCONNECTED")
        print("  state, Databricks may retain the private endpoint on your")
        print("  Azure resource for up to 7 days before permanently removing it.")
        print()

    print("  The NCC is still attached to your workspace. Detaching an NCC")
    print("  from a workspace is not currently supported by the Databricks")
    print("  API. If you need to remove it, contact Databricks support or")
    print("  manage it in the account console.")
    print()


if __name__ == "__main__":
    main()
