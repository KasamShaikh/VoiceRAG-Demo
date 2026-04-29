// =============================================================================
// Low-Latency Voice RAG Bot — Phase 1 Infrastructure
// Region: Sweden Central (all resources co-located for latency)
// =============================================================================

targetScope = 'resourceGroup'

@minLength(1)
@maxLength(20)
@description('Short environment name, used for resource naming. e.g. voicebot')
param environmentName string = 'voicebot'

@description('Azure region. Must be a region where Voice Live + AOAI + AI Search are all available.')
param location string = 'swedencentral'

@description('Tags applied to every resource.')
param tags object = {
  'azd-env-name': environmentName
  workload: 'voice-rag-poc'
  region: location
}

@description('Chat model deployment name. Use gpt-5.1-mini if available in region, else gpt-4.1-mini.')
param chatModelName string = 'gpt-4.1-mini'

@description('Chat model version (Azure OpenAI). Update if region requires a different version.')
param chatModelVersion string = '2025-04-14'

@description('Chat model TPM capacity (thousands). 50 = 50K TPM, plenty for a demo.')
param chatModelCapacity int = 50

@description('Embedding model deployment name.')
param embeddingModelName string = 'text-embedding-3-large'
param embeddingModelVersion string = '1'
param embeddingModelCapacity int = 50

@description('Object ID of the developer/principal that should also get data-plane access (for local testing). Leave empty to skip.')
param principalId string = ''

// -----------------------------------------------------------------------------
// Naming
// -----------------------------------------------------------------------------
var uniqueSuffix = uniqueString(resourceGroup().id, environmentName)
var names = {
  aoai:      'aoai-${environmentName}-${uniqueSuffix}'
  speech:    'speech-${environmentName}-${uniqueSuffix}'
  search:    'srch-${environmentName}-${uniqueSuffix}'
  storage:   take(replace('st${environmentName}${uniqueSuffix}', '-', ''), 24)
  redis:     'redis-${environmentName}-${uniqueSuffix}'
  appi:      'appi-${environmentName}-${uniqueSuffix}'
  law:       'law-${environmentName}-${uniqueSuffix}'
  cae:       'cae-${environmentName}-${uniqueSuffix}'
  aca:       'ca-bridge-${environmentName}'
  acr:       take(replace('acr${environmentName}${uniqueSuffix}', '-', ''), 50)
  uami:      'uami-${environmentName}-${uniqueSuffix}'
}

// -----------------------------------------------------------------------------
// User-Assigned Managed Identity (used by bridge Container App)
// -----------------------------------------------------------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.uami
  location: location
  tags: tags
}

// -----------------------------------------------------------------------------
// Log Analytics + Application Insights
// -----------------------------------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: names.law
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: names.appi
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
  }
}

// -----------------------------------------------------------------------------
// Storage Account (kb-pdfs blob container for AI Search datasource)
// -----------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  #disable-next-line BCP334
  name: names.storage
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false   // managed identity only
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }

  resource blob 'blobServices' = {
    name: 'default'
    resource kb 'containers' = {
      name: 'kb-pdfs'
      properties: { publicAccess: 'None' }
    }
  }
}

// -----------------------------------------------------------------------------
// Azure OpenAI (chat + embeddings)
// -----------------------------------------------------------------------------
resource aoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: names.aoai
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: names.aoai
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true   // managed identity only
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aoai
  name: chatModelName
  sku: {
    name: 'GlobalStandard'
    capacity: chatModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aoai
  name: embeddingModelName
  sku: {
    name: 'Standard'
    capacity: embeddingModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
  dependsOn: [ chatDeployment ]   // serialize deployment ops on the same account
}

// -----------------------------------------------------------------------------
// Azure AI Speech (Voice Live + real-time STT/TTS)
// -----------------------------------------------------------------------------
resource speech 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: names.speech
  location: location
  tags: tags
  kind: 'SpeechServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: names.speech
    publicNetworkAccess: 'Enabled'
  }
}

// -----------------------------------------------------------------------------
// Azure AI Search (Standard S1, semantic ranker enabled)
// -----------------------------------------------------------------------------
resource search 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: names.search
  location: location
  tags: tags
  sku: { name: 'standard' }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    semanticSearch: 'standard'
    publicNetworkAccess: 'enabled'
    authOptions: null
    disableLocalAuth: true   // RBAC only
  }
}

// -----------------------------------------------------------------------------
// Azure Cache for Redis (Basic C1 — RediSearch via Enterprise tier ideal,
// but Basic suffices for prototype semantic-cache demo)
// -----------------------------------------------------------------------------
resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: names.redis
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'Basic'
      family: 'C'
      capacity: 1
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    redisConfiguration: {
      'maxmemory-policy': 'allkeys-lru'
    }
  }
}

// -----------------------------------------------------------------------------
// Azure Container Registry + Container Apps Environment + Container App
// -----------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  #disable-next-line BCP334
  name: names.acr
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: names.cae
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

resource bridge 'Microsoft.App/containerApps@2024-03-01' = {
  name: names.aca
  location: location
  tags: union(tags, { 'azd-service-name': 'bridge' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'   // enables WebSocket
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'bridge'
          // Placeholder image; azd will replace this on deploy
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('1.0'), memory: '2.0Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID',           value: uami.properties.clientId }
            { name: 'AZURE_OPENAI_ENDPOINT',     value: aoai.properties.endpoint }
            { name: 'AZURE_OPENAI_CHAT_DEPLOYMENT',      value: chatModelName }
            { name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: embeddingModelName }
            { name: 'AZURE_SEARCH_ENDPOINT',     value: 'https://${search.name}.search.windows.net' }
            { name: 'AZURE_SEARCH_INDEX',        value: 'kb-index' }
            { name: 'AZURE_SPEECH_ENDPOINT',     value: speech.properties.endpoint }
            { name: 'AZURE_SPEECH_REGION',       value: location }
            { name: 'AZURE_STORAGE_ACCOUNT',     value: storage.name }
            { name: 'AZURE_STORAGE_CONTAINER',   value: 'kb-pdfs' }
            { name: 'REDIS_HOST',                value: redis.properties.hostName }
            { name: 'REDIS_PORT',                value: '6380' }
            { name: 'REDIS_SSL',                 value: 'true' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// -----------------------------------------------------------------------------
// RBAC — bridge MI gets data-plane roles on every dependency
// -----------------------------------------------------------------------------
var roleIds = {
  aoaiUser:              '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  cognitiveUser:         'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User (for Speech)
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchServiceContrib:  '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  searchIndexDataContrib:'8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  blobReader:            '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1' // Storage Blob Data Reader
  blobContrib:           'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
  acrPull:               '7f951dda-4ed3-11e8-91a4-d4f0273a8f81'
  redisDataContrib:      'e0f68234-74aa-48ed-b826-c38b57376e17' // Redis Cache Contributor
}

// Bridge MI -> AOAI
resource ra_bridge_aoai 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aoai
  name: guid(aoai.id, uami.id, roleIds.aoaiUser)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.aoaiUser)
  }
}

// Bridge MI -> Speech (Voice Live uses Cognitive Services User)
resource ra_bridge_speech 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: speech
  name: guid(speech.id, uami.id, roleIds.cognitiveUser)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveUser)
  }
}

// Bridge MI -> AI Search (read index + manage index for indexer runs)
resource ra_bridge_search_reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, uami.id, roleIds.searchIndexDataContrib)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataContrib)
  }
}
resource ra_bridge_search_contrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, uami.id, roleIds.searchServiceContrib)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContrib)
  }
}

// Bridge MI -> Storage (read PDFs)
resource ra_bridge_blob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, uami.id, roleIds.blobReader)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobReader)
  }
}

// Bridge MI -> ACR (pull images)
resource ra_bridge_acr 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, roleIds.acrPull)
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPull)
  }
}

// AI Search system MI -> AOAI (so the embedding skill can call AOAI with MI)
resource ra_search_aoai 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aoai
  name: guid(aoai.id, search.id, roleIds.aoaiUser)
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.aoaiUser)
  }
}

// AI Search system MI -> Storage (so the indexer can read PDFs)
resource ra_search_blob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, search.id, roleIds.blobReader)
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobReader)
  }
}

// Optional: developer principal gets data-plane access for local testing
resource ra_dev_aoai 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: aoai
  name: guid(aoai.id, principalId, roleIds.aoaiUser)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.aoaiUser)
  }
}
resource ra_dev_search 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: search
  name: guid(search.id, principalId, roleIds.searchIndexDataContrib)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataContrib)
  }
}
resource ra_dev_search_contrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: search
  name: guid(search.id, principalId, roleIds.searchServiceContrib)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContrib)
  }
}
resource ra_dev_blob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: storage
  name: guid(storage.id, principalId, roleIds.blobContrib)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobContrib)
  }
}
resource ra_dev_speech 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: speech
  name: guid(speech.id, principalId, roleIds.cognitiveUser)
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveUser)
  }
}

// -----------------------------------------------------------------------------
// Outputs (consumed by azd / .env / clients)
// -----------------------------------------------------------------------------
output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup().name

output AZURE_OPENAI_ENDPOINT string = aoai.properties.endpoint
output AZURE_OPENAI_CHAT_DEPLOYMENT string = chatModelName
output AZURE_OPENAI_EMBEDDING_DEPLOYMENT string = embeddingModelName

output AZURE_SEARCH_ENDPOINT string = 'https://${search.name}.search.windows.net'
output AZURE_SEARCH_NAME string = search.name
output AZURE_SEARCH_INDEX string = 'kb-index'

output AZURE_SPEECH_ENDPOINT string = speech.properties.endpoint
output AZURE_SPEECH_REGION string = location
output AZURE_SPEECH_NAME string = speech.name

output AZURE_STORAGE_ACCOUNT string = storage.name
output AZURE_STORAGE_CONTAINER string = 'kb-pdfs'
output AZURE_STORAGE_BLOB_ENDPOINT string = storage.properties.primaryEndpoints.blob

output REDIS_HOST string = redis.properties.hostName
output REDIS_PORT int = 6380

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.properties.loginServer
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = cae.id
output BRIDGE_FQDN string = bridge.properties.configuration.ingress.fqdn

output AZURE_USER_ASSIGNED_IDENTITY_ID string = uami.id
output AZURE_CLIENT_ID string = uami.properties.clientId

output APPLICATIONINSIGHTS_CONNECTION_STRING string = appi.properties.ConnectionString
