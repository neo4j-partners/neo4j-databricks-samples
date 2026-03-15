#!/bin/bash
#
# Setup Databricks secrets for Neo4j Aura Agent MCP
# Reads configuration from .env and creates secrets in Databricks
#
# Usage:
#   1. Copy .env.sample to .env and fill in your MCP server URL
#   2. Run: ./setup-secrets.sh [profile] [scope-name]
#      Examples:
#        ./setup-secrets.sh                             # Default profile, default scope
#        ./setup-secrets.sh DEFAULT                     # Explicit default profile
#        ./setup-secrets.sh my-profile                  # Custom profile, default scope
#        ./setup-secrets.sh my-profile my-scope         # Custom profile and scope
#
# Mapping (.env → Databricks secret):
#   MCP_SERVER_URL → mcp-server-url

set -e

PROFILE="${1:-DEFAULT}"
SCOPE_NAME="${2:-aura-mcp-secrets}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# Pass --profile to all databricks CLI commands
DBX="databricks --profile $PROFILE"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

log_info "Using profile: $PROFILE"
log_info "Using secret scope: $SCOPE_NAME"

# Check for .env file
if [[ ! -f "$ENV_FILE" ]]; then
    log_error ".env file not found at $ENV_FILE"
    echo "Copy .env.sample to .env and fill in your MCP server URL:"
    echo "  cp .env.sample .env"
    exit 1
fi

# Check for databricks CLI
if ! command -v databricks &> /dev/null; then
    log_error "Databricks CLI not found"
    echo "Install with: pip install databricks-cli"
    echo "Or: brew install databricks"
    echo ""
    echo "Then configure with: databricks auth login"
    exit 1
fi

# Load .env file
log_info "Loading configuration from $ENV_FILE"
set -a
source "$ENV_FILE"
set +a

# Validate required variables
missing=()
[[ -z "$MCP_SERVER_URL" ]] && missing+=("MCP_SERVER_URL")

if [[ ${#missing[@]} -gt 0 ]]; then
    log_error "Missing required variables in .env: ${missing[*]}"
    exit 1
fi

# Create secret scope (ignore error if already exists)
log_info "Creating secret scope: $SCOPE_NAME"
if $DBX secrets create-scope "$SCOPE_NAME" 2>/dev/null; then
    log_info "Secret scope created"
else
    log_warn "Secret scope already exists (or failed to create)"
fi

# Function to set a secret
set_secret() {
    local key=$1
    local value=$2
    log_info "Setting secret: $key"
    echo -n "$value" | $DBX secrets put-secret "$SCOPE_NAME" "$key"
}

# Set secrets
set_secret "mcp-server-url" "$MCP_SERVER_URL"

log_info "Done! Secrets configured in scope: $SCOPE_NAME"
echo ""

# List secrets to confirm
log_info "Validating secrets..."
echo ""
echo "Secrets in scope '$SCOPE_NAME':"
$DBX secrets list-secrets "$SCOPE_NAME"
echo ""

echo "Use in Databricks notebooks:"
echo "  mcp_url = dbutils.secrets.get(\"$SCOPE_NAME\", \"mcp-server-url\")"
echo ""
