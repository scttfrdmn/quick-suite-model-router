"""
query_spend — AgentCore Lambda Target

Queries the qs-router-spend DynamoDB table and aggregates cost by dimension.

Inputs (event dict):
  department  (str, optional) — filter to a specific department
  user_id     (str, optional) — filter to a specific user
  date_range  (dict)          — {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
  group_by    (str)           — one of: department | user | tool | date

Returns:
  {
    "results": [
      {"group": "...", "cost_usd": N, "token_count_in": N, "token_count_out": N, "call_count": N}
    ],
    "total_cost_usd": N
  }
"""

import json
import logging
import os
from collections import defaultdict

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SPEND_TABLE = os.environ.get("SPEND_TABLE", "")

_dynamo_resource = None


def _get_table():
    global _dynamo_resource
    if _dynamo_resource is None:
        _dynamo_resource = boto3.resource("dynamodb")
    return _dynamo_resource.Table(SPEND_TABLE)


def handler(event, context):
    logger.info(json.dumps({
        "group_by": event.get("group_by"),
        "requestId": event.get("requestContext", {}).get("requestId"),
    }, default=str))

    if not SPEND_TABLE:
        return {"results": [], "total_cost_usd": 0.0, "error": "SPEND_TABLE not configured"}

    # Authorization: extract caller identity from Cognito JWT claims.
    # Non-admins are restricted to their own department and user_id.
    # No claims = direct Lambda invocation (backward compatible / test path).
    _claims = (
        event.get("requestContext", {})
             .get("authorizer", {})
             .get("claims", {})
    )
    if _claims:
        caller_sub = _claims.get("sub") or _claims.get("cognito:username") or ""
        caller_dept = (_claims.get("custom:department") or "").strip()
        groups_raw = _claims.get("cognito:groups", "") or ""
        is_admin = any(g in str(groups_raw) for g in ("finance_admin", "admin"))

        if not is_admin:
            requested_dept = event.get("department") or ""
            if requested_dept and requested_dept != caller_dept:
                return {
                    "error": f"Not authorized to query spend for department '{requested_dept}'",
                    "results": [],
                    "total_cost_usd": 0.0,
                }
            department = caller_dept or requested_dept or None
            requested_uid = event.get("user_id") or ""
            if requested_uid and requested_uid != caller_sub:
                return {
                    "error": f"Not authorized to query spend for user '{requested_uid}'",
                    "results": [],
                    "total_cost_usd": 0.0,
                }
            user_id = caller_sub or requested_uid or None
        else:
            department = event.get("department") or None
            user_id = event.get("user_id") or None
    else:
        department = event.get("department") or None
        user_id = event.get("user_id") or None

    date_range = event.get("date_range") or {}
    group_by = event.get("group_by", "department")

    if group_by not in ("department", "user", "tool", "date"):
        return {"error": f"Invalid group_by: {group_by!r}. Must be one of: department, user, tool, date"}

    start_date = date_range.get("start", "")
    end_date = date_range.get("end", "")

    # Build filter expression
    filter_expr = None

    def _and(expr, new):
        return new if expr is None else (expr & new)

    if department:
        filter_expr = _and(filter_expr, Attr("department").eq(department))
    if user_id:
        filter_expr = _and(filter_expr, Attr("user_id").eq(user_id))
    if start_date:
        filter_expr = _and(filter_expr, Attr("date").gte(start_date))
    if end_date:
        filter_expr = _and(filter_expr, Attr("date").lte(end_date))

    try:
        table = _get_table()
        scan_kwargs = {}
        if filter_expr is not None:
            scan_kwargs["FilterExpression"] = filter_expr

        items = []
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        # Handle pagination
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **scan_kwargs)
            items.extend(resp.get("Items", []))

    except Exception as e:
        logger.error(f"Spend table scan failed: {e}")
        return {"results": [], "total_cost_usd": 0.0, "error": str(e)}

    # Aggregate by group_by dimension
    _GROUP_KEY = {
        "department": "department",
        "user": "user_id",
        "tool": "tool",
        "date": "date",
    }
    dim_key = _GROUP_KEY[group_by]

    agg: dict[str, dict] = defaultdict(lambda: {
        "cost_usd": 0.0,
        "token_count_in": 0,
        "token_count_out": 0,
        "call_count": 0,
    })

    for item in items:
        group_val = item.get(dim_key, "unknown")
        bucket = agg[group_val]
        try:
            bucket["cost_usd"] += float(item.get("cost_usd", 0))
        except (ValueError, TypeError):
            pass
        try:
            bucket["token_count_in"] += int(item.get("token_count_in", 0))
        except (ValueError, TypeError):
            pass
        try:
            bucket["token_count_out"] += int(item.get("token_count_out", 0))
        except (ValueError, TypeError):
            pass
        bucket["call_count"] += 1

    results = [
        {
            "group": group_val,
            "cost_usd": round(bucket["cost_usd"], 6),
            "token_count_in": bucket["token_count_in"],
            "token_count_out": bucket["token_count_out"],
            "call_count": bucket["call_count"],
        }
        for group_val, bucket in sorted(agg.items())
    ]

    total_cost_usd = round(sum(r["cost_usd"] for r in results), 6)

    return {
        "results": results,
        "total_cost_usd": total_cost_usd,
    }
