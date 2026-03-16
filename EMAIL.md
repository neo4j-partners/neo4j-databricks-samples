Hi team,

I set up and tested private connectivity between Databricks serverless compute and Neo4j on Azure using Private Link. The full walkthrough, including automation scripts and a test notebook, is in the repo:

https://github.com/neo4j-partners/neo4j-databricks-samples/tree/main/private-link-ee

I tested against Neo4j Enterprise Edition deployed from the Azure Marketplace rather than Aura VDC, since the only Aura VDC instance I had access to was on GCP. The connectivity pattern is a very close approximation of what Aura VDC provides. Aura VDC manages the Private Link Service automatically; with EE, the repo's Bicep template and Python scripts build that same layer. From the Databricks notebook's perspective, the connection works the same way in both cases.

The key piece on the Databricks side is a Network Connectivity Configuration (NCC). Our understanding is that serverless runs inside Databricks-managed infrastructure with no customer-controlled VNet, so there is no direct network path to private resources. The NCC bridges that gap by creating a private endpoint in the Databricks-managed subscription that connects to a Private Link Service in front of the database. All driver traffic routes over the Azure backbone, and nothing touches the public internet.

The repo includes scripts that handle discovery, infrastructure deployment, connection approval, NCC attachment, and teardown. Only the Azure resource group name is required as input; everything else is discovered automatically.

Best,
Ryan
