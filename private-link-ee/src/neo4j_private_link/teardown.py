"""
Teardown Private Link for Neo4j Enterprise Edition.

Removes all Private Link infrastructure created by setup-private-link:
Private Link Service, internal load balancer, and NAT subnet. Leaves the
marketplace deployment intact.

Usage:
    uv run teardown-private-link
"""

from neo4j_private_link.helpers import (
    az,
    discover_vmss,
    load_env,
    optional_env,
    require_env,
)

# Resource names (hardcoded, matching private-link.bicep)
PLS_NAME = "neo4j-pls"
LB_NAME = "neo4j-internal-lb"
NAT_SUBNET_NAME = "pls-nat-subnet"


def remove_vmss_from_backend_pool(info: dict):
    """Remove the internal LB's backend pool from the VMSS."""
    rg = info["resource_group"]
    vmss_name = info["vmss_name"]

    pools = az(["vmss", "show", "--resource-group", rg, "--name", vmss_name,
                "--query",
                "virtualMachineProfile.networkProfile"
                ".networkInterfaceConfigurations[0]"
                ".ipConfigurations[0]"
                ".loadBalancerBackendAddressPools"],
               check=False)

    if not pools:
        print("  No backend pools found on VMSS.")
        return

    # Find the backend pool belonging to neo4j-internal-lb
    target_index = None
    for i, pool in enumerate(pools):
        pool_id = pool.get("id", "")
        if LB_NAME in pool_id:
            target_index = i
            break

    if target_index is None:
        print(f"  VMSS is not in {LB_NAME} backend pool. Nothing to remove.")
        return

    print(f"  Removing backend pool (index {target_index}) from VMSS {vmss_name}...")
    az(["vmss", "update",
        "--resource-group", rg,
        "--name", vmss_name,
        "--remove",
        "virtualMachineProfile.networkProfile"
        ".networkInterfaceConfigurations[0]"
        ".ipConfigurations[0]"
        ".loadBalancerBackendAddressPools",
        str(target_index)])

    print("  Updating VMSS instances...")
    az(["vmss", "update-instances",
        "--resource-group", rg,
        "--name", vmss_name,
        "--instance-ids", "*"])


def main():
    load_env()
    resource_group = require_env("RESOURCE_GROUP", "Set RESOURCE_GROUP in .env")
    vmss_name = optional_env("VMSS_NAME") or None

    print("=" * 60)
    print("TEARDOWN: Private Link for Neo4j EE")
    print("=" * 60)

    # Step 1: Discover VMSS
    print("\nStep 1: Discover VMSS")
    info = discover_vmss(resource_group, vmss_name)
    print(f"  VMSS: {info['vmss_name']}")
    print(f"  VNet: {info['vnet_name']}")

    # Step 2: Remove VMSS from internal LB backend pool
    print(f"\nStep 2: Remove VMSS from {LB_NAME} backend pool")
    remove_vmss_from_backend_pool(info)

    # Step 3: Delete Private Link Service
    print(f"\nStep 3: Delete Private Link Service ({PLS_NAME})")
    az(["network", "private-link-service", "delete",
        "--resource-group", resource_group,
        "--name", PLS_NAME], check=False)

    # Step 4: Delete Internal Load Balancer
    print(f"\nStep 4: Delete Internal Load Balancer ({LB_NAME})")
    az(["network", "lb", "delete",
        "--resource-group", resource_group,
        "--name", LB_NAME], check=False)

    # Step 5: Delete NAT Subnet
    print(f"\nStep 5: Delete NAT Subnet ({NAT_SUBNET_NAME})")
    az(["network", "vnet", "subnet", "delete",
        "--resource-group", resource_group,
        "--vnet-name", info["vnet_name"],
        "--name", NAT_SUBNET_NAME], check=False)

    print()
    print("=" * 60)
    print("TEARDOWN COMPLETE")
    print("=" * 60)
    print()
    print("All Private Link resources have been removed.")
    print("The Neo4j marketplace deployment is unchanged.")
    print()
    print("Note: The NCC private endpoint rule in Databricks was not")
    print("removed. Clean it up in the Databricks account console if")
    print("no longer needed.")
    print()


if __name__ == "__main__":
    main()
