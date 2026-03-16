// private-link.bicep
// Creates Private Link infrastructure for Neo4j Enterprise Edition.
// Deployed on top of an existing Neo4j marketplace deployment.
//
// Resources created:
//   1. pls-nat-subnet       — NAT subnet for Private Link Service
//   2. neo4j-internal-lb    — Internal Standard Load Balancer (Bolt 7687)
//   3. neo4j-pls            — Private Link Service
//
// Usage:
//   az deployment group create \
//     --resource-group <RG> \
//     --template-file private-link.bicep \
//     --parameters vnetName=<VNET> neo4jSubnetName=<SUBNET> location=<LOCATION>

@description('Name of the existing VNet containing Neo4j VMs')
param vnetName string

@description('Name of the existing subnet where Neo4j VMs run')
param neo4jSubnetName string = 'subnet'

@description('Address prefix for the Private Link NAT subnet')
param plsSubnetPrefix string = '10.1.0.0/24'

@description('Azure region')
param location string = resourceGroup().location

// ---------------------------------------------------------------------------
// Existing resources
// ---------------------------------------------------------------------------

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
}

resource neo4jSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: vnet
  name: neo4jSubnetName
}

// ---------------------------------------------------------------------------
// 1. Private Link NAT Subnet
// ---------------------------------------------------------------------------

resource plsSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: 'pls-nat-subnet'
  properties: {
    addressPrefix: plsSubnetPrefix
    privateLinkServiceNetworkPolicies: 'Disabled'
  }
}

// ---------------------------------------------------------------------------
// 2. Internal Standard Load Balancer
// ---------------------------------------------------------------------------

resource internalLb 'Microsoft.Network/loadBalancers@2023-11-01' = {
  name: 'neo4j-internal-lb'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    frontendIPConfigurations: [
      {
        name: 'neo4j-frontend'
        properties: {
          subnet: {
            id: neo4jSubnet.id
          }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
    backendAddressPools: [
      {
        name: 'neo4j-backend'
      }
    ]
    probes: [
      {
        name: 'neo4j-bolt-probe'
        properties: {
          protocol: 'Tcp'
          port: 7687
          intervalInSeconds: 5
          probeThreshold: 2
        }
      }
    ]
    loadBalancingRules: [
      {
        name: 'neo4j-bolt-rule'
        properties: {
          protocol: 'Tcp'
          frontendPort: 7687
          backendPort: 7687
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/loadBalancers/frontendIPConfigurations', 'neo4j-internal-lb', 'neo4j-frontend')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/loadBalancers/backendAddressPools', 'neo4j-internal-lb', 'neo4j-backend')
          }
          probe: {
            id: resourceId('Microsoft.Network/loadBalancers/probes', 'neo4j-internal-lb', 'neo4j-bolt-probe')
          }
          enableTcpReset: true
          // idleTimeoutInMinutes defaults to 4 — sufficient for the demo.
          // For production with long-lived connection pools, increase this.
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// 3. Private Link Service
// ---------------------------------------------------------------------------

resource pls 'Microsoft.Network/privateLinkServices@2023-11-01' = {
  name: 'neo4j-pls'
  location: location
  properties: {
    loadBalancerFrontendIpConfigurations: [
      {
        id: internalLb.properties.frontendIPConfigurations[0].id
      }
    ]
    ipConfigurations: [
      {
        name: 'pls-nat-ip'
        properties: {
          subnet: {
            id: plsSubnet.id
          }
          primary: true
          privateIPAllocationMethod: 'Dynamic'
          privateIPAddressVersion: 'IPv4'
        }
      }
    ]
    visibility: {
      subscriptions: [
        '*'
      ]
    }
    // No autoApproval — all connections require manual approval in the
    // Azure portal. This is the simplest path for the demo.
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output privateLinkServiceId string = pls.id
output privateLinkServiceName string = pls.name
output internalLbName string = internalLb.name
output backendPoolId string = internalLb.properties.backendAddressPools[0].id
