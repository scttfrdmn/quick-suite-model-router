"""
Tests for lambdas/query-spend/handler.py

Covers authorization enforcement (#42):
- Non-admin callers are restricted to their own department and user_id.
- Admin callers (finance_admin / admin group) can query any department/user.
- No claims = direct Lambda invocation → backward-compatible pass-through.
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

# Fake credentials before any boto3 import
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_HANDLER_PATH = os.path.join(REPO_ROOT, "lambdas", "query-spend", "handler.py")


def _load_handler(spend_table="qs-router-spend"):
    alias = f"_qs_handler_{spend_table.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(alias, _HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    with patch.dict(os.environ, {"SPEND_TABLE": spend_table}):
        spec.loader.exec_module(mod)
    return mod


def _make_table_mock(items=None):
    """Return a mock DynamoDB table that returns items on scan."""
    items = items or []
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": items}
    return mock_table


def _claims_event(department=None, user_id=None, groups="", caller_sub="u123",
                  caller_dept="cs", requested_dept=None, requested_uid=None):
    """Build an event with Cognito claims."""
    claims = {
        "sub": caller_sub,
        "custom:department": caller_dept,
        "cognito:groups": groups,
    }
    body = {}
    if requested_dept is not None:
        body["department"] = requested_dept
    if requested_uid is not None:
        body["user_id"] = requested_uid
    event = {
        "requestContext": {"authorizer": {"claims": claims}},
    }
    event.update(body)
    if department is not None:
        event["department"] = department
    if user_id is not None:
        event["user_id"] = user_id
    return event


# ---------------------------------------------------------------------------
# Authorization — non-admin caller restrictions
# ---------------------------------------------------------------------------

class TestNonAdminAuthorization:
    def _handler(self):
        return _load_handler()

    def test_non_admin_restricted_to_own_department(self):
        """Non-admin requesting another department gets authorization error."""
        mod = self._handler()
        event = _claims_event(
            caller_sub="user-cs",
            caller_dept="cs",
            requested_dept="biology",  # not their department
        )
        result = mod.handler(event, None)
        assert "error" in result
        assert "Not authorized" in result["error"]
        assert "biology" in result["error"]
        assert result["results"] == []
        assert result["total_cost_usd"] == 0.0

    def test_non_admin_can_query_own_department(self):
        """Non-admin requesting their own department succeeds."""
        mod = self._handler()
        items = [
            {"department": "cs", "user_id": "user-cs", "tool": "analyze",
             "cost_usd": "0.05", "token_count_in": 100, "token_count_out": 50, "date": "2026-04-01"},
        ]
        event = _claims_event(caller_sub="user-cs", caller_dept="cs", requested_dept="cs")
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result
        assert result["total_cost_usd"] > 0

    def test_non_admin_no_department_uses_own(self):
        """Non-admin with no requested department defaults to their own department."""
        mod = self._handler()
        items = [
            {"department": "cs", "user_id": "user-cs", "tool": "analyze",
             "cost_usd": "0.02", "token_count_in": 50, "token_count_out": 25, "date": "2026-04-01"},
        ]
        event = _claims_event(caller_sub="user-cs", caller_dept="cs")
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result

    def test_non_admin_cannot_query_other_user(self):
        """Non-admin requesting another user_id gets authorization error."""
        mod = self._handler()
        event = _claims_event(
            caller_sub="user-alice",
            caller_dept="cs",
            requested_uid="user-bob",  # not themselves
        )
        result = mod.handler(event, None)
        assert "error" in result
        assert "Not authorized" in result["error"]
        assert "user-bob" in result["error"]

    def test_non_admin_can_query_own_user_id(self):
        """Non-admin requesting their own user_id succeeds."""
        mod = self._handler()
        items = [
            {"department": "cs", "user_id": "user-alice", "tool": "code",
             "cost_usd": "0.10", "token_count_in": 200, "token_count_out": 100, "date": "2026-04-02"},
        ]
        event = _claims_event(caller_sub="user-alice", caller_dept="cs", requested_uid="user-alice")
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result


# ---------------------------------------------------------------------------
# Authorization — admin caller
# ---------------------------------------------------------------------------

class TestAdminAuthorization:
    def _handler(self):
        return _load_handler()

    def test_finance_admin_can_query_any_department(self):
        """finance_admin group member can query any department."""
        mod = self._handler()
        items = [
            {"department": "biology", "user_id": "bio-user", "tool": "research",
             "cost_usd": "0.15", "token_count_in": 300, "token_count_out": 150, "date": "2026-04-01"},
        ]
        event = _claims_event(
            caller_sub="admin-user",
            caller_dept="it",
            groups=["finance_admin"],
            requested_dept="biology",
        )
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result

    def test_admin_group_can_query_any_user(self):
        """admin group member can query any user_id."""
        mod = self._handler()
        items = [
            {"department": "cs", "user_id": "target-user", "tool": "generate",
             "cost_usd": "0.08", "token_count_in": 150, "token_count_out": 75, "date": "2026-04-01"},
        ]
        event = _claims_event(
            caller_sub="admin-user",
            caller_dept="it",
            groups=["admin"],
            requested_uid="target-user",
        )
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result

    def test_groups_as_comma_string_recognized(self):
        """cognito:groups may arrive as a comma-separated string."""
        mod = self._handler()
        items = [
            {"department": "physics", "user_id": "phys-user", "tool": "analyze",
             "cost_usd": "0.05", "token_count_in": 100, "token_count_out": 50, "date": "2026-04-01"},
        ]
        event = _claims_event(
            caller_sub="admin-user",
            caller_dept="it",
            groups="finance_admin,it-staff",
            requested_dept="physics",
        )
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result


# ---------------------------------------------------------------------------
# No-claims path (backward compatibility)
# ---------------------------------------------------------------------------

class TestNoClaims:
    def _handler(self):
        return _load_handler()

    def test_no_claims_passes_through_unrestricted(self):
        """Direct Lambda invocation (no requestContext) is unrestricted."""
        mod = self._handler()
        items = [
            {"department": "biology", "user_id": "bio-user", "tool": "research",
             "cost_usd": "0.20", "token_count_in": 400, "token_count_out": 200, "date": "2026-04-01"},
        ]
        event = {"department": "biology"}
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result
        assert result["total_cost_usd"] > 0

    def test_no_claims_can_query_any_department(self):
        """No-claims path can query any department without restriction."""
        mod = self._handler()
        items = [
            {"department": "chem", "user_id": "chem-user", "tool": "summarize",
             "cost_usd": "0.01", "token_count_in": 50, "token_count_out": 25, "date": "2026-04-01"},
        ]
        event = {"department": "chem", "user_id": "chem-user", "group_by": "tool"}
        with patch.object(mod, "_get_table", return_value=_make_table_mock(items)):
            result = mod.handler(event, None)
        assert "error" not in result

    def test_missing_spend_table_returns_error(self):
        """SPEND_TABLE not configured returns an error (unchanged from pre-auth behavior)."""
        spec = importlib.util.spec_from_file_location("_qs_no_table", _HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_qs_no_table"] = mod
        with patch.dict(os.environ, {"SPEND_TABLE": ""}):
            spec.loader.exec_module(mod)
        result = mod.handler({}, None)
        assert "error" in result
        assert "SPEND_TABLE not configured" in result["error"]

    def test_invalid_group_by_returns_error(self):
        """Invalid group_by value returns error regardless of claims."""
        mod = self._handler()
        event = {"group_by": "invalid_dimension"}
        with patch.object(mod, "_get_table", return_value=_make_table_mock([])):
            result = mod.handler(event, None)
        assert "error" in result
        assert "group_by" in result["error"].lower()
