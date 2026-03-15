# Automating Private Link Setup for Neo4j Marketplace Deployments

## Private Connectivity from Databricks Serverless to Neo4j

Databricks serverless compute runs inside Databricks-managed infrastructure with no customer-controlled VNet, no stable outbound IPs, and no direct path to resources in a customer's Azure network. Neo4j Enterprise Edition runs on VMs inside a customer-managed VNet. Connecting the two without exposing Neo4j to the public internet requires Azure Private Link.

This demo proves that the connection works. A Databricks serverless notebook runs a Python program that connects to a Neo4j Enterprise cluster over the Bolt protocol (port 7687), routed entirely over the Azure backbone via NCC private endpoint, Private Link Service, and an internal load balancer. No public internet, no IP allowlisting, no firewall rules between Databricks and Neo4j.

The private connectivity pattern here is the same one that Neo4j Aura VDC uses for its managed service: a Private Link Service in front of the database, with customers creating private endpoints to reach it. The difference is operational. Aura VDC manages the Private Link infrastructure automatically; with self-hosted Enterprise Edition, we build that layer ourselves. From the Databricks notebook's perspective, the connection is identical in both cases.

The public load balancer and Neo4j Browser (port 7474) remain accessible over the internet for cluster administration. Only driver traffic travels the private path.

## Port Clarification

The Neo4j port numbering matters because the wrong port behind Private Link means the demo fails silently.

- **7687 (Bolt)** is the binary protocol the Neo4j Python driver uses. This is the only port that needs to traverse Private Link for driver connectivity from Databricks serverless.
- **7474 (HTTP)** serves the Neo4j Browser web UI and REST API. It is not used by the Python driver. For this demo, the Browser stays on the public load balancer for administration.
- **7473 (HTTPS)** is the TLS-encrypted variant of the Browser. Not needed for the demo.
- **7688 (Bolt Routing)** is used for cluster-aware routing in multi-node clusters. The internal load balancer handles routing, so this port is not required on the Private Link path.

The internal load balancer forwards only port 7687. The health probe checks port 7687 via TCP. This is the minimal configuration for the demo.

## The Gap Between Marketplace and Private Connectivity

The Neo4j Enterprise marketplace deployment creates a fully functional cluster: a VMSS, a public load balancer (for 3+ nodes), a VNet with a single subnet, and per-instance public IPs. Every resource the database needs to run. None of the resources Databricks serverless needs to reach it privately.

NCC_EE.md lays out seven manual steps to bridge that gap, each requiring the operator to look up resource IDs, substitute them into Azure CLI commands, and run them in the right order. The steps are straightforward individually, but together they form a fragile chain. One wrong resource ID, one missed subnet flag, one typo in a VMSS update command, and the private endpoint never establishes. The manual process also doesn't encode the relationship between the marketplace deployment and the private link infrastructure, so there's no way to tear it down cleanly or redeploy it against a fresh cluster.

This proposal replaces the manual steps with a Bicep template and Python scripts. The Bicep template declares the private link infrastructure as a deployable unit. The setup script discovers the marketplace deployment's resources by name and feeds them as parameters. A companion teardown script reverses the process without touching the marketplace deployment.

## Why Bicep for Infrastructure, Python for Orchestration

The ARM templates that power the marketplace deployment are already written in Bicep. The modules live in `marketplace/neo4j-enterprise/modules/` as `network.bicep`, `loadbalancer.bicep`, `vmss.bicep`, and `identity.bicep`. The private link infrastructure belongs in the same language: a Bicep template that declares the internal load balancer, NAT subnet, and Private Link Service as a deployable unit. Azure Resource Manager handles idempotency — running `az deployment group create` with the same template and parameters a second time updates existing resources rather than failing or creating duplicates.

The orchestration layer — discovering marketplace resources, deploying the Bicep template, updating the VMSS, printing next steps — is Python. A shared helper module (`private_link_helpers.py`) provides an `az` CLI wrapper, `.env` loading, and resource discovery that the setup and teardown scripts import. Python provides better error handling and structured output compared to a bash script, and the team already has the pattern established from the validation work.

The one operation Bicep cannot express is updating the existing VMSS to join the internal load balancer's backend pool. That VMSS was created by the marketplace template, and Bicep's `existing` keyword can reference it but not safely modify its network profile without risking a conflict with the marketplace's own template. The setup script handles this single imperative step via `az vmss update`, keeping the Bicep template purely declarative.

## What the Marketplace Deployment Creates

The ARM template repo at `neo4j-partners/azure-resource-manager-neo4j` reveals the naming conventions and structure that the automation must work with.

**Resource naming.** Every resource uses a suffix derived from `uniqueString(resourceGroup().id, deployment().name)`. The VMSS is named `vmss-neo4j-{location}-{suffix}`, the VNet `vnet-neo4j-{location}-{suffix}`, the load balancer `lb-neo4j-{location}-{suffix}`. The setup script can discover these by querying the resource group for resources of the right type, or the operator can provide the VMSS name in `.env`.

**Network layout.** The VNet uses `10.0.0.0/8` with a single subnet at `10.0.0.0/16` named `subnet`. The address space has room for additional subnets; the Private Link NAT subnet fits at `10.1.0.0/24` without conflicts.

**VMSS network profile.** Each instance has one NIC named `nic` with one IP configuration named `ipconfig-cluster`. The IP configuration includes a public IP and, for 3+ node clusters, membership in the public load balancer's backend pool named `backend`. This NIC-based configuration is exactly what Azure Private Link Service requires for its backend pool.

**Load balancer.** Created only when `nodeCount >= 3`. Standard SKU, public frontend, health probes on ports 7474 (HTTP) and 7687 (TCP/Bolt). The internal load balancer we create mirrors the Bolt probe but skips the HTTP probe since only driver traffic crosses Private Link.

## VMSS Backend Pool Update: Validated Safe

Adding the internal load balancer's backend pool to the existing VMSS requires `az vmss update` followed by `az vmss update-instances --instance-ids "*"` to apply the new model to running instances. The marketplace template sets the VMSS upgrade policy to `Manual`, so the `update-instances` command is required to propagate the change.

**Tested on 2026-03-15 against `rk-neo4j-ee-private-link`.** The validation script (`validation/vmss_update_test.py`) created a temporary internal Standard Load Balancer, added the VMSS to its backend pool, updated all three instances, and monitored Neo4j Bolt connectivity throughout at 2-second intervals. Results:

- 41 connection checks over the full cycle (VMSS update, instance upgrade, 30-second soak)
- **Zero connection drops**
- Average latency: 714ms, max: 1662ms (consistent with baseline)
- Neo4j processes did not restart; no connection interruptions

The backend pool change is a live networking configuration update. Azure applies it to the NIC without restarting the VM or disrupting the Neo4j process. The setup script does not need a confirmation prompt or rolling update strategy for this step.

## The Automation Design

A Bicep template, two Python scripts, and a shared helper module. The Bicep template declares the private link infrastructure. The setup script orchestrates discovery, deployment, and VMSS wiring. The teardown script reverses everything. The shared module provides reusable discovery and CLI logic extracted from the validated `vmss_update_test.py`.

### Shared Module: `private_link_helpers.py`

Provides:

- **`az(args)`** — Runs `az` CLI commands, returns parsed JSON, prints the command for visibility.
- **`load_env()` / `require_env(key)` / `optional_env(key)`** — Loads `.env` from the project root. Environment variables take precedence over `.env` values.
- **`discover_vmss(resource_group)`** — Finds the Neo4j VMSS by name prefix `vmss-neo4j-`, extracts VNet name, subnet name, subnet ID, location, and subscription ID from the VMSS network profile and resource ID.
- **`discover_neo4j_uri(resource_group)`** — Finds the public LB IP/FQDN and constructs the Bolt URI.

All discovery is driven by the resource group name alone. The scripts discover the VMSS, VNet, subnet, subscription, and location automatically — no manual ID lookups required.

### Bicep Template: `private-link.bicep`

The template accepts parameters that describe the existing marketplace deployment and creates three resources.

**Parameters:**
- `vnetName` — the marketplace VNet (discovered by the setup script)
- `vnetResourceGroup` — resource group containing the VNet (usually the same group)
- `neo4jSubnetName` — the existing subnet where Neo4j VMs run (default: `subnet`)
- `plsSubnetPrefix` — address prefix for the Private Link NAT subnet (default: `10.1.0.0/24`)
- `location` — Azure region (discovered from VMSS)

**Resources created:**

1. **Private Link NAT subnet** (`pls-nat-subnet`). A new subnet in the existing VNet with `privateLinkServiceNetworkPolicies` set to `Disabled`. Azure uses this subnet for NAT IP addresses when proxying traffic through the Private Link Service.

2. **Internal Standard Load Balancer** (`neo4j-internal-lb`). Standard SKU, internal (no public IP), frontend IP in the existing Neo4j subnet.
   - **Health probe:** TCP on port 7687 (Bolt), 5-second interval, probe threshold of 2. This means Azure marks a backend instance unhealthy after two consecutive failures (10 seconds). The 5-second interval balances responsiveness against probe overhead for a 3-node cluster.
   - **Load balancing rule:** Forwards port 7687 to the backend pool. TCP Reset enabled — when the idle timeout is reached, Azure sends TCP RST to both the client and server so the Neo4j driver detects the closed connection immediately instead of waiting for a TCP retransmission timeout. Idle timeout uses the Azure default of 4 minutes (configurable from 4 to 100 minutes), which is sufficient for the demo since connections are short-lived. For production use with long-lived connection pools, increase this and ensure the Neo4j driver's `keep_alive=True` (enabled by default) sends keepalives more frequently than the timeout.
   - **No HTTP (7474) rule.** The Neo4j Browser stays on the public load balancer for cluster administration.

3. **Private Link Service** (`neo4j-pls`). Attached to the internal load balancer's frontend IP configuration, using the NAT subnet for NAT IP addresses. Visibility set to `*` (all subscriptions) so the Databricks-managed subscription can create a private endpoint without needing to look up its subscription ID. Visibility only controls which subscriptions can *request* a connection — it does not grant access. All connections still require manual approval in the Azure portal, which is the simplest path and matches how the demo flow works.

The template outputs the Private Link Service resource ID, which the operator uses when creating the NCC private endpoint rule in Databricks.

### Python Script: `setup-private-link.py`

Run with `uv run setup-private-link.py`. All configuration comes from `.env` — only `RESOURCE_GROUP` is required; everything else is discovered.

**Phase 1: Discovery.** Imports helpers from `private_link_helpers.py`. Queries the resource group, finds the VMSS by name prefix `vmss-neo4j-`, extracts VNet, subnet, subscription ID, and location from the VMSS network profile. Prints what it found.

**Phase 2: Bicep deployment.** Runs `az deployment group create` with `private-link.bicep`, passing discovered parameters. Bicep handles idempotency — safe to run multiple times.

**Phase 3: VMSS update.** Adds the internal load balancer's backend pool to the VMSS network profile via `az vmss update`, then applies to running instances with `az vmss update-instances --instance-ids "*"`. No confirmation prompt — this operation was validated safe with zero connection drops across 41 connection checks (see Phase 2 progress log).

**Phase 4: Output.** Prints a structured summary:
- What was created (resource names)
- The Private Link Service resource ID (copyable, on its own line)
- Numbered next steps: create NCC rule, approve connection, test from notebook
- NCC API curl command pre-filled with values from `.env` (`DATABRICKS_ACCOUNT_ID`, `NCC_ID`, PLS resource ID, `NEO4J_DOMAIN`), with `<TOKEN>` as the only placeholder
- A sample Python notebook cell ready to paste into Databricks

### Python Script: `teardown-private-link.py`

Run with `uv run teardown-private-link.py`. Reverses the setup without touching the marketplace deployment:

1. Remove the internal load balancer's backend pool from the VMSS network profile
2. Update VMSS instances to apply the change
3. Delete the Private Link Service (`az network private-link-service delete`)
4. Delete the internal load balancer (`az network lb delete`)
5. Delete the NAT subnet (`az network vnet subnet delete`)

Complete cleanup — all three resources created by the Bicep template are removed. `az deployment group delete` only removes deployment metadata, not actual resources, so the teardown script deletes each resource explicitly in dependency order. The script discovers resource names the same way the setup script does: from the resource group via `private_link_helpers.py`.

The script does not remove the NCC private endpoint rule in Databricks or the approved connection; those are cleaned up separately in the Databricks account console.

### Configuration: `.env` File

A `.env` file in the project root provides configuration. Only `RESOURCE_GROUP` is required — everything else is discovered automatically or has sensible defaults:

```bash
# Required
RESOURCE_GROUP=my-neo4j-rg

# Optional — discovered automatically from the VMSS if not set
VMSS_NAME=
VNET_NAME=
NEO4J_SUBNET_NAME=subnet

# Neo4j credentials (used by validation tests)
NEO4J_URI=
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# Private Link configuration
PLS_SUBNET_PREFIX=10.1.0.0/24

# Databricks NCC (used in setup script output for the curl command)
DATABRICKS_ACCOUNT_ID=
NCC_ID=
NEO4J_DOMAIN=neo4j-ee.private.neo4j.com
```

The scripts load `.env` if present. Environment variables take precedence over `.env` values.

## The Demo Flow

The end-to-end demo follows this sequence:

1. **Deploy Neo4j EE from the Azure Marketplace.** Standard 3-node cluster deployment. Takes approximately 10 minutes.

2. **Run `uv run setup-private-link.py`.** Discovers the marketplace resources, deploys the Bicep template, updates the VMSS. Takes approximately 5 minutes. Outputs the Private Link Service resource ID and next steps.

3. **Create the NCC private endpoint rule in Databricks.** Paste the resource ID into the Databricks account console or use the curl command the script prints. Takes 1 minute.

4. **Approve the connection in the Azure portal.** Go to Private Link Center, find the pending connection on `neo4j-pls`, click Approve. Wait for the NCC status to show `ESTABLISHED` (up to 10 minutes).

5. **Run the sample notebook.** From a Databricks serverless notebook:

```python
%pip install neo4j

from neo4j import GraphDatabase

uri = "neo4j://neo4j-ee.private.neo4j.com:7687"
auth = ("neo4j", "<PASSWORD>")

with GraphDatabase.driver(uri, auth=auth) as driver:
    driver.verify_connectivity()
    records, _, _ = driver.execute_query(
        "RETURN 'Connected over Private Link' AS message"
    )
    print(records[0]["message"])
```

The `neo4j://` scheme (not `neo4j+s://`) is correct here because traffic travels over the Azure backbone via Private Link. TLS between the driver and server is optional when the network path is private.

6. **Tear down with `uv run teardown-private-link.py`.** Removes all three private link resources (PLS, internal LB, NAT subnet), leaving the marketplace deployment intact.

## Reusability

The Bicep template is parameterized against the marketplace deployment, not coupled to it. It needs a VNet name, a subnet name, and a region. Those could come from any deployment that puts Neo4j VMs behind a NIC-based VMSS in an Azure VNet, whether the source is the marketplace, a custom ARM template, or a manual VM deployment. The Python scripts add the marketplace-specific discovery logic, but the Bicep template stands alone.

For teams managing multiple environments, the `.env` file provides per-environment configuration. Copy `.env` with environment-specific values (`RESOURCE_GROUP`, credentials) for each target deployment.

## What the Automation Does Not Cover

The Databricks NCC configuration remains manual. Creating the NCC private endpoint rule, approving the connection in the Azure portal, and attaching the NCC to the workspace are account-level operations that require Databricks account admin credentials. The setup script prints the exact resource ID and curl command needed, but executing them is a deliberate manual step.

DNS configuration is also out of scope. The operator must ensure that the domain name used in the NCC rule resolves to the internal load balancer's frontend IP within their VNet. For the demo, the simplest approach is to use the domain name in the NCC private endpoint rule directly; Databricks resolves it to the private endpoint automatically.

## File Structure

```
private-link-ee/
  README.md                    # Quick start and testing guide
  NCC_EE.md                    # Architecture and manual steps (existing)
  PRIVATE_LINK.md              # This proposal
  private-link.bicep           # Bicep template (internal LB, NAT subnet, PLS)
  private_link_helpers.py      # Shared module: az wrapper, discovery, env loading
  setup-private-link.py        # Setup script: discover, deploy, wire VMSS
  teardown-private-link.py     # Teardown script: remove PLS, LB, subnet
  .env.sample                  # Example configuration
```

## Open Questions

1. **Single-node deployments.** The marketplace template only creates a public load balancer for 3+ node clusters. For single-node deployments, the VMSS has no existing load balancer, but the internal LB created by the Bicep template works the same way — it targets the VMSS regardless of instance count. Azure Private Link Service requires a Standard Load Balancer, which we create. Should the demo require a 3-node cluster, or should the script handle the single-node case? (The Bicep template and VMSS update logic are identical either way; the only difference is that single-node deployments have no public LB to compare against.)

2. ~~**VMSS update validation.**~~ **Resolved.** Tested on 2026-03-15 — zero connection drops. The backend pool change is a live networking update, safe for running clusters.

3. ~~**Confirmation prompt for VMSS update.**~~ **Resolved.** Skipped. The VMSS update was validated safe with 41 connection checks and zero drops. No confirmation prompt needed.

---

## Progress Log

### Phase 1: Discovery — Completed (2026-03-14)

**Resource group:** `rk-neo4j-ee-private-link`

Queried the resource group and confirmed a standard 3-node marketplace deployment with the resource suffix `5k3g2fygo27h6`. All resources match the naming conventions documented in the ARM templates.

**Discovered resources:**

| Resource | Name |
|----------|------|
| VMSS | `vmss-neo4j-eastus-5k3g2fygo27h6` (3 instances: 0, 1, 2) |
| VNet | `vnet-neo4j-eastus-5k3g2fygo27h6` |
| Subnet | `subnet` (10.0.0.0/16 within 10.0.0.0/8 address space) |
| Public LB | `lb-neo4j-eastus-5k3g2fygo27h6` (Standard SKU, Regional) |
| NSG | `nsg-neo4j-eastus-5k3g2fygo27h6` |
| Public IP | `ip-neo4j-eastus-5k3g2fygo27h6` |
| Managed Identity | `usermanaged-neo4j-eastus-5k3g2fygo27h6` |

**Validated:**
- VMSS NIC uses IP configuration `ipconfig-cluster` with NIC-based backend pool membership (required for Private Link Service)
- VMSS is already in the public LB backend pool `backend`
- VNet has room for the PLS NAT subnet at `10.1.0.0/24` (only `10.0.0.0/16` is allocated)
- Public LB is Standard SKU with rules for ports 7474 and 7687
- Location: `eastus`
- Subscription: `47fd4ce5-a912-480e-bb81-95fbd59bb6c5`

**Discovery confirms the deployment is compatible with the proposed Private Link setup.** The NIC-based backend pool, Standard SKU load balancer, and available VNet address space are all prerequisites, and all are satisfied.

### Phase 2: VMSS Update Test — Passed (2026-03-15)

Ran `validation/vmss_update_test.py` against the live 3-node cluster. The script created a temporary internal Standard Load Balancer (`neo4j-test-internal-lb`), added the VMSS to its backend pool, updated all instances, and monitored Bolt connectivity at 2-second intervals throughout.

**Results:**
- 41 connection checks across vmss_update, instance upgrade, and 30-second soak phases
- **0 connection drops**
- Avg latency: 714ms, Max: 1662ms (consistent with baseline of ~900-1400ms)
- Temporary LB cleaned up automatically after test

The backend pool change is a live NIC configuration update. Azure does not restart the VM or interrupt the Neo4j process. The setup script can proceed without a confirmation gate or rolling update.

### Phase 3: Bicep Template, Shared Module, and Python Scripts — Completed (2026-03-14)

Implemented four files:

1. **`private_link_helpers.py`** — Shared module extracted from `validation/vmss_update_test.py`. Provides `az()` CLI wrapper, `.env` loading (`load_env`, `require_env`, `optional_env`), and resource discovery (`discover_vmss`, `discover_neo4j_uri`). The `discover_vmss` function now also extracts `subscription_id` from the VMSS resource ID, eliminating the need for manual subscription lookups.

2. **`private-link.bicep`** — Bicep template declaring three resources:
   - NAT subnet (`pls-nat-subnet`) with `privateLinkServiceNetworkPolicies: 'Disabled'`
   - Internal Standard LB (`neo4j-internal-lb`) with TCP health probe on 7687 (5s interval, threshold 2), load balancing rule (port 7687, TCP Reset enabled, default 4-minute idle timeout)
   - Private Link Service (`neo4j-pls`) with visibility `['*']`, no auto-approval, NAT IP on the NAT subnet
   - Outputs: PLS resource ID, PLS name, LB name, backend pool ID

3. **`setup-private-link.py`** — Four-phase setup script: discover (from resource group), deploy Bicep, update VMSS backend pool (idempotent — checks if already added), print structured output with PLS resource ID, next steps, pre-filled curl command, and sample notebook cell. Run with `uv run setup-private-link.py`.

4. **`teardown-private-link.py`** — Teardown script: remove VMSS from backend pool, delete PLS, delete LB, delete NAT subnet. Complete cleanup in dependency order. All deletions use `check=False` for graceful handling of already-deleted resources. Run with `uv run teardown-private-link.py`.

### Next Step: Phase 4 — Deploy and Test

Deploy the Private Link infrastructure against the live `rk-neo4j-ee-private-link` cluster:

1. Run `uv run setup-private-link.py` to deploy the Bicep template and wire the VMSS
2. Create the NCC private endpoint rule in Databricks using the output curl command
3. Approve the connection in the Azure portal
4. Test connectivity from a Databricks serverless notebook
5. Verify teardown with `uv run teardown-private-link.py`
