# Compliance Deployment Guide

**Audience:** Health science school IT administrators deploying Quick Suite
Model Router in environments subject to HIPAA or institutional data governance
requirements.

---

## VPC Isolation (`enable_vpc`)

By default the router Lambdas run in the AWS-managed Lambda network and reach
Secrets Manager, DynamoDB, and Bedrock over public AWS endpoints (TLS
encrypted, never leaving AWS infrastructure). For environments that require
verified network isolation, the `enable_vpc` CDK context flag places every
Lambda function inside a dedicated VPC with no internet egress.

**What the flag does:**

- Creates a private isolated VPC (two AZs, no NAT gateways) or uses an
  existing VPC via the `vpc_id` context variable.
- Attaches all Lambda functions (router, providers, query-spend) to private
  subnets with a security group that blocks outbound traffic except HTTPS
  within the VPC CIDR.
- Provisions Gateway VPC endpoints for S3 and DynamoDB (no per-hour charge).
- Provisions Interface VPC endpoints for Secrets Manager, Lambda, CloudWatch,
  X-Ray, Bedrock, and Bedrock Runtime. These carry a small per-hour cost per
  AZ; budget accordingly.
- Private DNS is enabled on all Interface endpoints so Lambda code requires
  no changes.

**To enable:**

```bash
# Synthesize with VPC isolation
cdk synth -c enable_vpc=true

# Deploy
cdk deploy -c enable_vpc=true

# Use an existing VPC (must have private isolated subnets)
cdk deploy -c enable_vpc=true -c vpc_id=vpc-0abc123def456
```

Or set permanently in `cdk.json`:

```json
{
  "context": {
    "enable_vpc": true
  }
}
```

**Important:** When `enable_vpc=true`, the external provider Lambdas
(Anthropic, OpenAI, Gemini) have no internet egress and cannot reach those
vendor APIs. Deploy with the PHI-only routing configuration below, or configure
NAT gateways separately if external provider access is needed.

---

## PHI-Tagged Request Routing

Any tool call that includes `"data_classification": "phi"` in the request body
is automatically restricted to Amazon Bedrock as the only provider, regardless
of the configured preference lists. Non-Bedrock providers (Anthropic direct,
OpenAI, Gemini) are silently excluded from the candidate set.

**Why this matters:** Bedrock calls stay entirely within your AWS account and
region. Anthropic direct, OpenAI, and Gemini calls leave AWS infrastructure.
For PHI under HIPAA, you need a signed Business Associate Agreement (BAA) with
every service that processes the data. AWS offers a BAA for Bedrock; getting
equivalent coverage from all three external providers adds complexity.

**How to tag a request:**

```json
{
  "prompt": "Summarize the following clinical note: ...",
  "data_classification": "phi",
  "tool": "summarize"
}
```

The field is case-insensitive (`"PHI"` and `"phi"` both activate the filter).
If Bedrock is unavailable or not configured for the requested tool, the router
returns a standard 503 error — no PHI reaches an external provider.

**Recommendation:** For a PHI-dedicated Quick Suite workspace, configure
`department_overrides` in `routing_config.yaml` so the health sciences
department preference lists contain only Bedrock models. The `data_classification`
field then acts as a secondary defense-in-depth check.

---

## CloudTrail Recommendations

Every API call through API Gateway is logged automatically in CloudTrail
under `execute-api.amazonaws.com`. For HIPAA audit purposes:

1. Enable a **dedicated trail** with S3 log delivery to a bucket in a
   separate AWS account (log archive account). Use S3 Object Lock
   (Compliance mode, 6-year retention) to prevent tampering.
2. Enable **CloudWatch Logs integration** on the trail so you can query
   access patterns with CloudWatch Logs Insights.
3. Enable **data events** for the DynamoDB spend ledger table
   (`qs-router-spend`) to record every read and write.
4. Enable **KMS encryption** on the trail using a CMK; rotate the key
   annually.

---

## Recommended Bedrock Guardrail Configuration for Healthcare

The stack deploys a default Bedrock Guardrail. For healthcare deployments,
strengthen it post-deployment via the AWS Console or CLI:

- **PII entities:** Add `US_SOCIAL_SECURITY_NUMBER` (BLOCK), `PHONE` (ANONYMIZE),
  `EMAIL` (ANONYMIZE), `NAME` (ANONYMIZE), `DATE_TIME` (ANONYMIZE). The
  default config includes SSN, phone, email, and credit card.
- **Denied topics:** Add a denied topic named "Medical Diagnosis" with
  description "Providing a clinical diagnosis or treatment recommendation for
  a specific patient." This prevents the LLM from acting as a treating
  clinician.
- **Content filters:** Set MISCONDUCT to HIGH (already HIGH in the default).
  Consider setting VIOLENCE to HIGH for behavioral health deployments.
- **Grounding:** Enable grounding checks on the `research` tool endpoint if
  users will query clinical literature — this reduces hallucinated citations.

Guardrail ID is in the `GuardrailId` CloudFormation output. Update the
`GUARDRAIL_VERSION` CDK context variable after publishing a new version:

```bash
cdk deploy -c guardrail_version=1
```

---

## External Provider Opt-Out

For deployments where institutional policy prohibits data transmission to
non-AWS AI providers, remove Anthropic, OpenAI, and Gemini from all
preference lists in `routing_config.yaml` and do not populate their Secrets
Manager entries. The router will use Bedrock exclusively.

You can verify at runtime by calling the `/status` endpoint — providers
without a populated API key secret show `"available": false`.
