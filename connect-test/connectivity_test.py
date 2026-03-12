#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "neo4j",
#     "python-dotenv",
# ]
# ///
"""Neo4j Connectivity Test Suite

Validates network and driver connectivity to Neo4j.
Reads credentials from .env file in the same directory.
"""

import os
import socket
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the script's directory
env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    print(f"[ERROR] .env file not found at {env_path}")
    print("Copy .env.sample to .env and fill in your Neo4j credentials:")
    print("  cp .env.sample .env")
    sys.exit(1)

load_dotenv(env_path)

# Configuration
NEO4J_HOST = os.getenv("NEO4J_HOST")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
NEO4J_BOLT_URI = f"neo4j+s://{NEO4J_HOST}"

# Validate required variables
missing = [v for v in ("NEO4J_HOST", "NEO4J_USER", "NEO4J_PASSWORD") if not os.getenv(v)]
if missing:
    print(f"[ERROR] Missing required variables in .env: {', '.join(missing)}")
    sys.exit(1)


# ── Section 1: Environment Information ──────────────────────────

def test_environment():
    print("=" * 60)
    print("ENVIRONMENT INFORMATION")
    print("=" * 60)
    print(f"\nPython Version: {sys.version}")

    try:
        import neo4j
        print(f"Neo4j Python Driver: {neo4j.__version__}")
    except ImportError:
        print("Neo4j Python Driver: NOT INSTALLED")


# ── Section 2: Network Connectivity Test (TCP Layer) ────────────

def test_tcp_connectivity():
    print("\n" + "=" * 60)
    print("TEST: Network Connectivity (TCP)")
    print("=" * 60)
    print(f"\nTarget: {NEO4J_HOST}:7687 (Bolt protocol port)")
    print("Testing: Can we reach Neo4j at the network level?")

    try:
        start_time = time.time()
        sock = socket.create_connection((NEO4J_HOST, 7687), timeout=10)
        elapsed = (time.time() - start_time) * 1000
        sock.close()

        print("\n" + "=" * 60)
        print(">>> CONNECTIVITY VERIFIED <<<")
        print("=" * 60)
        print(f"\n[PASS] TCP connection established in {elapsed:.1f}ms")
        print(f"\nConnection Details:")
        print(f"  - Host: {NEO4J_HOST}")
        print(f"  - Port: 7687 (Bolt)")
        print(f"  - TCP Latency: {elapsed:.1f}ms")
        print("\n" + "-" * 60)
        print("RESULT: Network path to Neo4j is OPEN")
        print("        Firewall rules allow Bolt protocol traffic")
        print("-" * 60)
        print("\nStatus: PASS")
        return True

    except Exception as e:
        print(f"\n[FAIL] Cannot reach {NEO4J_HOST}:7687 - {e}")
        print("\nStatus: FAIL")
        return False


# ── Section 3: Neo4j Python Driver Test ─────────────────────────

def test_python_driver():
    print("\n" + "=" * 60)
    print("TEST: Neo4j Python Driver")
    print("=" * 60)
    print(f"\nTarget: {NEO4J_BOLT_URI}")
    print("Testing: Can we authenticate and execute queries via Bolt protocol?")

    from neo4j import GraphDatabase

    try:
        start_time = time.time()
        driver = GraphDatabase.driver(NEO4J_BOLT_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

        # Verify connectivity
        driver.verify_connectivity()
        connect_time = (time.time() - start_time) * 1000

        print("\n" + "=" * 60)
        print(">>> AUTHENTICATION SUCCESSFUL <<<")
        print("=" * 60)
        print(f"\n[PASS] Driver connected and authenticated in {connect_time:.1f}ms")

        # Test simple query
        with driver.session() as session:
            query_start = time.time()
            result = session.run("RETURN 1 AS test")
            record = result.single()
            query_time = (time.time() - query_start) * 1000
            print(f"[PASS] Query executed: RETURN 1 = {record['test']} ({query_time:.1f}ms)")

            # Get Neo4j version
            result = session.run(
                "CALL dbms.components() YIELD name, versions RETURN name, versions"
            )
            neo4j_info = [f"{r['name']} {r['versions']}" for r in result]

        total_time = (time.time() - start_time) * 1000
        driver.close()

        print(f"\nConnection Details:")
        print(f"  - URI: {NEO4J_BOLT_URI}")
        print(f"  - User: {NEO4J_USER}")
        print(f"  - Database: {NEO4J_DATABASE}")
        print(f"  - Neo4j Server: {', '.join(neo4j_info)}")
        print(f"  - Connection Time: {connect_time:.1f}ms")
        print(f"  - Total Test Time: {total_time:.1f}ms")
        print("\n" + "-" * 60)
        print("RESULT: Neo4j Python Driver connection WORKING")
        print("        Credentials valid, Bolt protocol functional")
        print("-" * 60)
        print("\nStatus: PASS")
        return True

    except Exception as e:
        print(f"\n[FAIL] Connection failed: {e}")
        print("\nStatus: FAIL")
        return False


# ── Main ────────────────────────────────────────────────────────

def main():
    print(f"\nConfiguration loaded from {env_path}:")
    print(f"  Neo4j Host: {NEO4J_HOST}")
    print(f"  Bolt URI: {NEO4J_BOLT_URI}")
    print()

    test_environment()
    tcp_ok = test_tcp_connectivity()
    driver_ok = test_python_driver()

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  TCP Connectivity: {'PASS' if tcp_ok else 'FAIL'}")
    print(f"  Python Driver:    {'PASS' if driver_ok else 'FAIL'}")
    print()

    if not (tcp_ok and driver_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
