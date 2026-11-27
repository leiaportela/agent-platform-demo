"""Run one prompt through the agent locally (no gateways), printing every ADK event as JSON lines.
Used for before/after checks (e.g. get_customer_contact returns data when no gateway is in the way).
Usage: source infra/env.sh && MCP_BEARER=$(gcloud auth print-identity-token) python3 evals/local_query.py "question" """
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
if not os.environ.get("PROJ_ID"):
    sys.exit("PROJ_ID is not set: run `source infra/env.sh` first")
os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ["PROJ_ID"]
os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get("REGION", "us-central1")  # like Agent Runtime; the model client pins global itself

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from agent.agent import root_agent  # noqa: E402


async def main(prompt: str) -> None:
    runner = InMemoryRunner(agent=root_agent, app_name="local")
    session = await runner.session_service.create_session(app_name="local", user_id="local")
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    async for event in runner.run_async(user_id="local", session_id=session.id, new_message=msg):
        print(event.model_dump_json(exclude_none=True), flush=True)


asyncio.run(main(sys.argv[1]))
