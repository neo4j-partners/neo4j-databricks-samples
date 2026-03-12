# Neo4j Connectivity Test

Validates network and driver connectivity from a Databricks cluster to Neo4j Aura.

## Tests

| Section | Test | What it proves |
|---------|------|----------------|
| 1 | Environment Info | Cluster runtime and Neo4j Python driver version |
| 2 | TCP Connectivity | Network path to Neo4j on port 7687 is open |
| 3 | Python Driver | Credentials work, Bolt protocol queries execute |

## Setup

1. Copy `.env.sample` to `.env` and fill in your Neo4j credentials:
   ```bash
   cp .env.sample .env
   ```

2. Run the setup script to store credentials as Databricks secrets:
   ```bash
   ./setup.sh
   ```

3. Install the `neo4j` Python package on your cluster (or add `%pip install neo4j` to the notebook).

4. Import and run `neo4j_connectivity_test.ipynb` in Databricks.

## Serverless Compute

The `neo4j` Python package is not pre-installed on serverless. Install it via the **Environment** side panel (add `neo4j` as a dependency) or with a cell at the top of the notebook:

```python
%pip install neo4j
```

> **Do not** install `pyspark` or any library that pulls in `pyspark` — this will crash the serverless session.

### Section compatibility

| Section | Serverless | Notes |
|---------|:----------:|-------|
| 1 — Environment Info | Yes | Requires `neo4j` PyPI install (see above) |
| 2 — TCP Connectivity | Likely No | Uses `nc` (netcat) which may not be available — see workaround below |
| 3 — Python Driver | Yes | Pure Python, no JAR dependency |

### TCP test workaround

If `nc` is not available on serverless, replace the network test with a Python socket check:

```python
import socket, time

start = time.time()
sock = socket.create_connection((NEO4J_HOST, 7687), timeout=10)
elapsed = (time.time() - start) * 1000
sock.close()
print(f"[PASS] TCP connection established in {elapsed:.1f}ms")
```
