"""
Common provider interface and governance utilities.

Every provider Lambda implements the same contract:
  Input:  dict with prompt, model, system_prompt, max_tokens, temperature
  Output: dict with content, provider, model, input_tokens, output_tokens, etc.

Governance functions wrap all provider calls with Bedrock Guardrails
and CloudWatch usage metering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

_bedrock_client = None
_cw_client = None
_dynamo_resource = None


def get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def get_cw_client():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def get_dynamo_table(table_name: str):
    global _dynamo_resource
    if _dynamo_resource is None:
        _dynamo_resource = boto3.resource("dynamodb")
    return _dynamo_resource.Table(table_name)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def apply_guardrail(
    text: str,
    guardrail_id: str,
    guardrail_version: str = "DRAFT",
    source: str = "INPUT",
) -> tuple[str, bool]:
    """
    Apply Bedrock Guardrail to text.
    Returns (processed_text, was_blocked).
    """
    if not guardrail_id:
        return text, False

    try:
        client = get_bedrock_client()
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
        )

        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs", [])
            blocked_text = (
                outputs[0]["text"] if outputs else "Content blocked by policy."
            )
            return blocked_text, True

        return text, False

    except Exception as e:
        logger.warning(f"Guardrail application failed (fail-open): {e}")
        return text, False


def apply_guardrail_safe(
    text: str,
    guardrail_id: str,
    guardrail_version: str = "DRAFT",
    source: str = "INPUT",
) -> tuple[str, bool]:
    """
    Fail-closed guardrail wrapper. Returns (text, blocked=True) on any exception
    and emits a GuardrailError CloudWatch metric.
    """
    if not guardrail_id:
        return text, False

    try:
        client = get_bedrock_client()
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
        )
        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            outputs = response.get("outputs", [])
            blocked_text = (
                outputs[0]["text"] if outputs else "Content blocked by policy."
            )
            return blocked_text, True
        return text, False

    except Exception as exc:
        logger.error(json.dumps({"guardrail_error": str(exc), "source": source}))
        try:
            get_cw_client().put_metric_data(
                Namespace="QuickSuiteModelRouter",
                MetricData=[{
                    "MetricName": "GuardrailError",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Source", "Value": source}],
                }],
            )
        except Exception:
            pass
        return (text, True)  # fail-closed


# ---------------------------------------------------------------------------
# Usage metering
# ---------------------------------------------------------------------------

def emit_usage_metrics(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    guardrail_blocked: bool = False,
    guardrail_applied: bool = False,
    cache_hit: bool = False,
    department: str = "",
):
    """Emit CloudWatch custom metrics for usage tracking."""
    try:
        client = get_cw_client()
        dimensions = [
            {"Name": "Provider", "Value": provider},
            {"Name": "Model", "Value": model or "unknown"},
            {"Name": "Department", "Value": department or "none"},
        ]

        metrics = [
            {
                "MetricName": "InputTokens",
                "Dimensions": dimensions,
                "Value": input_tokens,
                "Unit": "Count",
            },
            {
                "MetricName": "OutputTokens",
                "Dimensions": dimensions,
                "Value": output_tokens,
                "Unit": "Count",
            },
            {
                "MetricName": "Latency",
                "Dimensions": dimensions,
                "Value": latency_ms,
                "Unit": "Milliseconds",
            },
        ]

        if guardrail_blocked:
            metrics.append({
                "MetricName": "GuardrailBlocked",
                "Dimensions": dimensions,
                "Value": 1,
                "Unit": "Count",
            })

        if guardrail_applied:
            metrics.append({
                "MetricName": "GuardrailApplied",
                "Dimensions": dimensions,
                "Value": 1,
                "Unit": "Count",
            })

        if cache_hit:
            metrics.append({
                "MetricName": "CacheHit",
                "Dimensions": [{"Name": "Provider", "Value": "cache"}],
                "Value": 1,
                "Unit": "Count",
            })
        else:
            metrics.append({
                "MetricName": "CacheMiss",
                "Dimensions": [{"Name": "Provider", "Value": "cache"}],
                "Value": 1,
                "Unit": "Count",
            })

        client.put_metric_data(
            Namespace="QuickSuiteModelRouter",
            MetricData=metrics,
        )
    except Exception as e:
        logger.warning(f"Failed to emit metrics: {e}")


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

def cache_key(prompt: str, model: str, system_prompt: str = "",
              max_tokens: int = 4096, context: str = "",
              temperature: float = 0.0, tool: str = "") -> str:
    """Generate a deterministic cache key from request parameters."""
    raw = f"{tool}|{model}|{system_prompt}|{max_tokens}|{temperature}|{context}|{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(table_name: str, key: str) -> Optional[dict]:
    """Retrieve a cached response. Returns None on miss or error."""
    if not table_name:
        return None
    try:
        table = get_dynamo_table(table_name)
        resp = table.get_item(Key={"cache_key": key})
        item = resp.get("Item")
        if item and "response" in item:
            return json.loads(item["response"])
        return None
    except Exception as e:
        logger.warning(f"Cache read failed: {e}")
        return None


def cache_put(
    table_name: str,
    key: str,
    response: dict,
    ttl_minutes: int = 60,
):
    """Store a response in the cache with TTL."""
    if not table_name:
        return
    try:
        table = get_dynamo_table(table_name)
        table.put_item(
            Item={
                "cache_key": key,
                "response": json.dumps(response),
                "ttl": int(time.time()) + (ttl_minutes * 60),
            }
        )
    except Exception as e:
        logger.warning(f"Cache write failed: {e}")


# ---------------------------------------------------------------------------
# Cost pricing table (input_per_1k, output_per_1k) in USD
# ---------------------------------------------------------------------------

_COST_TABLE: dict[str, tuple[float, float]] = {
    # Bedrock-hosted Anthropic
    "anthropic.claude-sonnet-4-20250514-v1:0": (0.003, 0.015),
    "anthropic.claude-3-5-sonnet-20241022-v2:0": (0.003, 0.015),
    "anthropic.claude-3-haiku-20240307-v1:0": (0.00025, 0.00125),
    # Bedrock Nova
    "amazon.nova-pro-v1:0": (0.0008, 0.0032),
    "amazon.nova-lite-v1:0": (0.00006, 0.00024),
    # Anthropic direct
    "claude-sonnet-4-20250514": (0.003, 0.015),
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-haiku-20240307": (0.00025, 0.00125),
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    # Gemini
    "gemini-2.5-pro": (0.00125, 0.01),
    "gemini-2.5-flash": (0.000075, 0.0003),
    "gemini-1.5-pro": (0.00125, 0.005),
}

_DEFAULT_COST = (0.001, 0.005)  # fallback if model not in table


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a call."""
    # Strip provider prefix (e.g. "bedrock/..." → "...")
    if "/" in model:
        model = model.split("/", 1)[1]
    in_per_1k, out_per_1k = _COST_TABLE.get(model, _DEFAULT_COST)
    return round((input_tokens / 1000.0) * in_per_1k + (output_tokens / 1000.0) * out_per_1k, 8)


# ---------------------------------------------------------------------------
# Spend ledger
# ---------------------------------------------------------------------------

def spend_record_write(
    table_name: str,
    department: str,
    user_id: str,
    tool: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
):
    """Write one spend record to the qs-router-spend table after a successful call."""
    if not table_name:
        return
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        timestamp = now.isoformat()
        cost_usd = compute_cost_usd(model, input_tokens, output_tokens)
        # TTL: 13 months from now
        expires_at = int(time.time()) + (13 * 30 * 24 * 3600)

        table = get_dynamo_table(table_name)
        table.put_item(
            Item={
                "pk": f"{department}#{user_id}",
                "sk": f"{tool}#{date_str}#{timestamp}",
                "department": department,
                "user_id": user_id,
                "tool": tool,
                "provider": provider,
                "model": model,
                "date": date_str,
                "timestamp": timestamp,
                "token_count_in": input_tokens,
                "token_count_out": output_tokens,
                "cost_usd": str(cost_usd),
                "expires_at": expires_at,
            }
        )
    except Exception as e:
        logger.warning(f"Spend ledger write failed: {e}")


def spend_query_department_month(table_name: str, department: str, month_prefix: str) -> float:
    """
    Return total cost_usd for ALL users in a department in a given month (YYYY-MM).
    Scans the table filtering on department + date begins_with month_prefix.
    Returns 0.0 on any error (fail-open).
    """
    if not table_name:
        return 0.0
    try:
        from boto3.dynamodb.conditions import Attr
        table = get_dynamo_table(table_name)
        resp = table.scan(
            FilterExpression=Attr("department").eq(department) & Attr("date").begins_with(month_prefix),
            ProjectionExpression="cost_usd",
        )
        total = 0.0
        for item in resp.get("Items", []):
            try:
                total += float(item.get("cost_usd", 0))
            except (ValueError, TypeError):
                pass
        return total
    except Exception as e:
        logger.warning(f"Spend department query failed (fail-open): {e}")
        return 0.0
