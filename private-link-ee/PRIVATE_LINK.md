# Private Link Implementation Details

## Overview

This demo demonstrates how to connect Databricks serverless compute to a self-hosted Neo4j Enterprise Edition cluster without exposing Neo4j to the public internet, using Azure Private Link. A Databricks serverless notebook connects to the Neo4j cluster over the Bolt protocol (port 7687), routed entirely over the Azure backbone via an NCC private endpoint, Private Link Service, and an internal load balancer. No public internet, no IP allowlisting, no firewall rules between Databricks and Neo4j.

We decided to use this approach because our research found that Databricks serverless compute runs inside Databricks-managed infrastructure with no customer-controlled VNet, no stable outbound IPs, and no direct path to resources in a customer's Azure network. Neo4j Enterprise Edition runs on VMs inside a customer-managed VNet. Azure Private Link is the only way to connect the two privately.

The private connectivity pattern here is the same one that Neo4j Aura VDC uses for its managed service: a Private Link Service in front of the database, with customers creating private endpoints to reach it. The difference is operational. Aura VDC manages the Private Link infrastructure automatically; with self-hosted Enterprise Edition, we build that layer ourselves. From the Databricks notebook's perspective, the connection is identical in both cases.

The public load balancer and Neo4j Browser (port 7474) remain accessible over the internet for cluster administration. Only driver traffic travels the private path.

## Port Details

The Neo4j port numbering matters because the wrong port behind Private Link means the connection fails silently.

- **7687 (Bolt)** — Binary protocol the Neo4j Python driver uses. The only port that traverses Private Link.
- **7474 (HTTP)** — Neo4j Browser web UI and REST API. Stays on the public load balancer for administration.
- **7473 (HTTPS)** — TLS-encrypted Browser variant. Not needed.
- **7688 (Bolt Routing)** — Cluster-aware routing in multi-node clusters. The internal load balancer handles routing, so this port is not required on the Private Link path.

The internal load balancer forwards only port 7687. The health probe checks port 7687 via TCP.

## Marketplace Deployment Resources

The ARM template repo at `neo4j-partners/azure-resource-manager-neo4j` defines the naming conventions and structure the automation works with.

**Resource naming.** Every resource uses a suffix derived from `uniqueString(resourceGroup().id, deployment().name)`. The VMSS is `vmss-neo4j-{location}-{suffix}`, the VNet `vnet-neo4j-{location}-{suffix}`, the load balancer `lb-neo4j-{location}-{suffix}`. The setup script discovers these by querying the resource group, or the operator can set `VMSS_NAME` in `.env`.

**Network layout.** The VNet uses `10.0.0.0/8` with a single subnet at `10.0.0.0/16` named `subnet`. The Private Link NAT subnet fits at `10.1.0.0/24` without conflicts.

**VMSS network profile.** Each instance has one NIC named `nic` with one IP configuration named `ipconfig-cluster`. The IP configuration includes a public IP and, for 3+ node clusters, membership in the public load balancer's backend pool named `backend`. This NIC-based configuration is what Azure Private Link Service requires for its backend pool.

**Load balancer.** Created only when `nodeCount >= 3`. Standard SKU, public frontend, health probes on ports 7474 (HTTP) and 7687 (TCP/Bolt). The internal load balancer mirrors the Bolt probe but skips the HTTP probe since only driver traffic crosses Private Link.

## VMSS Backend Pool Update

Adding the internal load balancer's backend pool to the existing VMSS requires `az vmss update` followed by `az vmss update-instances --instance-ids "*"`. The marketplace template sets the VMSS upgrade policy to `Manual`, so `update-instances` is required to propagate the change.

**Validated safe.** Tested on 2026-03-15 against a live 3-node cluster: 41 connection checks over the full cycle (VMSS update, instance upgrade, 30-second soak), zero connection drops, average latency 714ms. The backend pool change is a live NIC configuration update — Azure applies it without restarting the VM or disrupting the Neo4j process.

## Bicep Template: `private-link.bicep`

The template accepts parameters describing the existing marketplace deployment and creates three resources.

**Parameters:**
- `vnetName` — the marketplace VNet (discovered by the setup script)
- `vnetResourceGroup` — resource group containing the VNet (usually the same group)
- `neo4jSubnetName` — existing subnet where Neo4j VMs run (default: `subnet`)
- `plsSubnetPrefix` — address prefix for the Private Link NAT subnet (default: `10.1.0.0/24`)
- `location` — Azure region (discovered from VMSS)

**Resources created:**

1. **Private Link NAT subnet** (`pls-nat-subnet`). A new subnet in the existing VNet with `privateLinkServiceNetworkPolicies` set to `Disabled`. Azure uses this subnet for NAT IP addresses when proxying traffic through the Private Link Service.

2. **Internal Standard Load Balancer** (`neo4j-internal-lb`). Standard SKU, internal (no public IP), frontend IP in the existing Neo4j subnet.
   - **Health probe:** TCP on port 7687, 5-second interval, probe threshold of 2 (unhealthy after 10 seconds).
   - **Load balancing rule:** Forwards port 7687. TCP Reset enabled so the Neo4j driver detects closed connections immediately. Idle timeout uses the Azure default of 4 minutes. For production use with long-lived connection pools, increase this and ensure the Neo4j driver's `keep_alive=True` sends keepalives more frequently than the timeout.

3. **Private Link Service** (`neo4j-pls`). Attached to the internal load balancer's frontend IP, using the NAT subnet. Visibility set to `*` (all subscriptions can request connections), but auto-approval is disabled — every connection requires explicit approval.

The template outputs the Private Link Service resource ID for use when creating the NCC private endpoint rule.

## Script Internals

### Shared Module: `helpers.py`

- **`az(args)`** — Runs `az` CLI commands, returns parsed JSON, prints the command for visibility.
- **`load_env()` / `require_env(key)` / `optional_env(key)`** — Loads `.env` from the project root. Environment variables take precedence.
- **`discover_vmss(resource_group)`** — Finds the Neo4j VMSS by name prefix `vmss-neo4j-`, extracts VNet, subnet, location, and subscription ID from the VMSS network profile and resource ID.
- **`discover_neo4j_uri(resource_group)`** — Finds the public LB IP/FQDN and constructs the Bolt URI.

### Setup: `setup.py`

Four phases: (1) discover resources from the resource group, (2) deploy the Bicep template, (3) add the VMSS to the internal LB backend pool (idempotent — skips if already added), (4) print the PLS resource ID and next steps including a pre-filled NCC API curl command.

### Teardown: `teardown.py`

Reverses setup in dependency order: remove VMSS from backend pool, delete PLS, delete LB, delete NAT subnet. All deletions use `check=False` for graceful handling of already-deleted resources. Does not touch the marketplace deployment or Databricks NCC resources.

## Reusability

The Bicep template is parameterized against the marketplace deployment, not coupled to it. It needs a VNet name, a subnet name, and a region. Those could come from any deployment that puts Neo4j VMs behind a NIC-based VMSS in an Azure VNet — marketplace, custom ARM template, or manual VM deployment. The Python scripts add marketplace-specific discovery logic, but the Bicep template stands alone.
