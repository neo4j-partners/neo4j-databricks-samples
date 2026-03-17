# Connecting Databricks Serverless to Neo4j over Private Link

The goal of this project was to demonstrate secure connectivity from Databricks serverless compute to Neo4j on Azure using one of two approaches: Azure Private Link or IP-based filtering. Databricks serverless compute runs in Databricks-managed infrastructure with no customer-controlled VNet, which means there is no direct network path to resources in a customer's Azure environment. Any connection to Neo4j must either traverse the public internet (with IP-based filtering) or use Azure Private Link to route traffic over the Azure backbone.

Based on our research into Neo4j and Databricks connectivity on Azure, Private Link is the best solution.

## Private Link Solution

We established Private Link connectivity from Databricks serverless to a Neo4j Enterprise Edition cluster on Azure. A Bicep template and Python scripts automate the entire setup: an internal load balancer, a NAT subnet, and a Private Link Service layered on top of an existing Neo4j marketplace deployment. Traffic from a Databricks serverless notebook reaches Neo4j over the Bolt protocol (port 7687), routed entirely over the Azure backbone. No public internet exposure, no IP allowlisting.

The setup was validated against a live 3-node cluster with zero connection drops across 41 connectivity checks. The full workflow (deploy infrastructure, create NCC rule, approve connection, test from notebook, tear down) runs end to end. See the [README](README.md) for the complete setup guide.

The target deployment for this work is Neo4j Aura VDC on Azure. The only Aura VDC instance we had immediately available was on GCP, so to approximate the architecture on Azure we used a Neo4j Enterprise Edition marketplace deployment instead. Both deployments place Neo4j behind a VMSS in a customer-managed VNet, and both require a Private Link Service backed by an internal load balancer to expose the database to Databricks serverless. The Private Link setup for Aura VDC on Azure will follow nearly the same pattern, with the primary difference being that Aura VDC manages some of the infrastructure (like the Private Link Service) on behalf of the customer.

## IP Filtering: No Stable Path

IP-based filtering would avoid the Private Link infrastructure by allowlisting Databricks' outbound IPs on the Neo4j firewall. Our understanding is that Databricks serverless compute uses a wide range of shared outbound IP addresses that can change over time, making it impractical to maintain a stable allowlist. Every path we explored to obtain stable outbound IPs is either unavailable or, to our understanding, being deprecated.

- **[Service Tag CIDR Extraction](https://learn.microsoft.com/en-us/azure/virtual-network/service-tags-overview).** Databricks serverless outbound IPs come from the [`AzureDatabricksServerless` service tag](https://learn.microsoft.com/en-us/azure/databricks/resources/ip-domain-region), but these IPs are managed by Databricks and are not stable by default. The CIDR ranges contain too many possible IPs to maintain as a firewall allowlist. Not a viable option.

- **[Serverless Compute Firewall (legacy)](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/serverless-firewall).** The NCC-based firewall feature that allows adding Databricks-managed subnet IDs to Azure resource firewalls. This feature has reached end-of-life and will no longer be available after April 7, 2026. It is being replaced by Network Security Perimeter (NSP), which does not help with Neo4j connectivity (see below). The legacy static outbound IP lists shared through the Private Preview are being removed after May 25, 2026.

- **[Stable NAT IPs for Non-Storage Resources](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/serverless-firewall).** Databricks can provide stable NAT IPs specifically for non-storage use cases like connecting to Neo4j. The legacy firewall documentation still directs customers to contact their account team for stable NAT IPs, but the legacy static IP lists are being removed after May 25, 2026. This option is in transition and not a reliable long-term path.

- **[NCC with Network Security Perimeter (NSP)](https://learn.microsoft.com/en-us/azure/databricks/security/network/serverless-network-security/serverless-nsp-firewall).** NSP is the replacement for the legacy NCC firewall and uses the `AzureDatabricksServerless` service tag to simplify access rules. However, NSP only supports [Azure-native PaaS resources](https://learn.microsoft.com/en-us/azure/private-link/network-security-perimeter-concepts) (Storage, SQL Database, Key Vault, Cosmos DB, Event Hubs, etc.) and does not extend to third-party services. Neo4j Aura is not an Azure-native resource, so NSP cannot be used to secure connectivity to it.

## Application Gateway as an Alternative to Private Link

Aura Business Critical uses IP allowlisting rather than Private Link (Private Link is an Aura VDC feature). Without Private Link, it is reasonable to consider Azure Application Gateway as a network intermediary. The idea would look something like this:

```
Databricks serverless
    │
    │ NCC Private Endpoint → App Gateway Private Link
    ▼
Azure Application Gateway (static public IP, in a customer VNet)
    │
    │ Outbound to Aura BC, public IP allowlisted
    ▼
Neo4j Aura Business Critical
```

The primary challenge with this approach is that it has not been tested, and even if it could be made to work, it could be problematic in production because of how connection routing works. Application Gateway v2 supports TCP natively via its [Layer 4 proxy](https://learn.microsoft.com/en-us/azure/application-gateway/tcp-tls-proxy-overview), so Bolt traffic on port 7687 is a supported configuration. However, there are open questions about how the gateway interacts with the Neo4j driver that the documentation alone cannot answer. This would need to be prototyped and validated before any production use.

The core ambiguity is connection behavior at Layer 4. It is possible that the gateway maintains a persistent 1:1 connection pair (one frontend TCP connection mapped to one backend TCP connection) for the lifetime of the session, which is how a Layer 4 proxy would logically operate. However, the [Application Gateway FAQ](https://learn.microsoft.com/en-us/azure/application-gateway/application-gateway-faq) states under its TCP/TLS proxy section: "It doesn't use Keepalive for backend connections. For each incoming request on the frontend listener connection, Application Gateway initiates a new backend connection to fulfill that request." Whether "request" at Layer 4 means the entire TCP session or individual application-layer messages is unclear, and the answer determines whether this approach is viable at all.

**If connections are persistent (1:1 pair)**, the remaining concern is the Neo4j routing protocol. When a driver connects using `neo4j+s://`, it receives a routing table containing backend hostnames (e.g., `xxxxxxxx.databases.neo4j.io`) and opens new connections directly to those hostnames, bypassing the gateway entirely. Those connections fail because Databricks serverless has no stable outbound IP. The mitigation is to use `bolt+s://` instead, which disables routing and sends all traffic through the single gateway connection. For Aura BC this is acceptable because Aura manages routing server-side, but it loses client-side routing and failover discovery.

**If connections are not persistent** (new backend connection per application-layer message), the Bolt protocol would break entirely. Each query would require a new Bolt handshake and authentication on the backend, connection state would be lost between queries, and the driver's connection pooling would not function. This would make the approach unworkable.

Beyond connection behavior, [Application Gateway Private Link](https://learn.microsoft.com/en-us/azure/application-gateway/private-link#limitations) has an idle timeout of approximately 5 minutes (300 seconds). The driver must send TCP keepalives more frequently than this or idle connections will be dropped silently. Traffic from the gateway to Aura BC also still traverses the public internet; the IP allowlist restricts who can connect, but the data path itself is not private.