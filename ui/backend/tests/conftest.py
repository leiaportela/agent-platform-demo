import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The recorded fixtures carry these placeholder values; real runs get them from infra/env.sh.
os.environ.setdefault("PROJECT", "your-project-id")
os.environ.setdefault("AGENT_ORG", "111111111111")
