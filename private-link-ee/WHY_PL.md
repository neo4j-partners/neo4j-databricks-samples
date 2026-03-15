# Why Private Link: Databricks Serverless to Neo4j

The goal of this project was to demonstrate secure connectivity from Databricks serverless compute to Neo4j using one of two approaches: Azure Private Link or IP-based filtering. After evaluating both paths, we have a working Private Link solution and recommend it as the path forward.

## What We Have Working

Private Link connectivity from Databricks serverless to a self-hosted Neo4j Enterprise Edition cluster on Azure. A Bicep template and Python scripts automate the entire setup: an internal load balancer, a NAT subnet, and a Private Link Service layered on top of an existing Neo4j marketplace deployment. Traffic from a Databricks serverless notebook reaches Neo4j over the Bolt protocol (port 7687), routed entirely over the Azure backbone. No public internet exposure, no IP allowlisting.

The setup was validated against a live 3-node cluster with zero connection drops across 41 connectivity checks. The full workflow (deploy infrastructure, create NCC rule, approve connection, test from notebook, tear down) runs end to end. See the [README](README.md) for the complete setup guide.

## Why Not IP Filtering

IP-based filtering would avoid the Private Link infrastructure by allowlisting Databricks' outbound IPs on the Neo4j firewall. Our understanding is that Databricks serverless compute uses a wide range of shared outbound IP addresses that can change over time, making it impractical to maintain a stable allowlist. Every path we explored to obtain stable outbound IPs is either unavailable or, to our understanding, being deprecated.

- **Service Tag CIDR Extraction.** Databricks serverless outbound IPs come from Azure service tags, but these IPs are managed by Databricks and are not stable by default. The CIDR ranges contain too many possible IPs to maintain as a firewall allowlist. Not a viable option.

- **Serverless Compute Firewall (deprecated).** A Private Preview feature that provides a dedicated JSON endpoint with outbound IPs for serverless compute. It is our understanding that this feature is being deprecated.

- **Stable NAT IPs for Non-Storage Resources (deprecated).** Databricks can provide stable NAT IPs specifically for non-storage use cases like connecting to Neo4j. It is our understanding that this option is also being deprecated.

- **NCC with Network Security Perimeter (NSP).** NSP only supports Azure-native resources and does not extend to third-party services. Neo4j Aura is not an Azure-native resource, so NSP cannot be used to secure connectivity to it.

## Recommendation

With IP filtering options either unstable, deprecated, or architecturally incompatible, Private Link is the reliable connectivity path from Databricks serverless to Neo4j. The solution in this repository is working, tested, and ready to use.
