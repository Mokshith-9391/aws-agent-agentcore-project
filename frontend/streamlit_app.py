"""Separate, credentials-limited Streamlit frontend for AgentCore Runtime."""
import json
import os
import uuid

import streamlit as st

st.set_page_config(page_title="AWS Provisioning Agent")
st.title("AWS Provisioning Agent")
runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN")
principal_id = os.environ.get("FRONTEND_PRINCIPAL_ID")
if not runtime_arn or not principal_id:
    st.error("AGENTCORE_RUNTIME_ARN and FRONTEND_PRINCIPAL_ID must be configured by the trusted frontend host.")
    st.stop()
if "runtime_session_id" not in st.session_state:
    st.session_state.runtime_session_id = f"streamlit-{uuid.uuid4()}"


def invoke(payload: dict) -> dict:
    import boto3
    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION"))
    # The AgentCore SDK passes custom headers through botocore events. The
    # runtime must have X-Principal-Id in its request-header allowlist.
    def add_principal_header(request, **_kwargs):
        request.headers.add_header("X-Principal-Id", principal_id)
    handler = client.meta.events.register_first(
        "before-sign.bedrock-agentcore.InvokeAgentRuntime", add_principal_header
    )
    try:
        result = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn, runtimeSessionId=st.session_state.runtime_session_id,
            runtimeUserId=principal_id, payload=json.dumps(payload).encode(),
        )
    finally:
        client.meta.events.unregister("before-sign.bedrock-agentcore.InvokeAgentRuntime", handler)
    body = result["response"]
    raw = body.read() if hasattr(body, "read") else b"".join(body)
    return json.loads(raw)


if prompt := st.chat_input("Describe the AWS infrastructure you need"):
    with st.spinner("Creating a safe dry-run plan..."):
        result = invoke({"action": "plan", "prompt": prompt})
    st.session_state.last_result = result
    st.session_state.current_plan = result.get("response", {}).get("plan")

result = st.session_state.get("last_result")
if result:
    st.json(result)
    plan = st.session_state.get("current_plan")
    needs_approval = result.get("response", {}).get("requires_approval")
    is_approved = result.get("status") == "APPROVED"
    if plan and needs_approval:
        confirmation = None
        if plan.get("approval_type") == "explicit_confirmation":
            confirmation = st.text_input("Type CONFIRM DELETE for this high-risk plan", type="password")
        if st.button("Approve reviewed plan", type="primary"):
            st.session_state.last_result = invoke({"action": "approve", "plan_id": plan["plan_id"], "confirmation_token": confirmation})
            st.rerun()
    elif plan and (is_approved or not needs_approval) and st.button("Execute approved plan", type="primary"):
        st.session_state.last_result = invoke({"action": "execute", "plan_id": plan["plan_id"]})
        st.rerun()
