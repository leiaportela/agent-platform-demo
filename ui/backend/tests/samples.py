"""SAMPLE records constructed by hand (not recorded from a real run), for edge cases only.

Real records live in fixtures/real_* (see real.py) and the tests that pin field paths use those. Note what
the real records showed after these were written: the inbound gateway writes NO request rows (so
SAMPLE_INBOUND_ALLOWED has no real counterpart), and there is no "empty-status teardown" row after a deny;
the rows without a request URL and authz result are CONNECT tunnels. fixtures/recorded_model_armor_entries.json
are real direct API probes (no client_name), which the UI excludes.
"""

PROJECT = "your-project-id"

SAMPLE_OUTBOUND_ALLOWED = {
    "insertId": "sample-gw-allow-1",
    "logName": f"projects/{PROJECT}/logs/networkservices.googleapis.com%2Fgateway_requests",
    "resource": {"type": "networkservices.googleapis.com/Gateway",
                 "labels": {"location": "us-central1", "gateway_name": "agent-demo-egress-gw"}},
    "labels": {"gateway_name": "agent-demo-egress-gw"},
    "timestamp": "2026-11-26T21:00:01.100Z",
    "severity": "INFO",
    "trace": f"projects/{PROJECT}/traces/aaaabbbbccccdddd0000111122223333",
    "httpRequest": {"requestMethod": "POST", "status": 200, "latency": "0.231s",
                    "requestUrl": "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp"},
    "jsonPayload": {
        "authzPolicyInfo": {"result": "ALLOWED", "policies": [
            {"name": f"projects/{PROJECT}/locations/us-central1/authzPolicies/agent-demo-egress-gw-authz-policy-iap",
             "result": "ALLOWED"}]},
        "agentGatewayInfo": {"mcpInfo": {"method": "tools/call", "parameter": "get_latest_run"}},
    },
}

SAMPLE_OUTBOUND_DENIED = {
    **SAMPLE_OUTBOUND_ALLOWED,
    "insertId": "sample-gw-deny-1",
    "timestamp": "2026-11-26T21:00:02.100Z",
    "severity": "WARNING",
    "httpRequest": {"requestMethod": "POST", "status": 403, "latency": "0.012s",
                    "requestUrl": "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp"},
    "jsonPayload": {
        "authzPolicyInfo": {"result": "DENIED", "policies": [
            {"name": f"projects/{PROJECT}/locations/us-central1/authzPolicies/agent-demo-egress-gw-authz-policy-iap",
             "result": "DENIED"}]},
        "agentGatewayInfo": {"mcpInfo": {"method": "tools/call", "parameter": "get_customer_contact"}},
    },
}

SAMPLE_INBOUND_ALLOWED = {
    "insertId": "sample-igw-1",
    "resource": {"type": "networkservices.googleapis.com/Gateway",
                 "labels": {"location": "us-central1", "gateway_name": "agent-demo-ingress-gw"}},
    "timestamp": "2026-11-26T21:00:00.500Z",
    "trace": f"projects/{PROJECT}/traces/99998888777766665555444433332222",
    "httpRequest": {"requestMethod": "POST", "status": 200,
                    "requestUrl": "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/p/locations/us-central1/reasoningEngines/1:streamQuery?alt=sse"},
    "jsonPayload": {"authzPolicyInfo": {"result": "ALLOWED", "policies": [
        {"name": "agent-demo-ingress-gw-authz-policy-modelarmor"}]}},
}

SAMPLE_UNKNOWN_GATEWAY = {
    "insertId": "sample-other-gw",
    "resource": {"type": "networkservices.googleapis.com/Gateway", "labels": {"location": "us-central1"}},
    "httpRequest": {"status": 200},
    "jsonPayload": {},
}

SAMPLE_MODEL_ARMOR_RESPONSE_CLEAN = {
    "insertId": "sample-ma-resp-1",
    "logName": f"projects/{PROJECT}/logs/modelarmor.googleapis.com%2Fsanitize_operations",
    "resource": {"type": "modelarmor.googleapis.com/SanitizeOperation", "labels": {"template_id": "agent-demo-armor"}},
    "labels": {"modelarmor.googleapis.com/client_name": "AGENT_GATEWAY",
               "modelarmor.googleapis.com/operation_type": "SANITIZE_MODEL_RESPONSE"},
    "timestamp": "2026-11-26T21:00:09.000Z",
    "jsonPayload": {
        "@type": "type.googleapis.com/google.cloud.modelarmor.logging.v1.SanitizeOperationLogEntry",
        "operationType": "SANITIZE_MODEL_RESPONSE",
        "sanitizationResult": {
            "filterMatchState": "NO_MATCH_FOUND",
            "filterResults": {
                "sdp": {"sdpFilterResult": {"inspectResult": {"matchState": "NO_MATCH_FOUND"}}},
                "pi_and_jailbreak": {"piAndJailbreakFilterResult": {"matchState": "NO_MATCH_FOUND"}},
            },
            "invocationResult": "SUCCESS",
        },
    },
}

SAMPLE_ADK_CALL = {
    "author": "pipelines_analyst", "invocation_id": "e-1", "timestamp": 1795294801.2,
    "content": {"role": "model", "parts": [
        {"thought": True, "text": "thinking..."},
        {"function_call": {"id": "c1", "name": "get_customer_contact", "args": {"user_id": 9044}}}]},
    "usage_metadata": {"prompt_token_count": 1200, "candidates_token_count": 20, "total_token_count": 1250,
                       "thoughts_token_count": 30},
}

SAMPLE_ADK_DENIED_RESULT = {
    "author": "pipelines_analyst", "invocation_id": "e-1",
    "content": {"role": "user", "parts": [{"function_response": {"id": "c1", "name": "get_customer_contact",
        "response": {"error": "The 'get_customer_contact' call was denied by the outbound Agent Gateway (HTTP 403)."}}}]},
}

SAMPLE_ADK_ANSWER = {
    "author": "pipelines_analyst", "invocation_id": "e-1",
    "content": {"role": "model", "parts": [{"text": "I can't share contact details."}]},
    "usage_metadata": {"prompt_token_count": 1400, "candidates_token_count": 12, "total_token_count": 1412},
}
