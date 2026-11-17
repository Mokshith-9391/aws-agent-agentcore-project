"""Authenticated AgentCore Runtime contract with durable approval state.

AgentCore invokes POST /invocations and GET /ping. The only caller identity
accepted by this API is the principal forwarded by the trusted frontend through
the AgentCore authorizer/header allowlist; AWS profiles and credentials are
never accepted from a caller.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.models import ApprovalStatus, ApprovalToken, ProvisioningPlan
from agent.orchestrator import AgentOrchestrator
from config.settings import reload_settings
from history.approval_store import DynamoDBApprovalStore


class Invocation(BaseModel):
    action: Literal["plan", "approve", "execute"] = "plan"
    prompt: Optional[str] = Field(default=None, max_length=5000)
    plan_id: Optional[str] = None
    confirmation_token: Optional[str] = Field(default=None, max_length=64)
    region: Optional[str] = None
    learning_mode: bool = False


@lru_cache
def orchestrator() -> AgentOrchestrator:
    os.environ["AGENTCORE_RUNTIME"] = "true"
    settings = reload_settings()
    agent = AgentOrchestrator(settings=settings)
    ok, message = agent.initialize()
    if not ok:
        raise RuntimeError(message)
    return agent


@lru_cache
def approval_store() -> DynamoDBApprovalStore:
    return DynamoDBApprovalStore(settings=orchestrator()._settings)


def _principal(principal_id: Optional[str]) -> str:
    if not principal_id or len(principal_id.strip()) < 3:
        raise HTTPException(status_code=401, detail="A trusted principal identity is required.")
    return principal_id.strip()


app = FastAPI(title="AWS Provisioning AgentCore Runtime", docs_url=None, redoc_url=None)


@app.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "Healthy"}


@app.post("/invocations")
def invoke(request: Invocation, x_principal_id: Optional[str] = Header(default=None)) -> dict:
    """Plan, explicitly approve, then execute an immutable stored plan."""
    principal = _principal(x_principal_id)
    try:
        agent = orchestrator()
        store = approval_store()
        if request.action == "plan":
            if not request.prompt or not request.prompt.strip():
                raise HTTPException(status_code=422, detail="prompt is required when action=plan")
            # Planning is always dry-run. No caller can turn an ordinary prompt into
            # an infrastructure mutation.
            response = agent.process_request(
                user_request=request.prompt, region=request.region or agent._settings.AWS_REGION,
                profile="", dry_run=True, learning_mode=request.learning_mode,
                aws_account_id="AgentCore execution role",
            )
            if response.plan:
                approval = agent._approval_manager.determine_approval_requirement(response.plan)
                status = ApprovalStatus.AUTO_APPROVED if not approval["requires_approval"] else ApprovalStatus.PENDING
                store.create(response.plan, principal, status)
            return {"action": "plan", "response": response.model_dump(mode="json")}

        if not request.plan_id:
            raise HTTPException(status_code=422, detail="plan_id is required")
        record = store.get(request.plan_id, principal)
        plan = ProvisioningPlan.model_validate_json(record["plan"])

        if request.action == "approve":
            requirement = agent._approval_manager.determine_approval_requirement(plan)
            if not requirement["requires_approval"]:
                return {"action": "approve", "plan_id": plan.plan_id, "status": ApprovalStatus.AUTO_APPROVED.value}
            token = agent._approval_manager.create_approval_token(plan, request.confirmation_token)
            valid, issues = agent._approval_manager.validate_approval(plan, token, request.confirmation_token)
            if not valid:
                raise HTTPException(status_code=422, detail={"approval_errors": issues})
            store.approve(plan.plan_id, principal, token)
            return {"action": "approve", "plan_id": plan.plan_id, "status": ApprovalStatus.APPROVED.value}

        if not agent._settings.AGENTCORE_EXECUTION_ENABLED:
            raise HTTPException(status_code=403, detail="Live execution is disabled for this runtime.")
        executing = store.mark_executing(plan.plan_id, principal)
        token = ApprovalToken.model_validate_json(executing["approval_token"]) if executing.get("approval_token") else None
        response = agent.execute_approved_plan(
            plan=plan, region=plan.aws_region, profile="", learning_mode=request.learning_mode,
            confirmation_token=request.confirmation_token, approval_token=token,
        )
        store.mark_finished(plan.plan_id, principal, response.model_dump_json())
        return {"action": "execute", "response": response.model_dump(mode="json")}
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Plan not found") from exc
    except Exception as exc:
        # Do not disclose DynamoDB conditions, role permissions, or model failures.
        raise HTTPException(status_code=503, detail="Runtime request could not be completed") from exc
