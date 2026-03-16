# Neo4j EE Private Link for Databricks Serverless

This demo demonstrates how to connect Databricks serverless compute to a self-hosted Neo4j Enterprise Edition cluster without exposing Neo4j to the public internet, using Azure Private Link. A Databricks serverless notebook connects to the Neo4j cluster over the Bolt protocol (port 7687), routed entirely over the Azure backbone via an NCC private endpoint, Private Link Service, and an internal load balancer. No public internet, no IP allowlisting, no firewall rules between Databricks and Neo4j.

We decided to use this approach because our research found that Databricks serverless compute runs inside Databricks-managed infrastructure with no customer-controlled VNet, no stable outbound IPs, and no direct path to resources in a customer's Azure network. Neo4j Enterprise Edition runs on VMs inside a customer-managed VNet. Azure Private Link is the only way to connect the two privately.

The private connectivity pattern here is the same one that Neo4j Aura VDC uses for its managed service: a Private Link Service in front of the database, with customers creating private endpoints to reach it. The difference is operational. Aura VDC manages the Private Link infrastructure automatically; with self-hosted Enterprise Edition, we build that layer ourselves. From the Databricks notebook's perspective, the connection is identical in both cases.

The public load balancer and Neo4j Browser (port 7474) remain accessible over the internet for cluster administration. Only driver traffic travels the private path.

## What This Does

The Neo4j marketplace deployment creates a VMSS, a public load balancer, and a VNet. This project extends the marketplace deployment to add Private Link for serverless. It adds three resources to the existing deployment:

1. **Internal Load Balancer** (Standard SKU, forwards Bolt traffic on port 7687 to the Neo4j VMSS)
2. **NAT Subnet** (dedicated subnet for Private Link Service NAT IP addresses)
3. **Private Link Service** (exposes the internal LB as a private endpoint target)

A Bicep template declares these resources. Python scripts handle discovery, deployment, VMSS wiring, connection approval, and workspace attachment. The setup script discovers everything it needs from the resource group; no manual ID lookups required.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Azure Backbone (private)                          │
│                                                                            │
│  ┌──────────────────────┐         ┌──────────────────────────────────────┐ │
│  │ Databricks-managed   │         │ Customer VNet (10.0.0.0/8)          │ │
│  │                      │         │                                      │ │
│  │  ┌────────────────┐  │         │  ┌────────────┐    ┌──────────────┐ │ │
│  │  │  Serverless    │  │         │  │ Private    │    │ NAT Subnet   │ │ │
│  │  │  Notebook      │──┼────┐    │  │ Link       │    │ 10.1.0.0/24  │ │ │
│  │  │                │  │    │    │  │ Service    │◄───┤              │ │ │
│  │  └────────────────┘  │    │    │  │ (neo4j-pls)│    └──────────────┘ │ │
│  │                      │    │    │  └─────┬──────┘                     │ │
│  └──────────────────────┘    │    │        │                            │ │
│                              │    │        ▼                            │ │
│  ┌──────────────────────┐    │    │  ┌────────────┐    ┌──────────────┐ │ │
│  │ NCC                  │    │    │  │ Internal   │    │ Neo4j VMSS   │ │ │
│  │                      │    │    │  │ LB :7687   │───►│              │ │ │
│  │  ┌────────────────┐  │    │    │  │            │    │  ┌─┐ ┌─┐ ┌─┐│ │ │
│  │  │ Private        │◄─┼────┘    │  └────────────┘    │  │0│ │1│ │2││ │ │
│  │  │ Endpoint       │  │         │                    │  └─┘ └─┘ └─┘│ │ │
│  │  └────────────────┘  │         │  ┌────────────┐    │              │ │ │
│  │                      │         │  │ Public LB  │───►│  Bolt :7687  │ │ │
│  └──────────────────────┘         │  │ :7474/7687 │    │  Browser:7474│ │ │
│                                   │  └────────────┘    └──────────────┘ │ │
│                                   └──────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘

Traffic flow: Notebook ──► NCC Private Endpoint ──► Private Link Service
              ──► Internal LB ──► Neo4j VMSS (port 7687)
```

## Prerequisites

- **Azure CLI** with Bicep support (`az bicep version`; [install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli))
- **uv** ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **Databricks CLI** ([install](https://docs.databricks.com/dev-tools/cli/install.html)), used by `attach-ncc` and `setup-secrets.sh` for authentication
- **Neo4j EE marketplace deployment**, a running 3-node cluster deployed from the [Azure Marketplace](https://azuremarketplace.microsoft.com/en-us/marketplace/apps/neo4j.neo4j-ee)
- **Databricks workspace** in the same Azure region as the Neo4j cluster
- **Azure permissions**: Contributor on the resource group containing the Neo4j deployment
- **Databricks permissions**: Account admin (to create NCC private endpoint rules and attach to workspaces)

### Databricks CLI Profile Setup

The `attach-ncc` command uses a Databricks CLI profile to authenticate with the account console. Set up a profile in `~/.databrickscfg`:

```ini
[azure-account-admin]
host       = https://accounts.azuredatabricks.net
account_id = <your-azure-account-id>
auth_type  = databricks-cli
```

Then authenticate (opens a browser for OAuth login):

```bash
databricks auth login --profile azure-account-admin
```

Verify it works:

```bash
databricks auth token --profile azure-account-admin
```

## Quick Start

```bash
cd private-link-ee

# 1. Interactive setup: asks for resource group and Neo4j password,
#    discovers everything else, writes .env
uv run setup-private-link --init

# 2. Deploy Private Link infrastructure
uv run setup-private-link

# 3. Verify resources were created
uv run verify-private-link

# 4. Create NCC private endpoint rule in Databricks (see setup output)

# 5. Approve the pending connection
uv run approve-private-link

# 6. Attach the NCC to your workspace
uv run attach-ncc --profile azure-account-admin

# 7. Store secrets and test from a notebook
./setup-secrets.sh <databricks-cli-profile>

# 8. When done, tear down
uv run teardown-private-link
uv run verify-private-link --cleanup

# 9. Remove the NCC and private endpoint rule from Databricks
uv run detach-ncc --profile azure-account-admin
```

## Configuration

Run `uv run setup-private-link --init` to create `.env` interactively, or copy `.env.sample` to `.env` and fill in values manually:

| Variable | Required | Description |
|----------|----------|-------------|
| `RESOURCE_GROUP` | Yes | Azure resource group containing the Neo4j marketplace deployment |
| `VMSS_NAME` | No | Discovered automatically by prefix `vmss-neo4j-` |
| `VNET_NAME` | No | Discovered from the VMSS network profile |
| `NEO4J_SUBNET_NAME` | No | Defaults to `subnet` (marketplace default) |
| `PLS_SUBNET_PREFIX` | No | Defaults to `10.1.0.0/24` |
| `NEO4J_URI` | No | Discovered automatically from public LB |
| `NEO4J_USER` | No | Defaults to `neo4j` |
| `NEO4J_PASSWORD` | No | Neo4j password (used by `--init` setup) |
| `DATABRICKS_ACCOUNT_ID` | No | Pre-fills the NCC API curl command in setup output |
| `NCC_ID` | No | NCC UUID; copy from the browser URL when viewing the NCC in the account console |
| `NEO4J_DOMAIN` | No | Domain name for the NCC private endpoint rule |
| `DATABRICKS_WORKSPACE_ID` | No | Workspace ID for `attach-ncc` (prompted if not set) |
| `DATABRICKS_ACCOUNT_TOKEN` | No | Account admin token (prompted if not set) |

Only `RESOURCE_GROUP` is required. Everything else is discovered automatically or has defaults.

## Testing Step by Step

### Step 1: Initialize Environment

```bash
# Interactive setup: asks for resource group and Neo4j password,
# discovers VMSS, VNet, subnet, region, and Neo4j URI automatically
uv run setup-private-link --init
```

This creates `.env` with all discovered values. Verify prerequisites are met:

```bash
# Confirm Bicep is available
az bicep version
```

### Step 2: Deploy Private Link Infrastructure

```bash
uv run setup-private-link
```

The script runs four phases:

1. **Discovery**: finds the VMSS, VNet, subnet, subscription, and region
2. **Bicep deployment**: creates the NAT subnet, internal load balancer, and Private Link Service
3. **VMSS update**: adds the VMSS to the internal LB's backend pool
4. **Output**: prints the Private Link Service resource ID and next steps

Verify the resources were created:

```bash
uv run verify-private-link
```

### Step 3: Create NCC Private Endpoint Rule

Use the curl command printed by the setup script, or create the rule in the Databricks account console:

1. Go to [accounts.azuredatabricks.net](https://accounts.azuredatabricks.net/)
2. Navigate to **Cloud resources** > **Network connectivity configurations**
3. Select your NCC (must be in the same region as the Neo4j cluster)
4. Under **Private endpoint rules**, click **Add private endpoint rule**
5. Paste the Private Link Service resource ID from the setup output
6. Add the domain name (e.g., `neo4j-ee.private.neo4j.com`)
7. Click **Add**

### Step 4: Approve the Connection

```bash
uv run approve-private-link
```

The script finds pending private endpoint connections on `neo4j-pls` and approves them. This replaces the manual Azure portal approval step.

Wait for the NCC status in Databricks to change to **ESTABLISHED** (up to 10 minutes).

**Note:** The "You don't have access" error in the Azure portal when clicking the private endpoint link is expected. That link points to the Databricks-managed subscription in Microsoft's tenant. You don't need access there; the approval happens on your PLS, which this script handles.

### Step 5: Attach NCC to Workspace

```bash
uv run attach-ncc --profile azure-account-admin
```

The script prompts for your workspace ID and attaches the NCC via the Databricks Account API. Uses the Databricks CLI profile for authentication. Requires `DATABRICKS_ACCOUNT_ID` and `NCC_ID` in `.env`.

If you haven't set up a profile, see [Databricks CLI Profile Setup](#databricks-cli-profile-setup) in Prerequisites.

It may take a few minutes for serverless compute to pick up the NCC.

### Step 6: Test from Databricks

First, store the Neo4j password in a Databricks secret scope (reads from `.env`):

```bash
./setup-secrets.sh <databricks-cli-profile>
```

Then import `neo4j_private_link_test.ipynb` into your Databricks workspace and run it on **serverless** compute. The notebook reads the password from the secret scope and runs TCP and driver connectivity tests.

Uses `neo4j://` (not `neo4j+s://`) because traffic travels over the Azure backbone via Private Link. TLS between driver and server is optional when the network path is private.

### Step 7: Verify Idempotency (Optional)

Re-running the setup script is safe. It skips the VMSS update if the backend pool is already attached, and Bicep handles infrastructure idempotency:

```bash
uv run setup-private-link
# Should see: "VMSS ... already in internal LB backend pool. Skipping."
```

### Step 8: Teardown

```bash
uv run teardown-private-link
```

Removes all Private Link resources in dependency order:
1. VMSS removed from internal LB backend pool
2. Private endpoint connections deleted (active connections block PLS deletion)
3. Private Link Service deleted
4. Internal Load Balancer deleted
5. NAT subnet deleted

The Neo4j marketplace deployment is unchanged.

Verify cleanup:

```bash
uv run verify-private-link --cleanup
```

### Step 9: Detach NCC from Databricks

```bash
uv run detach-ncc --profile azure-account-admin
```

Removes the NCC and its private endpoint rules from the Databricks side. The script:

1. Fetches the workspace to confirm which NCC is attached and determine the region
2. Creates an empty placeholder NCC in the same region and swaps the workspace to use it (the Databricks API does not support unsetting the NCC, so a swap is required)
3. Deletes all private endpoint rules from the original NCC
4. Deletes the original NCC

The placeholder NCC (`neo4j-ncc-placeholder`) is left attached to the workspace. It has no rules and does not affect connectivity. You can leave it in place or remove it from the account console.

**Note:** If the original private endpoint rule was in `ESTABLISHED`, `REJECTED`, or `DISCONNECTED` state, Databricks may retain the private endpoint on your Azure resource for up to 7 days before permanently removing it.

## Troubleshooting

**"No Neo4j VMSS found in resource group"**
The setup script looks for a VMSS with name prefix `vmss-neo4j-`. Verify the VMSS exists with `az vmss list --resource-group <RG>`. If the name doesn't match the prefix, set `VMSS_NAME` in `.env`.

**Bicep deployment fails with subnet conflict**
If the NAT subnet `10.1.0.0/24` overlaps with an existing subnet, change `PLS_SUBNET_PREFIX` in `.env`.

**NCC status stays PENDING**
The connection must be approved (Step 4). Run `uv run approve-private-link` to find and approve pending connections.

**NCC status is REJECTED**
The connection was denied instead of approved. Delete the NCC rule, re-create it, and approve the new connection.

**Connection timeout from Databricks notebook**
- Verify NCC status is **ESTABLISHED** (not PENDING or REJECTED)
- Verify the domain name in the NCC rule matches the URI in the notebook
- Verify the NCC is attached to the workspace
- Wait 10 minutes after attaching/updating the NCC, then restart serverless compute

**"You don't have access" in Azure portal**
When clicking the private endpoint link on the PLS connections page, Azure navigates to the Databricks-managed subscription in Microsoft's tenant. This is expected. You don't need access there. Use `uv run approve-private-link` to approve from your side.

## Architecture

### Network Path

```
Databricks serverless notebook
    │
    │ neo4j://neo4j-ee.private.neo4j.com:7687
    │
    ▼
NCC Private Endpoint (Databricks-managed subscription)
    │
    │ Azure Private Link (Azure backbone, no public internet)
    │
    ▼
Private Link Service: neo4j-pls (customer subscription)
    │
    │ NAT via pls-nat-subnet (10.1.0.0/24)
    │
    ▼
Internal Load Balancer: neo4j-internal-lb
    │
    │ Health probe: TCP 7687, 5s interval, threshold 2
    │ Load balancing: port 7687, TCP Reset enabled
    │
    ▼
Neo4j VMSS (3 instances)
    │
    │ Bolt protocol on port 7687
    ▼
```

### How the Pieces Fit Together

The **marketplace deployment** creates the VMSS, public LB, VNet (`10.0.0.0/8`), subnet (`10.0.0.0/16`), NSG, and public IP. The VMSS NIC uses `ipconfig-cluster` with NIC-based backend pool membership.

**This project** adds the internal LB, NAT subnet (`10.1.0.0/24`), and Private Link Service. The setup script adds the VMSS to the internal LB's backend pool via `az vmss update`. This is a live NIC configuration change that does not restart VMs or interrupt Neo4j (validated with 41 connection checks and zero drops).

The **Databricks NCC** creates a private endpoint in the Databricks-managed subscription that connects to the Private Link Service. The domain name in the NCC rule (`neo4j-ee.private.neo4j.com`) is resolved internally by Databricks to the private endpoint IP. It does not need to exist in public DNS.

### Marketplace Deployment Resources

The ARM template at `neo4j-partners/azure-resource-manager-neo4j` defines the naming conventions the automation discovers. Every resource uses a suffix derived from `uniqueString(resourceGroup().id, deployment().name)`. The VMSS is `vmss-neo4j-{location}-{suffix}`, the VNet `vnet-neo4j-{location}-{suffix}`, the load balancer `lb-neo4j-{location}-{suffix}`. The setup script discovers these by querying the resource group, or the operator can set `VMSS_NAME` in `.env`.

The VNet uses `10.0.0.0/8` with a single subnet at `10.0.0.0/16` named `subnet`. The Private Link NAT subnet fits at `10.1.0.0/24` without conflicts.

Each VMSS instance has one NIC named `nic` with one IP configuration named `ipconfig-cluster`. The IP configuration includes a public IP and, for 3+ node clusters, membership in the public load balancer's backend pool named `backend`. This NIC-based configuration is what Azure Private Link Service requires for its backend pool. The public load balancer is created only when `nodeCount >= 3` and uses Standard SKU with health probes on ports 7474 (HTTP) and 7687 (TCP/Bolt).

### VMSS Backend Pool Update

Adding the internal load balancer's backend pool to the existing VMSS requires `az vmss update` followed by `az vmss update-instances --instance-ids "*"`. The marketplace template sets the VMSS upgrade policy to `Manual`, so `update-instances` is required to propagate the change. This is a live NIC configuration update; Azure applies it without restarting the VM or disrupting the Neo4j process. Tested against a live 3-node cluster: 41 connection checks over the full update cycle, zero connection drops, average latency 714ms.

### Bicep Template

The `private-link.bicep` template accepts parameters describing the existing marketplace deployment and creates three resources.

**Parameters:**
- `vnetName` — the marketplace VNet (discovered by the setup script)
- `vnetResourceGroup` — resource group containing the VNet (usually the same group)
- `neo4jSubnetName` — existing subnet where Neo4j VMs run (default: `subnet`)
- `plsSubnetPrefix` — address prefix for the Private Link NAT subnet (default: `10.1.0.0/24`)
- `location` — Azure region (discovered from VMSS)

**Resources created:**

1. **Private Link NAT subnet** (`pls-nat-subnet`). A new subnet in the existing VNet with `privateLinkServiceNetworkPolicies` set to `Disabled`. Azure uses this subnet for NAT IP addresses when proxying traffic through the Private Link Service.

2. **Internal Standard Load Balancer** (`neo4j-internal-lb`). Standard SKU, internal (no public IP), frontend IP in the existing Neo4j subnet. Health probe: TCP on port 7687, 5-second interval, probe threshold of 2 (unhealthy after 10 seconds). Load balancing rule forwards port 7687 with TCP Reset enabled so the Neo4j driver detects closed connections immediately. Idle timeout uses the Azure default of 4 minutes; for production use with long-lived connection pools, increase this and ensure the Neo4j driver's `keep_alive=True` sends keepalives more frequently than the timeout.

3. **Private Link Service** (`neo4j-pls`). Attached to the internal load balancer's frontend IP, using the NAT subnet. Visibility set to `*` (all subscriptions can request connections), but auto-approval is disabled. Every connection requires explicit approval.

The template outputs the Private Link Service resource ID for use when creating the NCC private endpoint rule.

### Port Usage

The Neo4j port numbering matters because the wrong port behind Private Link means the connection fails silently. The internal load balancer forwards only port 7687; the health probe checks port 7687 via TCP.

| Port | Protocol | Path | Purpose |
|------|----------|------|---------|
| 7687 | Bolt | Private Link (internal LB) | Driver connectivity from Databricks |
| 7474 | HTTP | Public LB | Neo4j Browser / admin |
| 7473 | HTTPS | Public LB | Neo4j Browser over TLS |
| 7688 | Bolt Routing | Not exposed | Handled by internal LB |

Only port 7687 traverses Private Link. The public LB and Neo4j Browser remain accessible for cluster administration.

### Security Boundaries

- **Private Link Service visibility** is set to `*` (all subscriptions can request connections), but **auto-approval is disabled**. Every connection requires explicit approval via `uv run approve-private-link` or the Azure portal.
- **No public internet exposure.** Driver traffic from Databricks never leaves the Azure backbone.
- **Neo4j credentials** are stored in a Databricks secret scope, not in notebook code.
- **`neo4j://` (not `neo4j+s://`)** is used because the network path is private. TLS between driver and server is optional when traffic doesn't traverse the public internet.

### Reusability

The Bicep template is parameterized against the marketplace deployment, not coupled to it. It needs a VNet name, a subnet name, and a region. Those could come from any deployment that puts Neo4j VMs behind a NIC-based VMSS in an Azure VNet: marketplace, custom ARM template, or manual VM deployment. The Python scripts add marketplace-specific discovery logic, but the Bicep template stands alone.

## File Structure

```
private-link-ee/
  README.md                      # This file
  NCC_EE.md                      # Architecture and manual steps
  private-link.bicep             # Bicep template (internal LB, NAT subnet, PLS)
  pyproject.toml                 # Package definition and script entry points
  src/neo4j_private_link/        # Python package
    helpers.py                   # Shared module: az wrapper, discovery, env loading
    setup.py                     # Setup: discover, deploy Bicep, wire VMSS
    approve.py                   # Approve pending private endpoint connections
    attach_ncc.py                # Attach NCC to a Databricks workspace
    detach_ncc.py                # Detach NCC from workspace and delete it
    verify.py                    # Verify resources exist or cleanup is complete
    teardown.py                  # Teardown: remove PLS, LB, subnet
  setup-secrets.sh               # Store Neo4j credentials in Databricks secrets
  neo4j_private_link_test.ipynb  # Test notebook for Databricks serverless
  .env.sample                    # Example configuration
```

## References

- [Configure private connectivity to resources in your VNet](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/pl-to-internal-network): Databricks NCC private endpoints to load balancer-backed resources
- [What is Azure Private Link Service?](https://learn.microsoft.com/en-us/azure/private-link/private-link-service-overview): architecture and requirements
- [Neo4j Azure Resource Manager Templates](https://github.com/neo4j-partners/azure-resource-manager-neo4j): official marketplace ARM templates
