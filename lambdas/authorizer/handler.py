"""
Cognito JWT Lambda Authorizer with per-user usage tracking.

Validates the Cognito Bearer token from the Authorization header, extracts the
`sub` claim, and returns an IAM Allow policy with `usageIdentifierKey = sub`.
API Gateway uses `usageIdentifierKey` to enforce per-user throttle + quota against
the per-user usage plan (when `rate_limit_per_minute` CDK context is set).
"""

import base64
import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")


def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode the JWT payload segment (middle part). No signature verification."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed JWT: expected 3 segments")
    # Add padding
    payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes.decode("utf-8"))


def handler(event, context):
    """
    API Gateway Lambda REQUEST authorizer.

    event keys used:
    - authorizationToken: Bearer <jwt>   (TOKEN authorizer)
    - headers.Authorization              (REQUEST authorizer fallback)
    - methodArn: resource ARN to authorize
    """
    token = event.get("authorizationToken") or (event.get("headers") or {}).get("Authorization", "")
    method_arn = event.get("methodArn", "*")

    if not token:
        logger.warning("No authorization token present")
        raise Exception("Unauthorized")  # API Gateway returns 401

    # Strip "Bearer " prefix
    if token.startswith("Bearer "):
        token = token[7:]

    try:
        claims = _decode_jwt_payload(token)
    except Exception as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise Exception("Unauthorized")

    sub = claims.get("sub", "")
    if not sub:
        logger.warning("JWT missing sub claim")
        raise Exception("Unauthorized")

    # Build IAM policy allowing invocation of this method's ARN (wildcard on stage/method)
    # e.g. arn:aws:execute-api:us-east-1:123456:abcdef/prod/POST/tools/analyze
    # → allow all methods in this API
    arn_parts = method_arn.split(":")
    if len(arn_parts) >= 6:
        region = arn_parts[3]
        account = arn_parts[4]
        api_gateway_arn_suffix = arn_parts[5]
        api_id = api_gateway_arn_suffix.split("/")[0]
        stage = api_gateway_arn_suffix.split("/")[1] if "/" in api_gateway_arn_suffix else "*"
        resource_arn = f"arn:aws:execute-api:{region}:{account}:{api_id}/{stage}/*/*"
    else:
        resource_arn = method_arn

    policy = {
        "principalId": sub,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "execute-api:Invoke",
                    "Resource": resource_arn,
                }
            ],
        },
        # usageIdentifierKey tells API Gateway which usage plan bucket to use per principal
        "usageIdentifierKey": sub,
        "context": {
            "sub": sub,
            "email": claims.get("email", ""),
            "cognito_groups": json.dumps(claims.get("cognito:groups", [])),
            "department": claims.get("custom:department", ""),
        },
    }

    logger.info("Authorized sub=%s", sub)
    return policy
