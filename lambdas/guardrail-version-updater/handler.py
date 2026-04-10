"""
guardrail_version_updater — Internal Lambda (not an AgentCore tool)

Updates the SSM parameter that controls the active Bedrock Guardrail version
for all provider Lambdas, without requiring a CDK stack redeploy.

Inputs (event dict):
  version  (str, required) — Guardrail version string (e.g. "1", "2", "DRAFT")

Returns:
  {"updated": True, "version": "...", "param": "..."}  on success
  {"error": "..."}                                       on failure
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SSM_PARAM = os.environ["GUARDRAIL_VERSION_SSM_PARAM"]
ssm = boto3.client("ssm")


def handler(event, context):
    version = (event.get("version") or "").strip()
    if not version:
        return {"error": "version is required"}

    try:
        ssm.put_parameter(
            Name=SSM_PARAM,
            Value=version,
            Type="String",
            Overwrite=True,
        )
        logger.info(f"Guardrail version updated to {version!r} in {SSM_PARAM}")
        return {"updated": True, "version": version, "param": SSM_PARAM}
    except Exception as e:
        logger.error(f"Failed to update guardrail version: {e}")
        return {"error": str(e)}
