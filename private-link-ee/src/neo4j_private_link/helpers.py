"""
Shared helpers for Neo4j Private Link setup and teardown.

Provides az CLI wrapper, .env loading, and marketplace resource discovery.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_DIR / ".env"
ENV_SAMPLE_PATH = PROJECT_DIR / ".env.sample"
BICEP_TEMPLATE = PROJECT_DIR / "private-link.bicep"


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def write_env(values: dict[str, str]):
    """Write values to .env, preserving comments and not overriding existing values."""
    lines: list[str] = []

    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, val = stripped.partition("=")
                    key = key.strip()
                    val = val.strip()
                    # Only update if currently empty and we have a new value
                    if not val and key in values and values[key]:
                        lines.append(f"{key}={values[key]}\n")
                    else:
                        lines.append(line)
                else:
                    lines.append(line)
    elif ENV_SAMPLE_PATH.exists():
        with open(ENV_SAMPLE_PATH) as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _, val = stripped.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key in values and values[key]:
                        lines.append(f"{key}={values[key]}\n")
                    else:
                        lines.append(line)
                else:
                    lines.append(line)
    else:
        for key, val in values.items():
            lines.append(f"{key}={val}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)
    print(f"  .env written to {ENV_PATH}")


def load_env():
    """Load .env file from the project directory."""
    if not ENV_PATH.exists():
        print(f"ERROR: .env not found at {ENV_PATH}")
        print("  Run with --init to create it interactively:")
        print("  uv run setup-private-link --init")
        sys.exit(1)

    print(f"  Loading config from {ENV_PATH}")
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Only set if not already in environment (env vars take precedence)
            if key not in os.environ or not os.environ[key]:
                os.environ[key] = value


def require_env(key: str, description: str = "") -> str:
    """Get a required environment variable or exit with an error."""
    value = os.environ.get(key, "").strip()
    if not value:
        msg = f"ERROR: {key} is not set."
        if description:
            msg += f" {description}"
        print(msg)
        sys.exit(1)
    return value


def optional_env(key: str, default: str = "") -> str:
    """Get an optional environment variable with a default."""
    return os.environ.get(key, "").strip() or default


# ---------------------------------------------------------------------------
# Azure CLI wrapper
# ---------------------------------------------------------------------------

def az(args: list[str], check: bool = True) -> dict | list | str:
    """Run an az CLI command and return parsed JSON output."""
    cmd = ["az"] + args + ["-o", "json"]
    print(f"  $ az {' '.join(args)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        if check:
            print(f"    ERROR: {result.stderr.strip()}")
            sys.exit(1)
        return {}
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_vmss(resource_group: str, vmss_name: str | None = None) -> dict:
    """Find the Neo4j VMSS and extract network details.

    Returns dict with keys: vmss_name, location, vnet_name, subnet_name,
    subnet_id, subscription_id, resource_group.
    """
    if vmss_name:
        vmss_data = az(["vmss", "show", "--resource-group", resource_group,
                        "--name", vmss_name,
                        "--query", "{name:name, location:location}"])
        vmss_name = vmss_data["name"]
        location = vmss_data["location"]
    else:
        vmss_list = az(["vmss", "list", "--resource-group", resource_group,
                        "--query", "[?starts_with(name, 'vmss-neo4j-')]"])
        if not vmss_list:
            print("ERROR: No Neo4j VMSS found in resource group.")
            sys.exit(1)
        if len(vmss_list) > 1:
            print("WARNING: Multiple Neo4j VMSS found. Using the first one.")
            print("  Set VMSS_NAME in .env to select a specific one.")
        vmss_name = vmss_list[0]["name"]
        location = vmss_list[0]["location"]

    ip_config = az(["vmss", "show", "--resource-group", resource_group,
                    "--name", vmss_name, "--query",
                    "virtualMachineProfile.networkProfile"
                    ".networkInterfaceConfigurations[0]"
                    ".ipConfigurations[0]"])

    subnet_id = ip_config["subnet"]["id"]
    parts = subnet_id.split("/")
    vnet_name = parts[parts.index("virtualNetworks") + 1]
    subnet_name = parts[parts.index("subnets") + 1]
    subscription_id = parts[parts.index("subscriptions") + 1]

    return {
        "vmss_name": vmss_name,
        "location": location,
        "vnet_name": vnet_name,
        "subnet_name": subnet_name,
        "subnet_id": subnet_id,
        "subscription_id": subscription_id,
        "resource_group": resource_group,
    }


def discover_pls_connections(resource_group: str, pls_name: str = "neo4j-pls") -> list[dict]:
    """Find private endpoint connections on the Private Link Service.

    Returns a list of dicts with keys: name, status, description.
    """
    connections = az(["network", "private-link-service", "show",
                      "--resource-group", resource_group,
                      "--name", pls_name,
                      "--query", "privateEndpointConnections"], check=False)
    if not connections:
        return []

    results = []
    for conn in connections:
        state = conn.get("privateLinkServiceConnectionState", {})
        results.append({
            "name": conn.get("name", ""),
            "status": state.get("status", ""),
            "description": state.get("description", ""),
        })
    return results


def approve_pls_connection(resource_group: str, pls_name: str, connection_name: str,
                           description: str = "Approved") -> dict:
    """Approve a pending private endpoint connection on the Private Link Service."""
    return az(["network", "private-link-service", "connection", "update",
               "--resource-group", resource_group,
               "--service-name", pls_name,
               "--name", connection_name,
               "--connection-status", "Approved",
               "--description", description])


def discover_neo4j_uri(resource_group: str) -> str | None:
    """Find the Neo4j public LB FQDN and construct the Bolt URI."""
    public_ips = az(["network", "public-ip", "list",
                     "--resource-group", resource_group,
                     "--query", "[?starts_with(name, 'ip-neo4j-')]"])
    if not public_ips:
        return None

    ip = public_ips[0]
    fqdn = ip.get("dnsSettings", {}).get("fqdn")
    if fqdn:
        return f"neo4j://{fqdn}:7687"

    ip_address = ip.get("ipAddress")
    if ip_address:
        return f"neo4j://{ip_address}:7687"

    return None
