#!/usr/bin/env python3
"""Deploy the pipelines analyst to Agent Runtime with an Agent Identity, bound to BOTH gateways.

The engine is created once with both bindings: changing a gateway binding later archives every
earlier revision. Modeled on the Agent Gateway codelab's deploy_agent.py (identity_type +
agent_gateway_config), written for this demo.

Usage: agent/.venv/bin/python agent/deploy.py [--update ENGINE_RESOURCE_NAME]
"""

import argparse
import os
import shutil
import stat
import sys
import tempfile
import tomllib

PROJECT = "your-project-id"
REGION = "us-central1"
STAGING = "gs://agent-demo-staging-123456789012"
EGW = f"projects/{PROJECT}/locations/{REGION}/agentGateways/agent-demo-egress-gw"
IGW = f"projects/{PROJECT}/locations/{REGION}/agentGateways/agent-demo-ingress-gw"
MCP_URL = "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp"
HERE = os.path.dirname(os.path.abspath(__file__))

# Workaround from the codelab for source-based ADK deploys: a /code/.venv that points at the
# image's python so the runtime finds the installed packages.
CREATE_VENV = """#!/bin/bash
set -e
PYTHON3=$(which python3)
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
mkdir -p /code/.venv/bin /code/.venv/lib/python${PY_VER}/site-packages
ln -sf "$PYTHON3" /code/.venv/bin/python
ln -sf "$PYTHON3" /code/.venv/bin/python3
cat > /code/.venv/pyvenv.cfg << PYCFG
home = $(dirname $PYTHON3)
include-system-site-packages = true
PYCFG
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", help="existing engine resource name to update instead of creating")
    ap.add_argument("--display-name", default="agent-demo-agent")
    args = ap.parse_args()

    import vertexai
    from vertexai.agent_engines import AdkApp

    vertexai.init(project=PROJECT, location=REGION, staging_bucket=STAGING)
    client = vertexai.Client(project=PROJECT, location=REGION, http_options=dict(api_version="v1beta1"))

    with open(os.path.join(HERE, "pyproject.toml"), "rb") as f:
        requirements = tomllib.load(f)["project"]["dependencies"]

    staging = tempfile.mkdtemp(prefix="agent_demo_deploy_")
    cwd = os.getcwd()
    try:
        shutil.copytree(os.path.join(HERE, "agent"), os.path.join(staging, "agent"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        os.makedirs(os.path.join(staging, "installation_scripts"))
        script = os.path.join(staging, "installation_scripts", "create_venv.sh")
        with open(script, "w") as f:
            f.write(CREATE_VENV)
        os.chmod(script, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        sys.path.insert(0, staging)
        os.chdir(staging)
        os.environ["MCP_URL"] = MCP_URL  # never inherit a shell MCP_URL (env.sh exports the bare service URL)
        from agent.agent import root_agent

        config = {
            "display_name": args.display_name,
            "description": "Pipelines analyst: answers over the Vertex AI Pipelines demo predictions.",
            "staging_bucket": STAGING,
            "requirements": requirements,
            "extra_packages": ["agent", "installation_scripts/create_venv.sh"],
            "build_options": {"installation_scripts": ["installation_scripts/create_venv.sh"]},
            "identity_type": "AGENT_IDENTITY",
            "agent_gateway_config": {
                "agent_to_anywhere_config": {"agent_gateway": EGW},
                "client_to_agent_config": {"agent_gateway": IGW},
            },
            "min_instances": 1,
            "max_instances": 3,
            "env_vars": {
                "GOOGLE_GENAI_USE_VERTEXAI": "True",
                "GOOGLE_API_USE_CLIENT_CERTIFICATE": "false",
                "GOOGLE_API_USE_MTLS_ENDPOINT": "never",
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
                "MCP_URL": MCP_URL,
                "MODEL_NAME": "gemini-3.7-flash",
                "MODEL_LOCATION": "global",
            },
            "labels": {"app": "agent-demo"},
        }
        app = AdkApp(agent=root_agent)
        if args.update:
            engine = client.agent_engines.update(name=args.update, agent=app, config=config)
        else:
            engine = client.agent_engines.create(agent=app, config=config)
    finally:
        os.chdir(cwd)
        shutil.rmtree(staging, ignore_errors=True)
    print("ENGINE", engine.api_resource.name)


if __name__ == "__main__":
    main()
