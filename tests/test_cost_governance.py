"""
Tests for v0.7.0 Cost Governance:
- spend_record_write: correct DynamoDB PK/SK, TTL, cost_usd calculation
- compute_cost_usd: known models and fallback
- query_spend handler: aggregation by dimension, date filtering, empty results
- Budget cap enforcement: blocked at cap, fail-open on AWS error
- Router integration: spend record written on successful call
"""

import importlib
import json
import os
import sys
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO_ROOT, "lambdas", "common", "python"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambdas", "router"))
# query-spend handler is loaded on-demand via importlib to avoid name collision


# ---------------------------------------------------------------------------
# Helper: reload handler with specific env
# ---------------------------------------------------------------------------

def _load_handler(routing_config, provider_functions, provider_secrets,
                  cache_table="", spend_table="", budget_caps_secret_arn=""):
    env = {
        "ROUTING_CONFIG": json.dumps(routing_config),
        "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
        "PROVIDER_SECRETS": json.dumps(provider_secrets),
        "CACHE_TABLE": cache_table,
        "CACHE_TTL_MINUTES": "60",
        "SPEND_TABLE": spend_table,
        "BUDGET_CAPS_SECRET_ARN": budget_caps_secret_arn,
    }
    with patch.dict(os.environ, env):
        import handler
        importlib.reload(handler)
        return handler


def _make_lambda_payload(data: dict):
    payload_mock = MagicMock()
    payload_mock.read.return_value = json.dumps(data).encode()
    response_mock = MagicMock()
    response_mock.__getitem__ = MagicMock(
        side_effect=lambda k: payload_mock if k == "Payload" else None
    )
    return response_mock


# ---------------------------------------------------------------------------
# compute_cost_usd
# ---------------------------------------------------------------------------

class TestComputeCostUsd:
    def test_known_model_claude_sonnet(self):
        import provider_interface
        importlib.reload(provider_interface)
        cost = provider_interface.compute_cost_usd(
            "anthropic.claude-sonnet-4-20250514-v1:0", 1000, 500
        )
        # 1000 * 0.003/1000 + 500 * 0.015/1000 = 0.003 + 0.0075 = 0.0105
        assert abs(cost - 0.0105) < 1e-6

    def test_known_model_gpt4o(self):
        import provider_interface
        importlib.reload(provider_interface)
        cost = provider_interface.compute_cost_usd("gpt-4o", 2000, 1000)
        # 2000 * 0.0025/1000 + 1000 * 0.01/1000 = 0.005 + 0.01 = 0.015
        assert abs(cost - 0.015) < 1e-6

    def test_unknown_model_uses_fallback(self):
        import provider_interface
        importlib.reload(provider_interface)
        cost = provider_interface.compute_cost_usd("unknown-model-xyz", 1000, 1000)
        # fallback: 0.001/1k + 0.005/1k = 0.001 + 0.005 = 0.006
        assert abs(cost - 0.006) < 1e-6

    def test_zero_tokens_returns_zero(self):
        import provider_interface
        importlib.reload(provider_interface)
        cost = provider_interface.compute_cost_usd("gpt-4o", 0, 0)
        assert cost == 0.0

    def test_model_with_provider_prefix_stripped(self):
        import provider_interface
        importlib.reload(provider_interface)
        cost_with = provider_interface.compute_cost_usd(
            "bedrock/anthropic.claude-sonnet-4-20250514-v1:0", 1000, 0
        )
        cost_without = provider_interface.compute_cost_usd(
            "anthropic.claude-sonnet-4-20250514-v1:0", 1000, 0
        )
        assert cost_with == cost_without


# ---------------------------------------------------------------------------
# spend_record_write (MagicMock DynamoDB)
# ---------------------------------------------------------------------------

class TestSpendRecordWrite:
    def _reload_pi(self):
        import provider_interface
        importlib.reload(provider_interface)
        return provider_interface

    def test_write_called_with_correct_pk(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="cs-dept",
                user_id="alice",
                tool="analyze",
                provider="bedrock",
                model="anthropic.claude-sonnet-4-20250514-v1:0",
                input_tokens=100,
                output_tokens=50,
            )
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["pk"] == "cs-dept#alice"

    def test_write_sk_starts_with_tool(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="cs-dept",
                user_id="alice",
                tool="analyze",
                provider="bedrock",
                model="anthropic.claude-sonnet-4-20250514-v1:0",
                input_tokens=100,
                output_tokens=50,
            )
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["sk"].startswith("analyze#")

    def test_write_includes_cost_usd(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="bio-dept",
                user_id="bob",
                tool="code",
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                input_tokens=500,
                output_tokens=200,
            )
        item = mock_table.put_item.call_args[1]["Item"]
        assert float(item["cost_usd"]) > 0

    def test_write_ttl_is_13_months(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="d",
                user_id="u",
                tool="t",
                provider="p",
                model="m",
                input_tokens=10,
                output_tokens=5,
            )
        item = mock_table.put_item.call_args[1]["Item"]
        now = int(time.time())
        thirteen_months = 13 * 30 * 24 * 3600
        # Allow ±60 seconds
        assert abs(item["expires_at"] - (now + thirteen_months)) < 60

    def test_no_write_when_table_empty(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="",
                department="d",
                user_id="u",
                tool="t",
                provider="p",
                model="m",
                input_tokens=10,
                output_tokens=5,
            )
        mock_table.put_item.assert_not_called()

    def test_write_fails_silently(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("DynamoDB unavailable")
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            # Should not raise
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="d",
                user_id="u",
                tool="t",
                provider="p",
                model="m",
                input_tokens=10,
                output_tokens=5,
            )

    def test_write_includes_all_expected_attributes(self):
        pi = self._reload_pi()
        mock_table = MagicMock()
        with patch.object(pi, "get_dynamo_table", return_value=mock_table):
            pi.spend_record_write(
                table_name="qs-router-spend",
                department="cs-dept",
                user_id="alice",
                tool="analyze",
                provider="bedrock",
                model="anthropic.claude-sonnet-4-20250514-v1:0",
                input_tokens=100,
                output_tokens=50,
            )
        item = mock_table.put_item.call_args[1]["Item"]
        for key in ("pk", "sk", "department", "user_id", "tool", "provider",
                    "model", "date", "timestamp", "token_count_in",
                    "token_count_out", "cost_usd", "expires_at"):
            assert key in item, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# query_spend handler
# ---------------------------------------------------------------------------

def _make_scan_items(*items):
    """Return a DynamoDB scan response mock."""
    return {"Items": list(items)}


def _sample_item(department="cs-dept", user_id="alice", tool="analyze",
                 date="2026-04-01", cost_usd="0.005",
                 token_count_in=100, token_count_out=50):
    return {
        "pk": f"{department}#{user_id}",
        "sk": f"{tool}#{date}#2026-04-01T10:00:00+00:00",
        "department": department,
        "user_id": user_id,
        "tool": tool,
        "date": date,
        "cost_usd": cost_usd,
        "token_count_in": token_count_in,
        "token_count_out": token_count_out,
    }


def _load_query_spend_handler(spend_table="qs-router-spend"):
    """
    Load the query-spend handler using a unique module name to avoid
    collision with the router's 'handler' module.
    """
    import importlib.util
    qs_path = os.path.join(REPO_ROOT, "lambdas", "query-spend")
    spec = importlib.util.spec_from_file_location(
        "query_spend_handler",
        os.path.join(qs_path, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"SPEND_TABLE": spend_table}):
        spec.loader.exec_module(mod)
    return mod


class TestQuerySpendHandler:
    def _load_qs(self, spend_table="qs-router-spend"):
        return _load_query_spend_handler(spend_table)

    def test_returns_empty_results_for_unknown_department(self):
        qs = self._load_qs()
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": []}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({
                "department": "nonexistent",
                "date_range": {"start": "2026-01-01", "end": "2026-12-31"},
                "group_by": "department",
            }, None)
        assert result["results"] == []
        assert result["total_cost_usd"] == 0.0

    def test_group_by_department(self):
        qs = self._load_qs()
        items = [
            _sample_item("cs-dept", "alice", cost_usd="0.01"),
            _sample_item("cs-dept", "bob", cost_usd="0.005"),
            _sample_item("bio-dept", "carol", cost_usd="0.003"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "department"}, None)

        groups = {r["group"]: r for r in result["results"]}
        assert "cs-dept" in groups
        assert "bio-dept" in groups
        assert abs(groups["cs-dept"]["cost_usd"] - 0.015) < 1e-6
        assert groups["cs-dept"]["call_count"] == 2
        assert abs(result["total_cost_usd"] - 0.018) < 1e-6

    def test_group_by_tool(self):
        qs = self._load_qs()
        items = [
            _sample_item(tool="analyze", cost_usd="0.01"),
            _sample_item(tool="analyze", cost_usd="0.01"),
            _sample_item(tool="code", cost_usd="0.02"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "tool"}, None)

        groups = {r["group"]: r for r in result["results"]}
        assert groups["analyze"]["call_count"] == 2
        assert abs(groups["analyze"]["cost_usd"] - 0.02) < 1e-6
        assert abs(groups["code"]["cost_usd"] - 0.02) < 1e-6

    def test_group_by_user(self):
        qs = self._load_qs()
        items = [
            _sample_item(user_id="alice", cost_usd="0.005"),
            _sample_item(user_id="alice", cost_usd="0.005"),
            _sample_item(user_id="bob", cost_usd="0.01"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "user"}, None)

        groups = {r["group"]: r for r in result["results"]}
        assert abs(groups["alice"]["cost_usd"] - 0.01) < 1e-6
        assert abs(groups["bob"]["cost_usd"] - 0.01) < 1e-6

    def test_group_by_date(self):
        qs = self._load_qs()
        items = [
            _sample_item(date="2026-04-01", cost_usd="0.005"),
            _sample_item(date="2026-04-01", cost_usd="0.005"),
            _sample_item(date="2026-04-02", cost_usd="0.01"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "date"}, None)

        groups = {r["group"]: r for r in result["results"]}
        assert abs(groups["2026-04-01"]["cost_usd"] - 0.01) < 1e-6
        assert abs(groups["2026-04-02"]["cost_usd"] - 0.01) < 1e-6

    def test_total_cost_matches_sum_of_results(self):
        qs = self._load_qs()
        items = [
            _sample_item(department="cs-dept", cost_usd="0.012"),
            _sample_item(department="bio-dept", cost_usd="0.008"),
            _sample_item(department="cs-dept", cost_usd="0.005"),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "department"}, None)

        sum_from_results = sum(r["cost_usd"] for r in result["results"])
        assert abs(result["total_cost_usd"] - sum_from_results) < 1e-9

    def test_invalid_group_by_returns_error(self):
        qs = self._load_qs()
        result = qs.handler({"group_by": "invalid"}, None)
        assert "error" in result

    def test_missing_spend_table_returns_error(self):
        qs = self._load_qs(spend_table="")
        result = qs.handler({"group_by": "department"}, None)
        assert "error" in result

    def test_scan_error_returns_error_with_empty_results(self):
        qs = self._load_qs()
        mock_table = MagicMock()
        mock_table.scan.side_effect = Exception("DynamoDB unavailable")
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "department"}, None)
        assert result["results"] == []
        assert "error" in result

    def test_results_include_token_counts(self):
        qs = self._load_qs()
        items = [
            _sample_item(cost_usd="0.01", token_count_in=200, token_count_out=100),
            _sample_item(cost_usd="0.01", token_count_in=300, token_count_out=150),
        ]
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": items}
        with patch.object(qs, "_get_table", return_value=mock_table):
            result = qs.handler({"group_by": "department"}, None)

        r = result["results"][0]
        assert r["token_count_in"] == 500
        assert r["token_count_out"] == 250


# ---------------------------------------------------------------------------
# Budget cap enforcement (router)
# ---------------------------------------------------------------------------

class TestBudgetCapEnforcement:
    _ROUTING_CONFIG = {
        "routing": {
            "analyze": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are an analyst.",
            }
        },
        "defaults": {"max_tokens": 1024, "temperature": 0.7},
    }
    _PROVIDER_FUNCTIONS = {
        "bedrock": "arn:aws:lambda:us-east-1:123456789012:function:qs-provider-bedrock"
    }

    def _success_payload(self):
        return {
            "content": "Analysis result",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

    def test_budget_exceeded_returns_402(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        # Force budget caps loaded
        h._budget_caps = {"cs-dept": 100.0}
        h._budget_caps_loaded = True

        # Patch the bound reference in the handler module (imported via from ... import)
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_query_department_month", return_value=150.0):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 402
        body = json.loads(result["body"])
        assert body["error"] == "budget_exceeded"
        assert body["department"] == "cs-dept"
        assert body["cap_usd"] == 100.0
        assert body["spent_usd"] == 150.0

    def test_budget_under_cap_proceeds(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        h._budget_caps = {"cs-dept": 500.0}
        h._budget_caps_loaded = True

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_query_department_month", return_value=50.0), \
             patch.object(h, "spend_record_write"), \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(self._success_payload())):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200

    def test_budget_fail_open_on_dynamo_error(self):
        """If spend query raises, proceed with the call (fail-open)."""
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        h._budget_caps = {"cs-dept": 100.0}
        h._budget_caps_loaded = True

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_query_department_month",
                          side_effect=Exception("DynamoDB unavailable")), \
             patch.object(h, "spend_record_write"), \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(self._success_payload())):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            result = h.handle_tool_invocation(event)

        # Fail-open: call proceeds despite budget check failure
        assert result["statusCode"] == 200

    def test_no_cap_configured_proceeds(self):
        """No caps configured → all requests proceed."""
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
        )
        h._budget_caps = {}
        h._budget_caps_loaded = True

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_record_write"), \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(self._success_payload())):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200

    def test_department_without_cap_proceeds(self):
        """Department not in caps dict → proceed without check."""
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        h._budget_caps = {"other-dept": 100.0}
        h._budget_caps_loaded = True

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_record_write"), \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(self._success_payload())):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200

    def test_load_budget_caps_fail_open(self):
        """Secrets Manager failure → empty caps, fail-open."""
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        h._budget_caps_loaded = False
        h._budget_caps = {}

        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.side_effect = Exception("Secrets Manager unavailable")
        with patch.object(h, "secrets_client", mock_secrets):
            caps = h._load_budget_caps()

        assert caps == {}

    def test_load_budget_caps_parses_secret(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            budget_caps_secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:caps",
        )
        h._budget_caps_loaded = False
        h._budget_caps = {}

        mock_secrets = MagicMock()
        mock_secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"cs-dept": 500.0, "bio-dept": 250.0})
        }
        with patch.object(h, "secrets_client", mock_secrets):
            caps = h._load_budget_caps()

        assert caps["cs-dept"] == 500.0
        assert caps["bio-dept"] == 250.0


# ---------------------------------------------------------------------------
# Router integration: spend record written on successful call
# ---------------------------------------------------------------------------

class TestRouterSpendWrite:
    _ROUTING_CONFIG = {
        "routing": {
            "analyze": {
                "preferred": ["bedrock/anthropic.claude-sonnet-4-20250514-v1:0"],
                "system_prompt": "You are an analyst.",
            }
        },
        "defaults": {"max_tokens": 1024, "temperature": 0.7},
    }
    _PROVIDER_FUNCTIONS = {
        "bedrock": "arn:aws:lambda:us-east-1:123456789012:function:qs-provider-bedrock"
    }

    def test_spend_record_written_on_success(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
        )
        h._budget_caps = {}
        h._budget_caps_loaded = True

        success = {
            "content": "ok",
            "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 100,
            "output_tokens": 50,
            "guardrail_applied": False,
            "guardrail_blocked": False,
        }

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_record_write") as mock_write, \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(success)):
            event = {
                "tool": "analyze",
                "body": json.dumps({
                    "prompt": "Analyze enrollment trends",
                    "department": "cs-dept",
                    "user_id": "alice",
                }),
            }
            result = h.handle_tool_invocation(event)

        assert result["statusCode"] == 200
        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["department"] == "cs-dept"
        assert call_kwargs["user_id"] == "alice"
        assert call_kwargs["tool"] == "analyze"
        assert call_kwargs["input_tokens"] == 100
        assert call_kwargs["output_tokens"] == 50

    def test_spend_record_not_written_on_provider_error(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
        )
        h._budget_caps = {}
        h._budget_caps_loaded = True

        error_payload = {"errorMessage": "Provider failed", "errorType": "Exception"}

        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_record_write") as mock_write, \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(error_payload)):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Analyze this", "department": "cs-dept"}),
            }
            h.handle_tool_invocation(event)

        mock_write.assert_not_called()

    def test_spend_uses_default_department_when_not_provided(self):
        h = _load_handler(
            self._ROUTING_CONFIG,
            self._PROVIDER_FUNCTIONS,
            {},
            spend_table="qs-router-spend",
        )
        h._budget_caps = {}
        h._budget_caps_loaded = True

        success = {
            "content": "ok", "provider": "bedrock",
            "model": "anthropic.claude-sonnet-4-20250514-v1:0",
            "input_tokens": 10, "output_tokens": 5,
            "guardrail_applied": False, "guardrail_blocked": False,
        }
        with patch.object(h, "_available_providers", {"bedrock"}), \
             patch.object(h, "spend_record_write") as mock_write, \
             patch.object(h.lambda_client, "invoke",
                          return_value=_make_lambda_payload(success)):
            event = {
                "tool": "analyze",
                "body": json.dumps({"prompt": "Hello"}),
            }
            h.handle_tool_invocation(event)

        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["department"] == "default"
        assert call_kwargs["user_id"] == "anonymous"


# ---------------------------------------------------------------------------
# Substrate integration tests (query_spend with real DynamoDB)
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


class TestQuerySpendSubstrate:
    """Integration tests using Substrate for DynamoDB."""

    def _setup_table(self, substrate_url, monkeypatch):
        """Create qs-router-spend table in Substrate."""
        monkeypatch.setenv("AWS_ENDPOINT_URL", substrate_url)
        import boto3
        ddb = boto3.client(
            "dynamodb",
            endpoint_url=substrate_url,
            region_name="us-east-1",
        )
        try:
            ddb.create_table(
                TableName="qs-router-spend",
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        except ddb.exceptions.ResourceInUseException:
            pass

        resource = boto3.resource("dynamodb", endpoint_url=substrate_url, region_name="us-east-1")
        return resource.Table("qs-router-spend")

    @pytest.mark.integration
    def test_spend_record_write_and_query(self, substrate_url, reset_substrate, monkeypatch):
        import provider_interface
        importlib.reload(provider_interface)

        table = self._setup_table(substrate_url, monkeypatch)

        # Reload provider_interface with Substrate endpoint
        provider_interface._dynamo_resource = None
        import provider_interface
        importlib.reload(provider_interface)

        # Write two records
        provider_interface.spend_record_write(
            table_name="qs-router-spend",
            department="cs-dept",
            user_id="alice",
            tool="analyze",
            provider="bedrock",
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            input_tokens=1000,
            output_tokens=500,
        )
        provider_interface.spend_record_write(
            table_name="qs-router-spend",
            department="cs-dept",
            user_id="bob",
            tool="code",
            provider="openai",
            model="gpt-4o",
            input_tokens=2000,
            output_tokens=1000,
        )

        # Scan the table to verify records were written
        resp = table.scan()
        items = resp["Items"]
        assert len(items) == 2
        departments = {i["department"] for i in items}
        assert "cs-dept" in departments

    @pytest.mark.integration
    def test_query_spend_aggregates_correctly(self, substrate_url, reset_substrate, monkeypatch):
        import provider_interface
        importlib.reload(provider_interface)

        self._setup_table(substrate_url, monkeypatch)

        provider_interface._dynamo_resource = None
        import provider_interface
        importlib.reload(provider_interface)

        # Write records
        provider_interface.spend_record_write(
            table_name="qs-router-spend",
            department="bio-dept",
            user_id="carol",
            tool="analyze",
            provider="bedrock",
            model="amazon.nova-pro-v1:0",
            input_tokens=500,
            output_tokens=200,
        )
        provider_interface.spend_record_write(
            table_name="qs-router-spend",
            department="bio-dept",
            user_id="dave",
            tool="summarize",
            provider="openai",
            model="gpt-4o-mini",
            input_tokens=300,
            output_tokens=100,
        )

        # Query spend handler
        qs_handler = _load_query_spend_handler("qs-router-spend")
        qs_handler._dynamo_resource = None

        result = qs_handler.handler({
            "department": "bio-dept",
            "group_by": "department",
        }, None)

        assert result["total_cost_usd"] > 0
        assert len(result["results"]) == 1
        assert result["results"][0]["group"] == "bio-dept"
        assert result["results"][0]["call_count"] == 2
