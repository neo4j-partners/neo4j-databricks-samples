# Connecting Databricks Serverless to Neo4j Enterprise Edition via Azure Private Link

## The Problem

Databricks serverless compute runs inside Databricks-managed infrastructure. Your code executes there, but you don't control the VNet, the outbound IPs, or the routing. Neo4j Enterprise Edition runs on VMs you do control, inside your own Azure VNet. The two networks have no path between them.

The existing NCC_POC.md covers five connectivity patterns, but all target Neo4j Aura (the managed service). None address Neo4j Enterprise Edition deployed on Azure VMs via the official ARM templates. This proposal covers that gap: a private, stable, zero-IP-management connection from Databricks serverless compute to self-hosted Neo4j EE.

## The Architecture

Databricks NCC supports private endpoints to any resource behind an Azure Standard Load Balancer. This is the mechanism. The architecture chains four components:

```
Databricks Serverless → NCC Private Endpoint → Private Link Service → Internal Load Balancer → Neo4j EE VMs
```

Traffic flows entirely over the Azure backbone. No public internet, no IP allowlisting, no CIDR blocks to monitor weekly. The Private Link Service acts as a doorway into your VNet; the Internal Load Balancer distributes connections across your Neo4j cluster; and the NCC private endpoint gives Databricks serverless compute a private path to that doorway.

## Why This Works

Databricks explicitly supports NCC private endpoints to "resources behind a Standard Load Balancer." The official documentation at [Configure private connectivity to resources in your VNet](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/pl-to-internal-network) provides a step-by-step guide for exactly this scenario: creating a Standard Load Balancer, placing a Private Link Service in front of it, and connecting Databricks NCC to the Private Link Service.

Azure Private Link Service has one core requirement: a Standard SKU Load Balancer with a NIC-based backend pool. The Neo4j Enterprise ARM templates deploy VMs through a Virtual Machine Scale Set with a Standard Load Balancer for clusters of three or more nodes. The pieces are already in place; the gap is that the ARM template creates a public load balancer with public IP addresses, and it does not create a Private Link Service. Both gaps are straightforward to close.

## What the ARM Templates Deploy Today

The [Neo4j Enterprise ARM templates](https://github.com/neo4j-partners/azure-resource-manager-neo4j) deploy the following networking resources:

- A Virtual Network (10.0.0.0/8) with a single subnet (10.0.0.0/16)
- A Network Security Group with rules allowing inbound SSH (22), HTTP (7474), HTTPS (7473), Bolt (7687), and Bolt Routing (7688) from the internet
- Per-instance public IPs on the VMSS
- For clusters of 3+ nodes: a Standard Public Load Balancer with health probes on ports 7474 and 7687, and load balancing rules forwarding those ports to the backend pool

The templates contain no Private Link Service, no Private Endpoint configuration, and no parameters for private networking. Every deployment is internet-facing by default.

## Post-Deployment: Bridging Neo4j to Databricks

After deploying Neo4j EE from the Azure Marketplace, the VMs are running inside a VNet but have no private connectivity path to Databricks. The following steps create that path using standalone Azure resources deployed separately from the marketplace template. Nothing in the marketplace deployment is modified; these resources reference the existing VMSS and VNet.

### 1. A Subnet for Private Link NAT

Private Link Service requires its own subnet for NAT IP addresses. This subnet must be separate from the one your Neo4j VMs use. Create it in the same VNet:

```bash
az network vnet subnet create \
  --resource-group <RESOURCE_GROUP> \
  --vnet-name <VNET_NAME> \
  --name pls-nat-subnet \
  --address-prefixes 10.1.0.0/24
```

Disable Private Link network policies on this subnet:

```bash
az network vnet subnet update \
  --resource-group <RESOURCE_GROUP> \
  --vnet-name <VNET_NAME> \
  --name pls-nat-subnet \
  --disable-private-link-service-network-policies true
```

Microsoft recommends at least 8 NAT IP addresses available in this subnet. A /24 provides far more than enough.

### 2. An Internal Standard Load Balancer

The ARM template's public load balancer exposes Neo4j to the internet. For Private Link, create a separate internal load balancer that keeps traffic on the Azure backbone:

```bash
az network lb create \
  --resource-group <RESOURCE_GROUP> \
  --name neo4j-internal-lb \
  --sku Standard \
  --vnet-name <VNET_NAME> \
  --subnet <NEO4J_SUBNET_NAME> \
  --frontend-ip-name neo4j-frontend \
  --backend-pool-name neo4j-backend
```

Add a health probe for the Bolt protocol port:

```bash
az network lb probe create \
  --resource-group <RESOURCE_GROUP> \
  --lb-name neo4j-internal-lb \
  --name neo4j-bolt-probe \
  --protocol Tcp \
  --port 7687 \
  --interval 5
```

Add a load balancing rule to route Bolt traffic:

```bash
az network lb rule create \
  --resource-group <RESOURCE_GROUP> \
  --lb-name neo4j-internal-lb \
  --name neo4j-bolt-rule \
  --protocol Tcp \
  --frontend-port 7687 \
  --backend-port 7687 \
  --frontend-ip-name neo4j-frontend \
  --backend-pool-name neo4j-backend \
  --probe-name neo4j-bolt-probe \
  --enable-tcp-reset true
```

The idle timeout defaults to 4 minutes (configurable from 4 to 100 minutes), which is sufficient for short-lived demo queries. The Neo4j Python driver enables TCP keepalives by default, which prevents idle connections from being dropped. For production workloads with long-lived connection pools, add `--idle-timeout <minutes>`. TCP Reset is enabled so the driver receives an immediate RST when a connection is dropped rather than waiting for a TCP retransmission timeout.

If you also need HTTP access (port 7474) for the Neo4j Browser or health checks from Databricks, add a second probe and rule for that port.

### 3. Backend Pool Membership

Add your Neo4j VMSS instances to the internal load balancer's backend pool. The backend pool must use NIC-based configuration, not IP-based. This is a Private Link Service requirement.

```bash
az vmss update \
  --resource-group <RESOURCE_GROUP> \
  --name <VMSS_NAME> \
  --add virtualMachineProfile.networkProfile.networkInterfaceConfigurations[0].ipConfigurations[0].loadBalancerBackendAddressPools \
    id="/subscriptions/<SUB_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Network/loadBalancers/neo4j-internal-lb/backendAddressPools/neo4j-backend"
```

After updating the VMSS configuration, update the running instances:

```bash
az vmss update-instances \
  --resource-group <RESOURCE_GROUP> \
  --name <VMSS_NAME> \
  --instance-ids "*"
```

### 4. A Private Link Service

The Private Link Service sits in front of the internal load balancer and makes it reachable via private endpoints from other VNets, including the Databricks-managed VNet:

```bash
az network private-link-service create \
  --resource-group <RESOURCE_GROUP> \
  --name neo4j-pls \
  --vnet-name <VNET_NAME> \
  --subnet pls-nat-subnet \
  --lb-name neo4j-internal-lb \
  --lb-frontend-ip-configs neo4j-frontend \
  --location <REGION>
```

Note the resource ID from the output. It follows this format:

```
/subscriptions/<SUB_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Network/privateLinkServices/neo4j-pls
```

### 5. An NCC Private Endpoint Rule in Databricks

Create a private endpoint rule in your NCC that points to the Private Link Service. This step can be done through the Databricks account console UI or the REST API:

**Via Account Console UI:**

1. Go to [accounts.azuredatabricks.net](https://accounts.azuredatabricks.net/)
2. Navigate to **Security** > **Network connectivity configurations**
3. Select your NCC (or create one in the same region as your workspace)
4. Under **Private endpoint rules**, click **Add private endpoint rule**
5. Paste the Private Link Service resource ID
6. Add domain names that will resolve to your Neo4j cluster (up to 10)
7. Click **Add**

**Via REST API:**

```bash
curl --location 'https://accounts.azuredatabricks.net/api/2.0/accounts/<ACCOUNT_ID>/network-connectivity-configs/<NCC_ID>/private-endpoint-rules' \
  --header 'Content-Type: application/json' \
  --header 'Authorization: Bearer <TOKEN>' \
  --data '{
    "domain_names": [
      "neo4j-ee.internal.yourdomain.com"
    ],
    "resource_id": "/subscriptions/<SUB_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Network/privateLinkServices/neo4j-pls",
    "group_id": "neo4j-pls"
  }'
```

### 6. Approve the Private Endpoint Connection

Once the NCC rule is created, Databricks sends a private endpoint request to your Private Link Service. Approve it:

1. In the Azure portal, go to **Private Link Center** > **Private Link services**
2. Select `neo4j-pls`
3. Under **Settings** > **Private endpoint connections**, find the pending connection
4. Select it and click **Approve**
5. Return to the NCC page in Databricks and confirm the status changes to `ESTABLISHED`

This can take up to 10 minutes.

### 7. Attach the NCC to Your Workspace

If not already attached:

1. In the Databricks account console, click **Workspaces**
2. Select your workspace > **Update workspace**
3. Under **Network connectivity configurations**, select your NCC
4. Click **Update**
5. Wait 10 minutes for propagation
6. Restart any running serverless compute

## Testing the Connection

From a Databricks serverless notebook, install the Neo4j Python driver and test:

```python
from neo4j import GraphDatabase

uri = "neo4j://neo4j-ee.internal.yourdomain.com:7687"
auth = ("<USERNAME>", "<PASSWORD>")

with GraphDatabase.driver(uri, auth=auth) as driver:
    driver.verify_connectivity()
    records, _, _ = driver.execute_query("RETURN 1 AS test")
    print(f"Connection successful: {records[0]['test']}")
```

Note the `neo4j://` scheme rather than `neo4j+s://`. Since traffic travels over the Azure backbone via Private Link, TLS encryption between the driver and server is optional. If your Neo4j deployment is configured with TLS certificates, use `neo4j+s://` instead.

## DNS Considerations

Domain names added to the NCC private endpoint rule are implicitly allowlisted in Databricks network policies. However, DNS chasing and DNS redirect are not supported. Every domain name in the rule must resolve directly to the backend resource.

For a self-hosted deployment, you control the DNS. The simplest approach is to use a custom domain name (like `neo4j-ee.internal.yourdomain.com`) that resolves to the internal load balancer's frontend IP within your VNet. The NCC private endpoint rule maps that same domain name to the private endpoint, so Databricks serverless compute resolves it to the Private Link connection.

## Constraints and Limitations

**Idle timeout.** Azure Private Link drops TCP connections after approximately 5 minutes of inactivity. The Neo4j driver must send TCP keepalives at intervals shorter than 300 seconds. Configure this in the driver:

```python
from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    uri,
    auth=auth,
    keep_alive=True  # Enabled by default in the Python driver
)
```

**NIC-based backend pools only.** The internal load balancer's backend pool must use NIC-based configuration. IP-based backend pools are not supported by Azure Private Link Service. The VMSS approach used by the ARM templates naturally uses NIC-based pools, so this is satisfied by default.

**Single region.** The NCC, workspace, load balancer, Private Link Service, and Neo4j VMs must all be in the same Azure region. Cross-region Private Link requires additional networking (Azure Private Link with global peering or a multi-region load balancer), which NCC does not support.

**NCC limits.** Each Databricks account supports up to 10 NCCs per region with a combined maximum of 100 private endpoints. Each NCC can serve up to 50 workspaces. Domain names per private endpoint rule are capped at 10.

**Cost.** The internal load balancer, Private Link Service, and NCC private endpoint each carry Azure charges. Databricks also charges for networking costs when serverless workloads connect to customer resources via NCC. These costs are incremental to the Neo4j VM costs from the ARM deployment.

**No changes to the ARM template.** This proposal layers the Private Link infrastructure on top of the existing ARM deployment. It does not modify the ARM templates themselves. If Neo4j needs to be redeployed via the ARM template, the load balancer, Private Link Service, and NCC rule persist independently and only the backend pool membership needs to be re-established.

## How This Compares to Other Approaches

| Approach | Neo4j Tier | Public Internet | IP Maintenance | Complexity |
|----------|-----------|----------------|----------------|------------|
| This proposal (NCC + Private Link to EE) | Enterprise Edition (self-hosted) | No | None | Medium |
| NCC + Aura VDC Private Link (NCC_POC Part 4) | Aura VDC (managed) | No | None | Medium |
| IP Allowlisting (NCC_POC Part 1) | Any | Yes | Static, but fragile | Low |
| Service Tag CIDR extraction (NCC_POC Part 2) | Any | Yes | Weekly monitoring | Medium |
| NCC + App Gateway v2 (NCC_POC Part 5) | Any | No | None | High |

The closest analog is Part 4 of NCC_POC (Aura VDC Private Link), but that requires the Aura Enterprise/VDC tier. This proposal achieves the same private connectivity pattern with self-hosted Neo4j EE on Azure VMs, giving you full control over the database configuration, version, plugins, and infrastructure.

## References

### Databricks NCC and Private Link
- [Configure private connectivity to resources in your VNet](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/pl-to-internal-network) — the primary guide for NCC private endpoints to load balancer-backed resources
- [Manage private endpoint rules](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/manage-private-endpoint-rules) — supported resource types, including Standard Load Balancer
- [Configure private connectivity to Azure resources](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/serverless-private-link) — NCC requirements, limits, and private endpoint statuses
- [Serverless compute plane networking overview](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/) — how serverless networking works
- [Network Connectivity Configurations API](https://docs.databricks.com/api/azure/account/networkconnectivity) — REST API reference

### Azure Private Link Service
- [What is Azure Private Link Service?](https://learn.microsoft.com/en-us/azure/private-link/private-link-service-overview) — architecture, requirements, and limitations
- [Create a Private Link service (Azure portal)](https://learn.microsoft.com/en-us/azure/private-link/create-private-link-service-portal) — portal walkthrough
- [Create a Private Link service (Azure CLI)](https://learn.microsoft.com/en-us/azure/private-link/create-private-link-service-cli) — CLI walkthrough

### Azure Load Balancer
- [What is Azure Load Balancer?](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-overview) — Standard vs Basic SKU differences
- [Quickstart: Create an internal load balancer](https://learn.microsoft.com/en-us/azure/load-balancer/quickstart-load-balancer-standard-internal-portal) — step-by-step for internal Standard Load Balancer

### Neo4j Enterprise ARM Templates
- [Neo4j Azure Resource Manager Templates](https://github.com/neo4j-partners/azure-resource-manager-neo4j) — official ARM templates for Neo4j Enterprise and Community editions on Azure
