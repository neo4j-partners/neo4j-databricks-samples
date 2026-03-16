#!/bin/bash
#
# Setup Databricks secrets for Neo4j Private Link connectivity
# Reads Neo4j password from .env and stores it in a Databricks secret scope
#
# Usage:
#   1. Ensure .env has NEO4J_PASSWORD set (run setup-private-link --init)
#   2. Run: ./setup-secrets.sh [profile] [scope-name]
#      Examples:
#        ./setup-secrets.sh                             # Default profile, default scope
#        ./setup-secrets.sh azure-rk-knight             # Custom profile, default scope
#        ./setup-secrets.sh azure-rk-knight my-scope    # Custom profile and scope
#
# Mapping (.env -> Databricks secret):
#   NEO4J_PASSWORD -> password

set -e

PROFILE="${1:-DEFAULT}"
SCOPE_NAME="${2:-neo4j-private-link}"
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
    echo "Run the interactive setup first:"
    echo "  uv run setup-private-link --init"
    exit 1
fi

# Check for databricks CLI
if ! command -v databricks &> /dev/null; then
    log_error "Databricks CLI not found"
    echo "Install: https://docs.databricks.com/dev-tools/cli/install.html"
    echo ""
    echo "Then authenticate:"
    echo "  databricks auth login --profile $PROFILE"
    exit 1
fi

# Load .env file
log_info "Loading configuration from $ENV_FILE"
while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip comments and blank lines
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    # Strip surrounding quotes from value
    key="${line%%=*}"
    value="${line#*=}"
    value="${value%\"}"
    value="${value#\"}"
    export "$key=$value"
done < "$ENV_FILE"

# Validate required variables
missing=()
[[ -z "$NEO4J_PASSWORD" ]] && missing+=("NEO4J_PASSWORD")

if [[ ${#missing[@]} -gt 0 ]]; then
    log_error "Missing required variables in .env: ${missing[*]}"
    echo "Run the interactive setup to set NEO4J_PASSWORD:"
    echo "  uv run setup-private-link --init"
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
set_secret "password" "$NEO4J_PASSWORD"

log_info "Done! Secrets configured in scope: $SCOPE_NAME"
echo ""

# List secrets to confirm
log_info "Validating secrets..."
echo ""
echo "Secrets in scope '$SCOPE_NAME':"
$DBX secrets list-secrets "$SCOPE_NAME"
echo ""

echo "Use in Databricks notebooks:"
echo "  password = dbutils.secrets.get(\"$SCOPE_NAME\", \"password\")"
echo ""
