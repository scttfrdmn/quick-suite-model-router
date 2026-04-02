#!/usr/bin/env python3
"""Quick Suite Model Router — CDK Application Entry Point."""
import aws_cdk as cdk
from stacks.model_router_stack import ModelRouterStack

app = cdk.App()

primary_region = app.node.try_get_context("region") or "us-east-1"
account = app.node.try_get_context("account")

primary_stack = ModelRouterStack(
    app,
    "QuickSuiteModelRouter",
    description=(
        "Multi-provider LLM router for Amazon Quick Suite "
        "via Bedrock AgentCore Gateway"
    ),
    env=cdk.Environment(
        account=account,
        region=primary_region,
    ),
)

# Optional multi-region failover stack.
# Deploy with: cdk deploy --context secondary_region=us-west-2
secondary_region = app.node.try_get_context("secondary_region")
if secondary_region:
    from stacks.multi_region_stack import MultiRegionStack
    MultiRegionStack(
        app,
        "QuickSuiteModelRouterMultiRegion",
        description=(
            "Route 53 health-check failover for Quick Suite Model Router "
            "(secondary region)"
        ),
        primary_api_url=primary_stack.api_url,
        env=cdk.Environment(
            account=account,
            region="us-east-1",  # Route 53 resources must be in us-east-1
        ),
    )

app.synth()
