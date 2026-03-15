# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Verify Private Link infrastructure for Neo4j Enterprise Edition.

Checks that all resources exist and are correctly configured, or confirms
they have been fully cleaned up after teardown.

Usage:
    uv run verify-private-link.py           # Verify resources exist
    uv run verify-private-link.py --cleanup  # Verify resources were removed

Configuration comes from .env — only RESOURCE_GROUP is required.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from private_link_helpers import az, discover_pls_connections, discover_vmss, load_env, require_env


def check(label: str, passed: bool, detail: str = ""):
    """Print a check result."""
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {label}")
    if detail:
        print(f"         {detail}")
    return passed


def verify_resources(resource_group: str, info: dict):
    """Verify all Private Link resources exist and are configured."""
    print("=" * 60)
    print("VERIFY PRIVATE LINK RESOURCES")
    print("=" * 60)
    results = []

    # Private Link Service
    print("\nPrivate Link Service:")
    pls = az(["network", "private-link-service", "show",
              "--resource-group", resource_group, "--name", "neo4j-pls",
              "--query", "{name:name, provisioningState:provisioningState}"],
             check=False)
    if pls:
        results.append(check("neo4j-pls exists",
                             pls.get("provisioningState") == "Succeeded",
                             f"State: {pls.get('provisioningState', 'unknown')}"))
    else:
        results.append(check("neo4j-pls exists", False, "Not found"))

    # PLS connections
    connections = discover_pls_connections(resource_group)
    if connections:
        for conn in connections:
            results.append(check(
                f"Connection: {conn['status']}",
                conn["status"] == "Approved",
                conn["name"][:60],
            ))
    else:
        print("  [INFO] No private endpoint connections yet")

    # Internal Load Balancer
    print("\nInternal Load Balancer:")
    lb = az(["network", "lb", "show",
             "--resource-group", resource_group, "--name", "neo4j-internal-lb",
             "--query", "{name:name, sku:sku.name, provisioningState:provisioningState}"],
            check=False)
    if lb:
        results.append(check("neo4j-internal-lb exists",
                             lb.get("provisioningState") == "Succeeded",
                             f"SKU: {lb.get('sku', 'unknown')}, State: {lb.get('provisioningState', 'unknown')}"))
    else:
        results.append(check("neo4j-internal-lb exists", False, "Not found"))

    # Health probe
    probe = az(["network", "lb", "probe", "show",
                "--resource-group", resource_group, "--lb-name", "neo4j-internal-lb",
                "--name", "bolt-probe",
                "--query", "{port:port, protocol:protocol, intervalInSeconds:intervalInSeconds}"],
               check=False)
    if probe:
        results.append(check("Health probe on port 7687",
                             probe.get("port") == 7687,
                             f"Port: {probe.get('port')}, Protocol: {probe.get('protocol')}, Interval: {probe.get('intervalInSeconds')}s"))
    else:
        results.append(check("Health probe", False, "Not found"))

    # NAT Subnet
    print("\nNAT Subnet:")
    subnet = az(["network", "vnet", "subnet", "show",
                 "--resource-group", resource_group,
                 "--vnet-name", info["vnet_name"], "--name", "pls-nat-subnet",
                 "--query", "{name:name, addressPrefix:addressPrefix, privateLinkServiceNetworkPolicies:privateLinkServiceNetworkPolicies}"],
                check=False)
    if subnet:
        pls_disabled = subnet.get("privateLinkServiceNetworkPolicies") == "Disabled"
        results.append(check("pls-nat-subnet exists", True,
                             f"Prefix: {subnet.get('addressPrefix')}"))
        results.append(check("privateLinkServiceNetworkPolicies disabled", pls_disabled,
                             f"Value: {subnet.get('privateLinkServiceNetworkPolicies')}"))
    else:
        results.append(check("pls-nat-subnet exists", False, "Not found"))

    # VMSS backend pools
    print("\nVMSS Backend Pools:")
    pools = az(["vmss", "show",
                "--resource-group", resource_group, "--name", info["vmss_name"],
                "--query",
                "virtualMachineProfile.networkProfile"
                ".networkInterfaceConfigurations[0]"
                ".ipConfigurations[0]"
                ".loadBalancerBackendAddressPools[].id"],
               check=False)
    if pools:
        pool_count = len(pools)
        has_internal = any("neo4j-internal-lb" in p for p in pools)
        results.append(check(f"VMSS in {pool_count} backend pool(s)",
                             pool_count >= 2,
                             f"Expected 2 (public + internal)"))
        results.append(check("VMSS in internal LB pool", has_internal))
    else:
        results.append(check("VMSS backend pools", False, "Could not query"))

    # Summary
    passed = sum(results)
    total = len(results)
    print()
    print("=" * 60)
    if passed == total:
        print(f"ALL CHECKS PASSED ({passed}/{total})")
    else:
        print(f"CHECKS: {passed}/{total} passed, {total - passed} failed")
    print("=" * 60)


def verify_cleanup(resource_group: str, info: dict):
    """Verify all Private Link resources have been removed."""
    print("=" * 60)
    print("VERIFY CLEANUP")
    print("=" * 60)
    results = []

    # Private Link Service should be gone
    print("\nPrivate Link Service:")
    pls = az(["network", "private-link-service", "show",
              "--resource-group", resource_group, "--name", "neo4j-pls"],
             check=False)
    results.append(check("neo4j-pls removed", not pls))

    # Internal LB should be gone
    print("\nInternal Load Balancer:")
    lb = az(["network", "lb", "show",
             "--resource-group", resource_group, "--name", "neo4j-internal-lb"],
            check=False)
    results.append(check("neo4j-internal-lb removed", not lb))

    # NAT subnet should be gone
    print("\nNAT Subnet:")
    subnet = az(["network", "vnet", "subnet", "show",
                 "--resource-group", resource_group,
                 "--vnet-name", info["vnet_name"], "--name", "pls-nat-subnet"],
                check=False)
    results.append(check("pls-nat-subnet removed", not subnet))

    # VMSS should have only 1 backend pool
    print("\nVMSS Backend Pools:")
    pools = az(["vmss", "show",
                "--resource-group", resource_group, "--name", info["vmss_name"],
                "--query",
                "virtualMachineProfile.networkProfile"
                ".networkInterfaceConfigurations[0]"
                ".ipConfigurations[0]"
                ".loadBalancerBackendAddressPools[].id"],
               check=False)
    if pools:
        pool_count = len(pools)
        has_internal = any("neo4j-internal-lb" in p for p in pools)
        results.append(check(f"VMSS in {pool_count} backend pool(s)",
                             pool_count == 1,
                             f"Expected 1 (public LB only)"))
        results.append(check("VMSS not in internal LB pool", not has_internal))
    else:
        results.append(check("VMSS backend pools", False, "Could not query"))

    # Summary
    passed = sum(results)
    total = len(results)
    print()
    print("=" * 60)
    if passed == total:
        print(f"CLEANUP VERIFIED ({passed}/{total})")
    else:
        print(f"CHECKS: {passed}/{total} passed, {total - passed} still present")
    print("=" * 60)


def main():
    cleanup_mode = "--cleanup" in sys.argv

    load_env()
    resource_group = require_env("RESOURCE_GROUP", "Set RESOURCE_GROUP in .env")
    info = discover_vmss(resource_group)

    if cleanup_mode:
        verify_cleanup(resource_group, info)
    else:
        verify_resources(resource_group, info)


if __name__ == "__main__":
    main()
