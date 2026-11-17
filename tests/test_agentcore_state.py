from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from agent.models import ApprovalStatus, OperationCategory, OperationType, ProvisioningPlan
from agent.models import AgentResponse
from agentcore import runtime_entrypoint
from agentcore.runtime_entrypoint import Invocation, _principal
from config.settings import Settings
from history.approval_store import DynamoDBApprovalStore
from history.dynamodb_store import DynamoDBExecutionStore
from security.approvals import ApprovalManager


def _plan() -> ProvisioningPlan:
    plan = ProvisioningPlan(user_request="list buckets", intent="List buckets", operation_type=OperationType.LIST,
                            operation_category=OperationCategory.READ_ONLY, aws_region="ap-south-1")
    plan.plan_hash = plan.compute_plan_fingerprint()
    return plan


def test_approval_state_is_owner_scoped_and_conditional():
    table = Mock()
    store = DynamoDBApprovalStore(Settings(DYNAMODB_HISTORY_TABLE="state"), table=table)
    plan = _plan()
    store.create(plan, "user-1", ApprovalStatus.PENDING)
    token = Mock(model_dump_json=Mock(return_value="token"))
    store.approve(plan.plan_id, "user-1", token)
    assert table.put_item.call_args.kwargs["Item"]["owner_id"] == "user-1"
    assert "#status = :pending" in table.update_item.call_args.kwargs["ConditionExpression"]


def test_history_scan_does_not_use_query_only_option():
    table = Mock(scan=Mock(return_value={"Items": []}))
    store = DynamoDBExecutionStore(Settings(DYNAMODB_HISTORY_TABLE="history"), table=table)
    assert store.list_entries() == []
    assert "ScanIndexForward" not in table.scan.call_args.kwargs


def test_runtime_rejects_missing_principal():
    with pytest.raises(HTTPException) as error:
        _principal(None)
    assert error.value.status_code == 401


def test_runtime_planning_is_forced_to_dry_run_and_persisted(monkeypatch):
    plan = _plan()
    plan.operation_category = OperationCategory.WRITE
    plan.plan_hash = plan.compute_plan_fingerprint()
    agent = Mock(_settings=Settings(AWS_REGION="ap-south-1"), _approval_manager=ApprovalManager())
    agent.process_request.return_value = AgentResponse(message="review", plan=plan, requires_approval=True)
    store = Mock()
    monkeypatch.setattr(runtime_entrypoint, "orchestrator", lambda: agent)
    monkeypatch.setattr(runtime_entrypoint, "approval_store", lambda: store)
    result = runtime_entrypoint.invoke(Invocation(action="plan", prompt="create a bucket"), "user-1")
    assert result["action"] == "plan"
    assert agent.process_request.call_args.kwargs["dry_run"] is True
    assert store.create.call_args.args[1] == "user-1"
