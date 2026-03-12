# Neo4j Connectivity Test

Validates network and driver connectivity to Neo4j Aura — first from your local machine, then from a Databricks cluster.

## Tests

| Section | Test | What it proves |
|---------|------|----------------|
| 1 | Environment Info | Runtime and Neo4j Python driver version |
| 2 | TCP Connectivity | Network path to Neo4j on port 7687 is open |
| 3 | Python Driver | Credentials work, Bolt protocol queries execute |

## Setup

1. Copy `.env.sample` to `.env` and fill in your Neo4j credentials:
   ```bash
   cd connect-test
   cp .env.sample .env
   ```

## Step 1: Run locally

Run the connectivity test from your local machine first to verify your credentials and that Neo4j is reachable. This requires [uv](https://docs.astral.sh/uv/).

```bash
cd connect-test
uv run connectivity_test.py
```

Dependencies are declared inline in the script — `uv run` installs them automatically.

If all tests pass locally, your credentials are valid and Neo4j is accepting connections. Any failures on Databricks are then isolated to the cluster's network configuration.

## Step 2: Run on Databricks

Once the local test passes, run the same tests from your Databricks cluster.

1. Configure credentials using **one** of these options:
   - **Databricks Secrets**: Run the setup script to store credentials as secrets, then use Option A in the notebook:
     ```bash
     cd connect-test
     ./setup.sh
     ```
   - **Direct values**: Skip the setup script and uncomment Option B in the notebook's configuration cell, entering your credentials directly.

2. Import and run `connect-test/neo4j_connectivity_test.ipynb` in Databricks.

### Serverless Compute

The `neo4j` Python package is not pre-installed on serverless. Install it via the **Environment** side panel (add `neo4j` as a dependency) or with a cell at the top of the notebook:

```python
%pip install neo4j
```

#### Section compatibility

| Section | Serverless | Notes |
|---------|:----------:|-------|
| 1 — Environment Info | Yes | Requires `neo4j` PyPI install (see above) |
| 2 — TCP Connectivity | Likely No | Uses `nc` (netcat) which may not be available — see workaround below |
| 3 — Python Driver | Yes | Pure Python, no JAR dependency |

#### TCP test workaround

If `nc` is not available on serverless, replace the network test with a Python socket check:

```python
import socket, time

start = time.time()
sock = socket.create_connection((NEO4J_HOST, 7687), timeout=10)
elapsed = (time.time() - start) * 1000
sock.close()
print(f"[PASS] TCP connection established in {elapsed:.1f}ms")
```
