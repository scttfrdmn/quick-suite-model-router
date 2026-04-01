#!/usr/bin/env bash
# post-deploy.sh — Quick Suite Model Router post-deployment configuration helper
#
# Run after `cdk deploy` to retrieve stack outputs and print the commands
# needed to finish configuration (provider secrets, AgentCore registration).
#
# Usage:
#   bash scripts/post-deploy.sh
#   bash scripts/post-deploy.sh --stack-name MyCustomStackName
#   bash scripts/post-deploy.sh --region us-west-2

set -euo pipefail

STACK_NAME="QuickSuiteModelRouter"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region)     REGION="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo ""
echo "========================================================"
echo "  Quick Suite Model Router — Post-Deploy Configuration"
echo "========================================================"
echo ""

# Fetch all stack outputs into an associative array
echo "Fetching stack outputs from CloudFormation..."
OUTPUTS_JSON=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs' \
  --output json 2>/dev/null) || {
    echo ""
    echo "ERROR: Could not retrieve stack '$STACK_NAME' in region '$REGION'."
    echo "  - Verify the stack deployed successfully: cdk deploy"
    echo "  - Check region: export AWS_DEFAULT_REGION=<your-region>"
    echo "  - Check stack name: --stack-name <name>"
    exit 1
  }

get_output() {
  echo "$OUTPUTS_JSON" | python3 -c \
    "import json,sys; d={o['OutputKey']:o['OutputValue'] for o in json.load(sys.stdin)}; print(d.get('$1','<NOT_FOUND>'))"
}

API_URL=$(get_output "ApiEndpoint")
POOL_ID=$(get_output "CognitoUserPoolId")
CLIENT_ID=$(get_output "CognitoClientId")
TOKEN_URL=$(get_output "CognitoTokenUrl")
GUARDRAIL_ID=$(get_output "GuardrailId")
ANTHROPIC_ARN=$(get_output "AnthropicSecretArn")
OPENAI_ARN=$(get_output "OpenaiSecretArn")
GEMINI_ARN=$(get_output "GeminiSecretArn")

echo ""
echo "--------------------------------------------------------"
echo "  Stack Outputs"
echo "--------------------------------------------------------"
echo "  API Gateway URL : $API_URL"
echo "  Cognito Pool ID : $POOL_ID"
echo "  Cognito Client  : $CLIENT_ID"
echo "  Token URL       : $TOKEN_URL"
echo "  Guardrail ID    : $GUARDRAIL_ID"
echo ""

echo "--------------------------------------------------------"
echo "  Step 1 of 3 — Populate Provider Secrets"
echo "  (skip providers you don't have credentials for)"
echo "--------------------------------------------------------"
echo ""
echo "  # Amazon Bedrock (no secret needed — uses IAM)"
echo "  # Available immediately after deploy."
echo ""
echo "  # Anthropic"
echo "  aws secretsmanager put-secret-value \\"
echo "    --secret-id \"$ANTHROPIC_ARN\" \\"
echo "    --secret-string '{\"api_key\": \"sk-ant-api03-...\"}'"
echo ""
echo "  # OpenAI (university site license)"
echo "  aws secretsmanager put-secret-value \\"
echo "    --secret-id \"$OPENAI_ARN\" \\"
echo "    --secret-string '{\"api_key\": \"sk-...\", \"organization\": \"org-...\"}'"
echo ""
echo "  # Google Gemini"
echo "  aws secretsmanager put-secret-value \\"
echo "    --secret-id \"$GEMINI_ARN\" \\"
echo "    --secret-string '{\"api_key\": \"AIza...\"}'"
echo ""

echo "--------------------------------------------------------"
echo "  Step 2 of 3 — Register with AgentCore Gateway"
echo "--------------------------------------------------------"
echo ""
echo "  In the AWS Console → Bedrock → AgentCore → Gateways:"
echo ""
echo "  1. Create or open a Gateway"
echo "  2. Add Target → OpenAPI Target"
echo "  3. Fill in:"
echo "       Name        : quick-suite-model-router"
echo "       API URL     : $API_URL"
echo "       OpenAPI spec: ${API_URL}openapi.json"
echo "       Auth type   : OAuth 2.0 Client Credentials"
echo "       Token URL   : $TOKEN_URL"
echo "       Client ID   : $CLIENT_ID"
echo "       Client Secret: (retrieve from Secrets Manager — see below)"
echo ""
echo "  Retrieve the Cognito app client secret:"
echo "  aws cognito-idp describe-user-pool-client \\"
echo "    --user-pool-id $POOL_ID \\"
echo "    --client-id $CLIENT_ID \\"
echo "    --region $REGION \\"
echo "    --query 'UserPoolClient.ClientSecret' --output text"
echo ""
echo "  Full step-by-step: docs/quicksuite-integration.md"
echo ""

echo "--------------------------------------------------------"
echo "  Step 3 of 3 — Connect Quick Suite"
echo "--------------------------------------------------------"
echo ""
echo "  In Quick Suite → Settings → Integrations → MCP Actions:"
echo "  Use the filled-in values from quicksuite/agent-template.json"
echo "  (run this script first, paste outputs into the template)"
echo ""
echo "  Full instructions: docs/quicksuite-integration.md"
echo ""
echo "========================================================"
echo "  Done. See docs/ for detailed setup guides per provider."
echo "========================================================"
echo ""
