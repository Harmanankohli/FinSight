import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent.parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from agent_1_adk.agent import root_agent

__all__ = ["root_agent"]
