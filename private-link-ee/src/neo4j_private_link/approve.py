"""
Approve pending Private Link connections for Neo4j Enterprise Edition.

Finds pending private endpoint connections on the neo4j-pls Private Link
Service and approves them. This is the step after creating the NCC private
endpoint rule in Databricks — Databricks creates a private endpoint in its
managed subscription, which appears as a pending connection on your PLS.

Usage:
    uv run approve-private-link
"""

from neo4j_private_link.helpers import (
    approve_pls_connection,
    discover_pls_connections,
    load_env,
    require_env,
)


def main():
    load_env()
    resource_group = require_env("RESOURCE_GROUP", "Set RESOURCE_GROUP in .env")
    pls_name = "neo4j-pls"

    print("=" * 60)
    print("APPROVE PRIVATE LINK CONNECTIONS")
    print("=" * 60)

    # Find all connections
    print(f"\nChecking {pls_name} for private endpoint connections...")
    connections = discover_pls_connections(resource_group, pls_name)

    if not connections:
        print("\n  No private endpoint connections found.")
        print("  Create an NCC private endpoint rule in Databricks first.")
        return

    # Show current state
    pending = []
    for conn in connections:
        status = conn["status"]
        print(f"\n  {conn['name']}")
        print(f"    Status:      {status}")
        print(f"    Description: {conn['description']}")
        if status == "Pending":
            pending.append(conn)

    if not pending:
        print("\n  No pending connections to approve.")
        approved = [c for c in connections if c["status"] == "Approved"]
        if approved:
            print(f"  {len(approved)} connection(s) already approved.")
        return

    # Approve pending connections
    print(f"\nApproving {len(pending)} pending connection(s)...")
    for conn in pending:
        print(f"\n  Approving: {conn['name']}")
        approve_pls_connection(
            resource_group, pls_name, conn["name"],
            description="Approved for Databricks serverless",
        )
        print("    Approved.")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print()
    print("Wait for the NCC status in Databricks to show ESTABLISHED")
    print("(up to 10 minutes), then test from a serverless notebook.")


if __name__ == "__main__":
    main()
