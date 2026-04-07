"""
Security hardening tests for quick-suite-router v0.10.0 (#44–#50).

Covers:
  #44 — Safe logging (no PII/prompt data in CloudWatch)
  #46 — Context validation to prevent prompt injection
  #47 — Budget caps retry on Secrets Manager error + fail-closed mode
  #49 — Key rotation checker Lambda
  #50 — Content-Type validation (415 on wrong MIME type)
  #45/#48 — CDK stack assertions (Bedrock IAM region, PITR, deletion protection)
"""

import importlib
import importlib.util
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — do NOT put key-rotation-checker on the shared path to avoid
# overwriting the "handler" module name already claimed by the router handler.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_common_path = os.path.join(REPO_ROOT, "lambdas", "common", "python")
_router_path = os.path.join(REPO_ROOT, "lambdas", "router")
_providers_path = os.path.join(REPO_ROOT, "lambdas", "providers")
_spend_path = os.path.join(REPO_ROOT, "lambdas", "query-spend")
_stacks_path = os.path.join(REPO_ROOT, "stacks")

for _p in (_common_path, _router_path, _providers_path, _stacks_path):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _import_router_handler(env_overrides=None):
    """Import (or reimport) lambdas/router/handler.py with given env overrides."""
    env = {
        "ROUTING_CONFIG": "{}",
        "PROVIDER_FUNCTIONS": "{}",
        "PROVIDER_SECRETS": "{}",
        "CACHE_TABLE": "",
        "SPEND_TABLE": "",
        "BUDGET_CAPS_SECRET_ARN": "",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    if env_overrides:
        env.update(env_overrides)
    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(
            "router_handler",
            os.path.join(REPO_ROOT, "lambdas", "router", "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["router_handler"] = mod
        spec.loader.exec_module(mod)
    return mod


def _import_spend_handler():
    """Import lambdas/query-spend/handler.py under a unique name."""
    spec = importlib.util.spec_from_file_location(
        "spend_handler",
        os.path.join(REPO_ROOT, "lambdas", "query-spend", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["spend_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


def _import_rotation_checker(env_overrides=None):
    """Import lambdas/key-rotation-checker/handler.py under a unique name."""
    env = {
        "PROVIDER_SECRET_ARNS": json.dumps([
            "arn:aws:secretsmanager:us-east-1:123:secret:a",
            "arn:aws:secretsmanager:us-east-1:123:secret:b",
        ]),
        "KEY_ROTATION_MAX_AGE_DAYS": "90",
    }
    if env_overrides:
        env.update(env_overrides)
    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location(
            "rotation_checker",
            os.path.join(REPO_ROOT, "lambdas", "key-rotation-checker", "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["rotation_checker"] = mod
        spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# TestSafeLogging (#44)
# ===========================================================================

class TestSafeLogging:
    """Router handler and query-spend handler must not log sensitive fields."""

    def test_router_handler_does_not_log_prompt(self, routing_config, provider_functions):
        """logger.info at entry must not include the prompt or body."""
        h = _import_router_handler({
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
        })

        logged = []

        class _Cap(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        cap = _Cap()
        h.logger.addHandler(cap)

        event = {
            "httpMethod": "POST",
            "path": "/tools/analyze",
            "tool": "analyze",
            "body": json.dumps({"prompt": "SUPER_SECRET_PROMPT", "user_id": "alice@uni.edu"}),
            "headers": {"Content-Type": "application/json"},
        }

        # Patch at the provider-selection level so we don't need full AWS setup
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            h.handler(event, None)

        h.logger.removeHandler(cap)

        assert logged, "Expected at least one log record"
        entry = logged[0]
        assert "SUPER_SECRET_PROMPT" not in entry
        assert "alice@uni.edu" not in entry

    def test_router_handler_logs_tool_and_method(self, routing_config, provider_functions):
        """Entry log must include tool, path, httpMethod — not the body."""
        h = _import_router_handler({
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
        })

        logged = []

        class _Cap(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        cap = _Cap()
        h.logger.addHandler(cap)

        event = {
            "httpMethod": "POST",
            "path": "/tools/analyze",
            "tool": "analyze",
            "body": json.dumps({"prompt": "hello"}),
            "headers": {"Content-Type": "application/json"},
        }

        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            h.handler(event, None)

        h.logger.removeHandler(cap)

        assert logged
        entry_dict = json.loads(logged[0])
        assert entry_dict.get("tool") == "analyze"
        assert entry_dict.get("httpMethod") == "POST"
        assert entry_dict.get("path") == "/tools/analyze"

    def test_query_spend_handler_does_not_log_sensitive_fields(self):
        """query-spend handler entry log must not contain department or user_id values."""
        h = _import_spend_handler()

        logged = []

        class _Cap(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        cap = _Cap()
        h.logger.addHandler(cap)

        with patch.object(h, "SPEND_TABLE", ""):
            h.handler({"department": "biology", "user_id": "alice@uni.edu", "group_by": "department"}, None)

        h.logger.removeHandler(cap)

        assert logged
        entry = logged[0]
        assert "biology" not in entry
        assert "alice@uni.edu" not in entry


# ===========================================================================
# TestContextValidation (#46)
# ===========================================================================

class TestContextValidation:
    """_parse_context must reject invalid/oversized/injected message history."""

    @pytest.fixture(scope="class")
    def parse_context(self):
        import openai_provider
        importlib.reload(openai_provider)
        return openai_provider._parse_context

    @pytest.fixture(scope="class")
    def max_chars(self):
        import openai_provider
        importlib.reload(openai_provider)
        return openai_provider._MAX_MESSAGE_CONTENT_CHARS

    @pytest.fixture(scope="class")
    def max_messages(self):
        import openai_provider
        importlib.reload(openai_provider)
        return openai_provider._MAX_HISTORY_MESSAGES

    def test_accepts_well_formed_history(self, parse_context):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        assert parse_context(json.dumps(history)) == history

    def test_rejects_invalid_role(self, parse_context):
        history = [{"role": "root", "content": "injected"}]
        assert parse_context(json.dumps(history)) is None

    def test_rejects_non_string_content(self, parse_context):
        history = [{"role": "user", "content": 12345}]
        assert parse_context(json.dumps(history)) is None

    def test_rejects_oversized_content(self, parse_context, max_chars):
        big_content = "x" * (max_chars + 1)
        history = [{"role": "user", "content": big_content}]
        assert parse_context(json.dumps(history)) is None

    def test_truncates_overlong_history(self, parse_context, max_messages):
        history = [{"role": "user", "content": f"msg {i}"} for i in range(max_messages + 5)]
        result = parse_context(json.dumps(history))
        assert result is not None
        assert len(result) == max_messages
        assert result[-1]["content"] == f"msg {max_messages + 4}"  # most recent kept

    def test_rejects_non_dict_entry(self, parse_context):
        history = ["not a dict", {"role": "user", "content": "valid"}]
        assert parse_context(json.dumps(history)) is None

    def test_rejects_non_list_json(self, parse_context):
        assert parse_context(json.dumps({"role": "user", "content": "x"})) is None

    def test_returns_none_for_empty_string(self, parse_context):
        assert parse_context("") is None

    def test_returns_none_for_empty_list(self, parse_context):
        assert parse_context("[]") is None

    def test_system_role_accepted(self, parse_context):
        history = [{"role": "system", "content": "Be concise."}]
        assert parse_context(json.dumps(history)) == history

    def test_anthropic_provider_uses_same_validation(self):
        import anthropic_provider
        importlib.reload(anthropic_provider)
        history = [{"role": "admin", "content": "injected"}]
        assert anthropic_provider._parse_context(json.dumps(history)) is None

    def test_gemini_provider_uses_same_validation(self):
        import gemini_provider
        importlib.reload(gemini_provider)
        history = [{"role": "user", "content": "ok"}, {"role": "hacker", "content": "bad"}]
        assert gemini_provider._parse_context(json.dumps(history)) is None


# ===========================================================================
# TestBudgetCapsRetry (#47)
# ===========================================================================

class TestBudgetCapsRetry:
    """Budget caps loader must retry on errors and support fail-closed mode."""

    def test_resets_loaded_flag_on_error(self):
        """After a Secrets Manager failure, _budget_caps_loaded is reset so the next call retries."""
        h = _import_router_handler({
            "BUDGET_CAPS_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:caps",
        })
        h._budget_caps_loaded = False
        h._budget_caps = {}

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception("timeout")
        h.secrets_client = mock_sm

        h._load_budget_caps()

        assert h._budget_caps_loaded is False  # reset — allows retry next invocation

    def test_fail_closed_raises_when_required(self):
        """BUDGET_CAPS_REQUIRED=true causes load failure to raise RuntimeError."""
        h = _import_router_handler({
            "BUDGET_CAPS_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:caps",
        })
        h._budget_caps_loaded = False
        h._budget_caps = {}
        h.BUDGET_CAPS_REQUIRED = True

        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception("SM unavailable")
        h.secrets_client = mock_sm

        with pytest.raises(RuntimeError, match="Budget caps are required"):
            h._load_budget_caps()

    def test_succeeds_normally(self):
        """Successful load sets _budget_caps_loaded=True and returns caps."""
        h = _import_router_handler({
            "BUDGET_CAPS_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123:secret:caps",
        })
        h._budget_caps_loaded = False
        h._budget_caps = {}
        caps = {"biology": 500.0, "cs": 1000.0}

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": json.dumps(caps)}
        h.secrets_client = mock_sm

        result = h._load_budget_caps()

        assert result == caps
        assert h._budget_caps_loaded is True


# ===========================================================================
# TestContentTypeValidation (#50)
# ===========================================================================

class TestContentTypeValidation:
    """handle_tool_invocation must return 415 for non-JSON Content-Type headers."""

    @pytest.fixture
    def h(self, routing_config, provider_functions):
        return _import_router_handler({
            "ROUTING_CONFIG": json.dumps(routing_config),
            "PROVIDER_FUNCTIONS": json.dumps(provider_functions),
            "SPEND_TABLE": "test-spend",
        })

    def test_returns_415_for_text_plain(self, h):
        event = {
            "tool": "analyze",
            "headers": {"Content-Type": "text/plain"},
            "body": "not json",
        }
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 415
        assert "application/json" in json.loads(result["body"])["error"]

    def test_returns_415_for_multipart(self, h):
        event = {
            "tool": "analyze",
            "headers": {"Content-Type": "multipart/form-data"},
            "body": "form-data",
        }
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 415

    def test_proceeds_for_application_json(self, h):
        """application/json content type is accepted (4xx from missing prompt, not 415)."""
        event = {
            "tool": "analyze",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"not_prompt": "x"}),
        }
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] != 415

    def test_proceeds_without_content_type_header(self, h):
        """Missing header is allowed for backward compat (direct Lambda invocations)."""
        event = {
            "tool": "analyze",
            "body": json.dumps({"not_prompt": "x"}),
            # no headers key
        }
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] != 415

    def test_lowercase_content_type_header_rejected(self, h):
        """lowercase 'content-type' header with wrong MIME is rejected."""
        event = {
            "tool": "analyze",
            "headers": {"content-type": "text/xml"},
            "body": "<xml/>",
        }
        with patch.object(h, "select_provider", return_value=(None, None)), \
             patch.object(h, "_load_budget_caps", return_value={}):
            result = h.handle_tool_invocation(event)
        assert result["statusCode"] == 415


# ===========================================================================
# TestKeyRotationChecker (#49)
# ===========================================================================

class TestKeyRotationChecker:
    """key-rotation-checker Lambda emits correct metrics and handles errors gracefully."""

    @pytest.fixture
    def checker(self):
        return _import_rotation_checker()

    def test_returns_overdue_count_when_old(self, checker):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        fresh = datetime.now(timezone.utc) - timedelta(days=10)

        def describe(SecretId):
            return {"LastChangedDate": old if SecretId.endswith(":a") else fresh}

        checker.MAX_AGE_DAYS = 90
        mock_sm = MagicMock()
        mock_sm.describe_secret.side_effect = describe
        checker.sm = mock_sm
        checker.cw = MagicMock()

        result = checker.handler({}, None)
        assert result["overdue"] == 1
        assert result["checked"] == 2

    def test_returns_zero_overdue_when_fresh(self, checker):
        fresh = datetime.now(timezone.utc) - timedelta(days=30)

        checker.MAX_AGE_DAYS = 90
        mock_sm = MagicMock()
        mock_sm.describe_secret.return_value = {"LastChangedDate": fresh}
        checker.sm = mock_sm
        checker.cw = MagicMock()

        result = checker.handler({}, None)
        assert result["overdue"] == 0
        assert result["checked"] == 2

    def test_emits_cloudwatch_metric(self, checker):
        old = datetime.now(timezone.utc) - timedelta(days=200)

        checker.MAX_AGE_DAYS = 90
        mock_sm = MagicMock()
        mock_sm.describe_secret.return_value = {"LastChangedDate": old}
        checker.sm = mock_sm
        mock_cw = MagicMock()
        checker.cw = mock_cw

        checker.handler({}, None)

        mock_cw.put_metric_data.assert_called_once()
        kwargs = mock_cw.put_metric_data.call_args[1]
        assert kwargs["Namespace"] == "QuickSuiteModelRouter"
        assert kwargs["MetricData"][0]["MetricName"] == "KeyRotationOverdue"
        assert kwargs["MetricData"][0]["Value"] == 2  # both secrets old

    def test_skips_inaccessible_secret_gracefully(self, checker):
        """DescribeSecret errors are skipped — handler does not raise."""
        checker.MAX_AGE_DAYS = 90
        mock_sm = MagicMock()
        mock_sm.describe_secret.side_effect = Exception("AccessDenied")
        checker.sm = mock_sm
        checker.cw = MagicMock()

        result = checker.handler({}, None)  # must not raise
        assert result["checked"] == 2
        assert result["overdue"] == 0

    def test_uses_max_age_days_threshold(self, checker):
        """A 5-day-old secret is overdue when MAX_AGE_DAYS=3."""
        recent = datetime.now(timezone.utc) - timedelta(days=5)

        checker.MAX_AGE_DAYS = 3
        mock_sm = MagicMock()
        mock_sm.describe_secret.return_value = {"LastChangedDate": recent}
        checker.sm = mock_sm
        checker.cw = MagicMock()

        result = checker.handler({}, None)
        assert result["overdue"] == 2  # both ARNs are overdue


# ===========================================================================
# TestCdkStack (#45, #48) — CDK assertions
# ===========================================================================

class TestCdkStack:
    """CDK stack assertions for Bedrock IAM scoping, PITR, and deletion protection."""

    @pytest.fixture(scope="class")
    def template(self):
        import aws_cdk as cdk
        from aws_cdk.assertions import Template
        from model_router_stack import ModelRouterStack

        app = cdk.App()
        stack = ModelRouterStack(app, "TestSecurityStack")
        return Template.from_stack(stack)

    def test_bedrock_iam_scoped_to_region(self, template):
        """Bedrock InvokeModel IAM resources must NOT use wildcard region."""
        iam_policies = template.find_resources("AWS::IAM::Policy")
        found_bedrock_policy = False
        for _name, policy in iam_policies.items():
            statements = (
                policy.get("Properties", {})
                .get("PolicyDocument", {})
                .get("Statement", [])
            )
            for stmt in statements:
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if not any("InvokeModel" in a for a in actions):
                    continue
                found_bedrock_policy = True
                resources = stmt.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]
                for r in resources:
                    if isinstance(r, str):
                        assert "bedrock:*::" not in r, f"Wildcard region in Bedrock ARN: {r}"
                        assert "bedrock:*:*:" not in r, f"Wildcard region in Bedrock ARN: {r}"
        assert found_bedrock_policy, "Expected a Bedrock InvokeModel IAM policy"

    def test_spend_table_has_pitr_enabled(self, template):
        """Spend ledger must have PointInTimeRecoveryEnabled=True."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "qs-router-spend",
                "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
            },
        )

    def test_spend_table_has_deletion_protection(self, template):
        """Spend ledger must have DeletionProtectionEnabled=True."""
        template.has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": "qs-router-spend",
                "DeletionProtectionEnabled": True,
            },
        )

    def test_key_rotation_checker_lambda_present(self, template):
        """key-rotation-checker Lambda must exist in the stack."""
        from aws_cdk.assertions import Match
        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"FunctionName": Match.string_like_regexp(".*key-rotation-checker.*")},
        )

    def test_weekly_eventbridge_rule_present(self, template):
        """A scheduled EventBridge rule for key rotation check must exist."""
        from aws_cdk.assertions import Match
        template.has_resource_properties(
            "AWS::Events::Rule",
            {"ScheduleExpression": Match.any_value()},
        )
