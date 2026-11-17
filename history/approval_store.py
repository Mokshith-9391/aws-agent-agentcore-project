"""Durable, owner-scoped approval state for AgentCore plan execution.

The application never creates this table. It shares the history table and uses
the ``PK`` values ``PLAN#<plan-id>``. Conditional updates ensure a plan cannot
be approved or executed twice by concurrent AgentCore workers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from agent.models import ApprovalStatus, ApprovalToken, ProvisioningPlan
from config.settings import Settings, get_settings


class DynamoDBApprovalStore:
    def __init__(self, settings: Optional[Settings] = None, table=None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.DYNAMODB_HISTORY_TABLE:
            raise ValueError("DYNAMODB_HISTORY_TABLE is required for durable approvals")
        if table is None:
            import boto3
            table = boto3.resource("dynamodb", region_name=self.settings.AWS_REGION).Table(
                self.settings.DYNAMODB_HISTORY_TABLE
            )
        self.table = table

    @staticmethod
    def _key(plan_id: str) -> dict[str, str]:
        return {"PK": f"PLAN#{plan_id}"}

    def create(self, plan: ProvisioningPlan, owner_id: str, approval_status: ApprovalStatus) -> None:
        self.table.put_item(
            Item={
                **self._key(plan.plan_id), "entity_type": "PLAN", "plan_id": plan.plan_id,
                "owner_id": owner_id, "status": approval_status.value,
                "plan": plan.model_dump_json(by_alias=True),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ConditionExpression="attribute_not_exists(PK)",
        )

    def get(self, plan_id: str, owner_id: str) -> dict[str, Any]:
        item = self.table.get_item(Key=self._key(plan_id), ConsistentRead=True).get("Item")
        if not item or item.get("owner_id") != owner_id:
            raise KeyError("Plan not found for this principal")
        return item

    def approve(self, plan_id: str, owner_id: str, token: ApprovalToken) -> None:
        self.table.update_item(
            Key=self._key(plan_id),
            UpdateExpression="SET #status = :approved, approval_token = :token, approved_at = :at",
            ConditionExpression="owner_id = :owner AND #status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":owner": owner_id, ":pending": ApprovalStatus.PENDING.value,
                ":approved": ApprovalStatus.APPROVED.value,
                ":token": token.model_dump_json(), ":at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def mark_executing(self, plan_id: str, owner_id: str) -> dict[str, Any]:
        response = self.table.update_item(
            Key=self._key(plan_id),
            UpdateExpression="SET #status = :executing",
            ConditionExpression="owner_id = :owner AND (#status = :approved OR #status = :auto)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":owner": owner_id, ":approved": ApprovalStatus.APPROVED.value,
                ":auto": ApprovalStatus.AUTO_APPROVED.value, ":executing": "EXECUTING",
            }, ReturnValues="ALL_NEW",
        )
        return response["Attributes"]

    def mark_finished(self, plan_id: str, owner_id: str, response_json: str) -> None:
        self.table.update_item(
            Key=self._key(plan_id), UpdateExpression="SET #status = :done, execution_response = :response",
            ConditionExpression="owner_id = :owner AND #status = :executing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":owner": owner_id, ":executing": "EXECUTING", ":done": "COMPLETED", ":response": response_json},
        )
