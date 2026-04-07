"""
Key Rotation Checker — internal Lambda (not an AgentCore tool).

Runs weekly via EventBridge Scheduler. Checks each provider API key secret's
LastChangedDate against KEY_ROTATION_MAX_AGE_DAYS and emits a KeyRotationOverdue
CloudWatch metric so operators are alerted when keys are overdue for rotation (#49).

Env vars:
  PROVIDER_SECRET_ARNS      — JSON list of Secrets Manager ARNs to check
  KEY_ROTATION_MAX_AGE_DAYS — max allowed key age in days (default 90)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

cw = boto3.client("cloudwatch")
sm = boto3.client("secretsmanager")

SECRET_ARNS = json.loads(os.environ.get("PROVIDER_SECRET_ARNS", "[]"))
MAX_AGE_DAYS = int(os.environ.get("KEY_ROTATION_MAX_AGE_DAYS", "90"))


def handler(event, context):
    """Check provider secret ages and emit KeyRotationOverdue metric."""
    now = datetime.now(timezone.utc)
    overdue = []

    for arn in SECRET_ARNS:
        try:
            desc = sm.describe_secret(SecretId=arn)
            # LastChangedDate is set on any update; fall back to CreatedDate for fresh secrets.
            last_changed = desc.get("LastChangedDate") or desc.get("CreatedDate")
            if last_changed is None:
                logger.warning(f"No date found for secret {arn}; skipping")
                continue
            age_days = (now - last_changed).days
            if age_days > MAX_AGE_DAYS:
                overdue.append(arn)
                logger.error(json.dumps({
                    "key_rotation_overdue": True,
                    "arn": arn,
                    "age_days": age_days,
                    "max_age_days": MAX_AGE_DAYS,
                }))
            else:
                logger.info(json.dumps({"key_rotation_ok": True, "arn": arn, "age_days": age_days}))
        except Exception as e:
            logger.warning(f"Could not check secret {arn}: {e}")

    overdue_count = len(overdue)
    try:
        cw.put_metric_data(
            Namespace="QuickSuiteModelRouter",
            MetricData=[{
                "MetricName": "KeyRotationOverdue",
                "Value": overdue_count,
                "Unit": "Count",
            }],
        )
    except Exception as e:
        logger.warning(f"Failed to emit KeyRotationOverdue metric: {e}")

    logger.info(json.dumps({
        "checked": len(SECRET_ARNS),
        "overdue": overdue_count,
        "max_age_days": MAX_AGE_DAYS,
    }))
    return {"checked": len(SECRET_ARNS), "overdue": overdue_count}
