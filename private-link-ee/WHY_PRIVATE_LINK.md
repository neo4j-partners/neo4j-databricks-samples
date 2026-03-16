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

This would appear to solve the problem. Application Gateway v2 provides a static public IP and supports Private Link for inbound connections. Databricks serverless could reach the gateway via an NCC private endpoint, and the gateway would forward traffic to Aura BC using its stable public IP, which can be added to the Aura BC allowlist. This sidesteps the problem of Databricks serverless having no stable outbound IPs.

In practice, this approach runs into several issues:

- **Protocol mismatch.** Application Gateway is a Layer 7 HTTP/HTTPS load balancer. Neo4j's Bolt protocol (port 7687) is a binary TCP protocol, not HTTP. Application Gateway v2 has added TCP/TLS proxy support, but Bolt traffic does not benefit from any of the gateway's Layer 7 features. Compatibility with the Neo4j driver's connection lifecycle (routing tables, connection pooling, keep-alives) has not been validated in this configuration.

- **Idle timeout.** Application Gateway Private Link has an idle timeout of approximately 5 minutes (300 seconds). Neo4j driver connections are typically long-lived. If the driver and gateway keep-alive intervals are not tuned precisely, connections will drop silently. The driver may not handle reconnection gracefully through a proxy.

- **Public internet leg.** Traffic from Application Gateway to Aura BC still traverses the public internet. The IP allowlist restricts who can connect, but the data path itself is not private. This may not meet the security or compliance requirements that motivated the private connectivity effort in the first place.

- **Cost and operational overhead.** Application Gateway v2 is a significant resource with its own pricing, subnet requirements, scaling behavior, and operational surface. Managing it purely as a TCP proxy for database traffic adds complexity without the benefits (WAF, URL routing, SSL termination) that justify its cost in typical web application deployments.

The more direct paths are upgrading to Aura VDC, which provides Private Link natively, or deploying Neo4j Enterprise Edition with Private Link infrastructure. This repository demonstrates the latter.

## Path Forward

With IP filtering options either unstable, deprecated, or architecturally incompatible, Private Link is the reliable connectivity path from Databricks serverless to Neo4j. The solution in this repository is working, tested, and ready to use.
