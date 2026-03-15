# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Attach an NCC to a Databricks workspace.

Prompts for the workspace ID and attaches the NCC specified in .env
(DATABRICKS_ACCOUNT_ID, NCC_ID) to that workspace via the Databricks
Account API.

Usage:
    uv run attach-ncc.py --profile <databricks-cli-profile>
    uv run attach-ncc.py

Authentication:
    --profile <name>    Use a Databricks CLI profile from ~/.databrickscfg
                        to get a token via `databricks auth token`
    DATABRICKS_ACCOUNT_TOKEN in .env or environment
    Otherwise, prompts for a token interactively

Configuration comes from .env — requires DATABRICKS_ACCOUNT_ID and NCC_ID.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from private_link_helpers import load_env, optional_env, require_env


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


def main():
    profile = parse_args()

    load_env()
    account_id = require_env("DATABRICKS_ACCOUNT_ID", "Set DATABRICKS_ACCOUNT_ID in .env")
    ncc_id = require_env("NCC_ID", "Set NCC_ID in .env")

    print("=" * 60)
    print("ATTACH NCC TO DATABRICKS WORKSPACE")
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

    print(f"\nAttaching NCC {ncc_id} to workspace {workspace_id}...")

    url = (
        f"https://accounts.azuredatabricks.net/api/2.0/accounts/"
        f"{account_id}/workspaces/{workspace_id}"
    )

    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--location",
         "--request", "PATCH", url,
         "--header", "Content-Type: application/json",
         "--header", f"Authorization: Bearer {token}",
         "--data", json.dumps({"network_connectivity_config_id": ncc_id})],
        capture_output=True, text=True, check=False,
    )

    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        sys.exit(1)

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  ERROR: Unexpected response: {result.stdout}")
        sys.exit(1)

    if "error_code" in response or "error" in response:
        error = response.get("message", response.get("error", result.stdout))
        print(f"  ERROR: {error}")
        sys.exit(1)

    workspace_name = response.get("workspace_name", workspace_id)
    ncc_name = response.get("network_connectivity_config_id", ncc_id)

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print(f"  Workspace: {workspace_name}")
    print(f"  NCC:       {ncc_name}")
    print()
    print("It may take a few minutes for serverless compute to pick up")
    print("the NCC. If the first notebook test fails, wait and retry.")


if __name__ == "__main__":
    main()
