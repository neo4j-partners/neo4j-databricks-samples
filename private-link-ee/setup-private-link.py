# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Setup Private Link for Neo4j Enterprise Edition.

Discovers marketplace resources, deploys the Bicep template (internal LB,
NAT subnet, Private Link Service), and adds the VMSS to the internal LB's
backend pool.

Usage:
    # First time — interactive setup, discovers resources, writes .env:
    uv run setup-private-link.py --init

    # Deploy (reads from .env):
    uv run setup-private-link.py

Configuration comes from .env — only RESOURCE_GROUP is required.
Everything else is discovered automatically from the marketplace deployment.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from private_link_helpers import (
    BICEP_TEMPLATE,
    ENV_PATH,
    az,
    discover_neo4j_uri,
    discover_vmss,
    load_env,
    optional_env,
    require_env,
    write_env,
)


# ---------------------------------------------------------------------------
# Interactive init
# ---------------------------------------------------------------------------

def init_env():
    """Interactive setup: ask for RG and password, discover everything else, write .env."""
    print("=" * 60)
    print("Neo4j Private Link — Environment Setup")
    print("=" * 60)
    print()

    # Ask for resource group
    default_rg = ""
    if ENV_PATH.exists():
        # Try to read existing value
        with open(ENV_PATH) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("RESOURCE_GROUP="):
                    default_rg = stripped.partition("=")[2].strip()
                    break

    prompt = "Azure resource group"
    if default_rg:
        prompt += f" [{default_rg}]"
    prompt += ": "
    resource_group = input(prompt).strip() or default_rg
    if not resource_group:
        print("ERROR: Resource group is required.")
        sys.exit(1)

    # Discover VMSS and network details
    print(f"\nDiscovering resources in {resource_group}...")
    info = discover_vmss(resource_group)
    print(f"  VMSS:         {info['vmss_name']}")
    print(f"  VNet:         {info['vnet_name']}")
    print(f"  Subnet:       {info['subnet_name']}")
    print(f"  Region:       {info['location']}")
    print(f"  Subscription: {info['subscription_id']}")

    # Discover Neo4j URI
    print("\nDiscovering Neo4j URI...")
    neo4j_uri = discover_neo4j_uri(resource_group)
    if neo4j_uri:
        print(f"  Found: {neo4j_uri}")
    else:
        print("  Could not discover automatically.")
        neo4j_uri = input("  Enter Neo4j Bolt URI (e.g. neo4j://host:7687): ").strip()
        if not neo4j_uri:
            print("ERROR: Neo4j URI is required.")
            sys.exit(1)

    # Ask for password
    print()
    neo4j_password = getpass.getpass("Neo4j password: ")
    if not neo4j_password:
        print("ERROR: Password is required.")
        sys.exit(1)

    # Write .env
    values = {
        "RESOURCE_GROUP": resource_group,
        "VMSS_NAME": info["vmss_name"],
        "VNET_NAME": info["vnet_name"],
        "NEO4J_SUBNET_NAME": info["subnet_name"],
        "NEO4J_URI": neo4j_uri,
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": neo4j_password,
    }
    write_env(values)
    print()
    print("Next steps:")
    print("  Deploy:   uv run setup-private-link.py")
    print("  Teardown: uv run teardown-private-link.py")


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def deploy_bicep(info: dict, pls_subnet_prefix: str) -> dict:
    """Deploy the private-link.bicep template and return outputs."""
    rg = info["resource_group"]

    result = az(["deployment", "group", "create",
                 "--resource-group", rg,
                 "--name", "neo4j-private-link",
                 "--template-file", str(BICEP_TEMPLATE),
                 "--parameters",
                 f"vnetName={info['vnet_name']}",
                 f"neo4jSubnetName={info['subnet_name']}",
                 f"plsSubnetPrefix={pls_subnet_prefix}",
                 f"location={info['location']}"])

    outputs = result.get("properties", {}).get("outputs", {})
    return {
        "pls_id": outputs.get("privateLinkServiceId", {}).get("value", ""),
        "pls_name": outputs.get("privateLinkServiceName", {}).get("value", ""),
        "lb_name": outputs.get("internalLbName", {}).get("value", ""),
        "backend_pool_id": outputs.get("backendPoolId", {}).get("value", ""),
    }


def add_vmss_to_backend_pool(info: dict, backend_pool_id: str):
    """Add the VMSS to the internal LB's backend pool (idempotent)."""
    rg = info["resource_group"]
    vmss_name = info["vmss_name"]

    # Check if already in the backend pool (makes re-runs safe)
    pools = az(["vmss", "show", "--resource-group", rg, "--name", vmss_name,
                "--query",
                "virtualMachineProfile.networkProfile"
                ".networkInterfaceConfigurations[0]"
                ".ipConfigurations[0]"
                ".loadBalancerBackendAddressPools"],
               check=False)

    if pools:
        for pool in pools:
            if pool.get("id", "").lower() == backend_pool_id.lower():
                print(f"  VMSS {vmss_name} already in internal LB backend pool. Skipping.")
                return

    print(f"\nAdding VMSS {vmss_name} to internal LB backend pool...")
    az(["vmss", "update",
        "--resource-group", rg,
        "--name", vmss_name,
        "--add",
        "virtualMachineProfile.networkProfile"
        ".networkInterfaceConfigurations[0]"
        ".ipConfigurations[0]"
        ".loadBalancerBackendAddressPools",
        f"id={backend_pool_id}"])

    print("Updating VMSS instances...")
    az(["vmss", "update-instances",
        "--resource-group", rg,
        "--name", vmss_name,
        "--instance-ids", "*"])


def print_output(info: dict, deployment: dict):
    """Print structured summary with next steps and save to file."""
    pls_id = deployment["pls_id"]
    account_id = optional_env("DATABRICKS_ACCOUNT_ID", "<ACCOUNT_ID>")
    ncc_id = optional_env("NCC_ID", "<NCC_ID>")
    domain = optional_env("NEO4J_DOMAIN", "neo4j-ee.private.neo4j.com")

    curl_cmd = (
        f"curl --location "
        f"'https://accounts.azuredatabricks.net/api/2.0/accounts/"
        f"{account_id}/network-connectivity-configs/"
        f"{ncc_id}/private-endpoint-rules' \\\n"
        f"  --header 'Content-Type: application/json' \\\n"
        f"  --header 'Authorization: Bearer <TOKEN>' \\\n"
        f"  --data '{{\n"
        f'    "domain_names": ["{domain}"],\n'
        f'    "resource_id": "{pls_id}",\n'
        f'    "group_id": "neo4j-pls"\n'
        f"  }}'"
    )

    notebook = (
        "%pip install neo4j\n"
        "\n"
        "from neo4j import GraphDatabase\n"
        "\n"
        f'uri = "neo4j://{domain}:7687"\n'
        'auth = ("neo4j", "<PASSWORD>")\n'
        "\n"
        "with GraphDatabase.driver(uri, auth=auth) as driver:\n"
        "    driver.verify_connectivity()\n"
        "    records, _, _ = driver.execute_query(\n"
        '        "RETURN \'Connected over Private Link\' AS message"\n'
        "    )\n"
        '    print(records[0]["message"])'
    )

    lines = [
        "",
        "=" * 60,
        "PRIVATE LINK SETUP COMPLETE",
        "=" * 60,
        "",
        "Resources created:",
        f"  Private Link Service:   {deployment['pls_name']}",
        f"  Internal Load Balancer: {deployment['lb_name']}",
        f"  NAT Subnet:             pls-nat-subnet",
        f"  VMSS backend pool:      updated",
        "",
        "Private Link Service Resource ID:",
        f"  {pls_id}",
        "",
        "-" * 60,
        "NEXT STEPS",
        "-" * 60,
        "",
        "1. Create an NCC (Network Connectivity Configuration) in Databricks",
        "   In the Databricks account console, go to:",
        "   Security > Network connectivity configurations > Create",
        "   Give it a name and select the Azure region matching your deployment.",
        "",
        "2. Add a private endpoint rule to the NCC",
        "   Open the NCC you just created, go to the",
        "   'Private endpoint rules' tab, and click 'Add private endpoint rule'.",
        "   Paste the Private Link Service Resource ID above into the",
        "   'Azure resource ID' field and click 'Add'.",
        "",
        "   NOTE: The Databricks UI does not expose the domain name field.",
        "   Use the curl command below instead — it includes the domain name",
        f"   ({domain}) which Databricks needs to route traffic through",
        "   the private endpoint. The domain name does not need to exist in",
        "   public DNS; Databricks resolves it internally.",
        "",
        "3. Approve the pending connection",
        "   Run: uv run approve-private-link.py",
        "   This finds and approves pending connections on the PLS.",
        "   Wait for NCC status to show ESTABLISHED (up to 10 min).",
        "",
        "4. Attach the NCC to your Databricks workspace",
        "   Run: uv run attach-ncc.py",
        "   Prompts for workspace ID and attaches the NCC via the API.",
        "",
        "5. Test from a Databricks serverless notebook",
        "   See the sample notebook cell below.",
        f"   The notebook URI must use the same domain ({domain})",
        "   that was specified in the NCC private endpoint rule.",
        "",
        "-" * 60,
        "NCC API CURL COMMAND",
        "-" * 60,
        "",
        curl_cmd,
        "",
        "-" * 60,
        "SAMPLE NOTEBOOK CELL",
        "-" * 60,
        "",
        notebook,
        "",
        "=" * 60,
    ]

    output = "\n".join(lines)
    print(output)

    output_file = Path(__file__).resolve().parent / "setup-output.txt"
    output_file.write_text(output + "\n")
    print(f"\nOutput saved to {output_file}")


def main():
    # Check for --init flag
    if "--init" in sys.argv:
        init_env()
        return

    # Load configuration
    load_env()
    resource_group = require_env("RESOURCE_GROUP", "Set RESOURCE_GROUP in .env")
    vmss_name = optional_env("VMSS_NAME") or None

    # Phase 1: Discovery
    print("=" * 60)
    print("PHASE 1: Discovery")
    print("=" * 60)
    info = discover_vmss(resource_group, vmss_name)
    print(f"  VMSS:         {info['vmss_name']}")
    print(f"  VNet:         {info['vnet_name']}")
    print(f"  Subnet:       {info['subnet_name']}")
    print(f"  Region:       {info['location']}")
    print(f"  Subscription: {info['subscription_id']}")

    pls_subnet_prefix = optional_env("PLS_SUBNET_PREFIX", "10.1.0.0/24")

    # Phase 2: Bicep deployment
    print("\n" + "=" * 60)
    print("PHASE 2: Deploy Private Link Infrastructure")
    print("=" * 60)
    deployment = deploy_bicep(info, pls_subnet_prefix)
    print(f"  PLS:          {deployment['pls_name']}")
    print(f"  LB:           {deployment['lb_name']}")
    print(f"  Backend Pool: {deployment['backend_pool_id']}")

    # Phase 3: VMSS update
    print("\n" + "=" * 60)
    print("PHASE 3: Update VMSS Backend Pool")
    print("=" * 60)
    add_vmss_to_backend_pool(info, deployment["backend_pool_id"])

    # Phase 4: Output
    print_output(info, deployment)


if __name__ == "__main__":
    main()
