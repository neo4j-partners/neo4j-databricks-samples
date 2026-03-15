# Neo4j EE Private Link for Databricks Serverless

Private connectivity from Databricks serverless compute to a self-hosted Neo4j Enterprise Edition cluster on Azure, using Azure Private Link. No public internet, no IP allowlisting.

```
Databricks Serverless --> NCC Private Endpoint --> Private Link Service --> Internal Load Balancer --> Neo4j EE VMs
```

## Prerequisites

- **Azure CLI** with Bicep support (`az bicep version` — [install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli))
- **uv** ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **Databricks CLI** ([install](https://docs.databricks.com/dev-tools/cli/install.html)) — used by `attach-ncc.py` for authentication
- **Neo4j EE marketplace deployment** — a running 3-node cluster deployed from the [Azure Marketplace](https://azuremarketplace.microsoft.com/en-us/marketplace/apps/neo4j.neo4j-ee)
- **Databricks workspace** in the same Azure region as the Neo4j cluster
- **Azure permissions** — Contributor on the resource group containing the Neo4j deployment
- **Databricks permissions** — Account admin (to create NCC private endpoint rules and attach to workspaces)

### Databricks CLI Profile Setup

The `attach-ncc.py` script uses a Databricks CLI profile to authenticate with the account console. Set up a profile in `~/.databrickscfg`:

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

# 1. Interactive setup — asks for resource group and Neo4j password,
#    discovers everything else, writes .env
uv run setup-private-link.py --init

# 2. Deploy Private Link infrastructure
uv run setup-private-link.py

# 3. Create NCC private endpoint rule in Databricks (see setup output)

# 4. Approve the pending connection
uv run approve-private-link.py

# 5. Attach the NCC to your workspace
uv run attach-ncc.py --profile azure-account-admin

# 6. Wait for NCC status to show ESTABLISHED, then test from a notebook

# 7. When done, tear down
uv run teardown-private-link.py
```

## Configuration

Run `uv run setup-private-link.py --init` to create `.env` interactively, or copy `.env.sample` to `.env` and fill in values manually:

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
| `NCC_ID` | No | NCC UUID — copy from the browser URL when viewing the NCC in the account console |
| `NEO4J_DOMAIN` | No | Domain name for the NCC private endpoint rule |
| `DATABRICKS_WORKSPACE_ID` | No | Workspace ID for `attach-ncc.py` (prompted if not set) |
| `DATABRICKS_ACCOUNT_TOKEN` | No | Account admin token (prompted if not set) |

Only `RESOURCE_GROUP` is required. Everything else is discovered automatically or has defaults.

## Testing Step by Step

### Step 1: Initialize Environment

```bash
# Interactive setup — asks for resource group and Neo4j password,
# discovers VMSS, VNet, subnet, region, and Neo4j URI automatically
uv run setup-private-link.py --init
```

This creates `.env` with all discovered values. Verify prerequisites are met:

```bash
# Confirm Bicep is available
az bicep version
```

### Step 2: Deploy Private Link Infrastructure

```bash
uv run setup-private-link.py
```

The script runs four phases:

1. **Discovery** — finds the VMSS, VNet, subnet, subscription, and region
2. **Bicep deployment** — creates the NAT subnet, internal load balancer, and Private Link Service
3. **VMSS update** — adds the VMSS to the internal LB's backend pool
4. **Output** — prints the Private Link Service resource ID and next steps

**Verify the Azure resources were created:**

```bash
# Private Link Service
az network private-link-service show \
  --resource-group <your-resource-group> \
  --name neo4j-pls \
  --query "{name:name, provisioningState:provisioningState}" -o table

# Internal Load Balancer
az network lb show \
  --resource-group <your-resource-group> \
  --name neo4j-internal-lb \
  --query "{name:name, sku:sku.name, provisioningState:provisioningState}" -o table

# NAT Subnet
az network vnet subnet show \
  --resource-group <your-resource-group> \
  --vnet-name <your-vnet-name> \
  --name pls-nat-subnet \
  --query "{name:name, addressPrefix:addressPrefix, privateLinkServiceNetworkPolicies:privateLinkServiceNetworkPolicies}" -o table

# VMSS backend pool membership (should show 2 pools: public LB + internal LB)
az vmss show \
  --resource-group <your-resource-group> \
  --name <your-vmss-name> \
  --query "virtualMachineProfile.networkProfile.networkInterfaceConfigurations[0].ipConfigurations[0].loadBalancerBackendAddressPools[].id" -o tsv
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
uv run approve-private-link.py
```

The script finds pending private endpoint connections on `neo4j-pls` and approves them. This replaces the manual Azure portal approval step.

Wait for the NCC status in Databricks to change to **ESTABLISHED** (up to 10 minutes).

**Note:** The "You don't have access" error in the Azure portal when clicking the private endpoint link is expected — that link points to the Databricks-managed subscription. You don't need access there; the approval happens on your PLS, which this script handles.

### Step 5: Attach NCC to Workspace

```bash
uv run attach-ncc.py --profile azure-account-admin
```

The script prompts for your workspace ID and attaches the NCC via the Databricks Account API. Uses the Databricks CLI profile for authentication. Requires `DATABRICKS_ACCOUNT_ID` and `NCC_ID` in `.env`.

If you haven't set up a profile, see [Databricks CLI Profile Setup](#databricks-cli-profile-setup) in Prerequisites.

It may take a few minutes for serverless compute to pick up the NCC.

### Step 6: Test from Databricks

In a Databricks **serverless** notebook:

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

Use `neo4j://` (not `neo4j+s://`) — traffic travels over the Azure backbone via Private Link, so TLS is optional.

### Step 7: Verify Idempotency (Optional)

Re-running the setup script should be safe — it skips the VMSS update if the backend pool is already attached, and Bicep handles infrastructure idempotency:

```bash
uv run setup-private-link.py
# Should see: "VMSS ... already in internal LB backend pool. Skipping."
```

### Step 8: Teardown

```bash
uv run teardown-private-link.py
```

Removes all three Private Link resources in dependency order:
1. VMSS removed from internal LB backend pool
2. Private Link Service deleted
3. Internal Load Balancer deleted
4. NAT subnet deleted

The Neo4j marketplace deployment is unchanged.

**Verify cleanup:**

```bash
# All three should return "not found" errors
az network private-link-service show --resource-group <your-resource-group> --name neo4j-pls 2>&1 | head -1
az network lb show --resource-group <your-resource-group> --name neo4j-internal-lb 2>&1 | head -1
az network vnet subnet show --resource-group <your-resource-group> --vnet-name <your-vnet-name> --name pls-nat-subnet 2>&1 | head -1

# VMSS should have only 1 backend pool (the public LB)
az vmss show --resource-group <your-resource-group> --name <your-vmss-name> \
  --query "length(virtualMachineProfile.networkProfile.networkInterfaceConfigurations[0].ipConfigurations[0].loadBalancerBackendAddressPools)" -o tsv
# Expected: 1
```

**Note:** The NCC private endpoint rule in Databricks is not removed by the teardown script. Delete it manually in the Databricks account console if no longer needed.

## Troubleshooting

**"No Neo4j VMSS found in resource group"**
The setup script looks for a VMSS with name prefix `vmss-neo4j-`. Verify the VMSS exists with `az vmss list --resource-group <RG>`. If the name doesn't match the prefix, set `VMSS_NAME` in `.env`.

**Bicep deployment fails with subnet conflict**
If the NAT subnet `10.1.0.0/24` overlaps with an existing subnet, change `PLS_SUBNET_PREFIX` in `.env`.

**NCC status stays PENDING**
The connection must be approved (Step 4). Run `uv run approve-private-link.py` to find and approve pending connections.

**NCC status is REJECTED**
The connection was denied instead of approved. Delete the NCC rule, re-create it, and approve the new connection.

**Connection timeout from Databricks notebook**
- Verify NCC status is **ESTABLISHED** (not PENDING or REJECTED)
- Verify the domain name in the NCC rule matches the URI in the notebook
- Verify the NCC is attached to the workspace
- Wait 10 minutes after attaching/updating the NCC, then restart serverless compute

**Teardown fails on subnet deletion**
The NAT subnet can't be deleted while the Private Link Service exists. Ensure Step 3 (PLS deletion) succeeded before Step 5 (subnet deletion). If the PLS deletion timed out, delete it manually: `az network private-link-service delete --resource-group <RG> --name neo4j-pls`

## File Structure

```
private-link-ee/
  README.md                    # This file
  NCC_EE.md                    # Architecture and manual steps
  PRIVATE_LINK.md              # Design proposal and progress log
  private-link.bicep           # Bicep template (internal LB, NAT subnet, PLS)
  private_link_helpers.py      # Shared module: az wrapper, discovery, env loading
  setup-private-link.py        # Setup script: discover, deploy, wire VMSS
  approve-private-link.py      # Approve pending private endpoint connections
  attach-ncc.py                # Attach NCC to a Databricks workspace
  teardown-private-link.py     # Teardown script: remove PLS, LB, subnet
  .env.sample                  # Example configuration
```

## References

- [Configure private connectivity to resources in your VNet](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/pl-to-internal-network) — Databricks NCC private endpoints to load balancer-backed resources
- [What is Azure Private Link Service?](https://learn.microsoft.com/en-us/azure/private-link/private-link-service-overview) — architecture and requirements
- [Neo4j Azure Resource Manager Templates](https://github.com/neo4j-partners/azure-resource-manager-neo4j) — official marketplace ARM templates
