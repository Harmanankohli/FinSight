"""Unit test conftest — ensures langfuse.observe is a working identity decorator."""

import sys
from unittest.mock import MagicMock


def _identity_decorator(*args, **kwargs):
    """Return an identity decorator — used to stub @observe(), @logged(), etc."""
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return lambda f: f


# Fix langfuse.observe if it was stubbed as a MagicMock by the
# characterization conftest (or any other conftest that loaded earlier).
_langfuse = sys.modules.get("langfuse")
if isinstance(_langfuse, MagicMock):
    _langfuse.observe = _identity_decorator
    _langfuse.decorators = MagicMock()
    _langfuse.decorators.observe = _identity_decorator

# Evict cached mcp_tools.tools submodules so they get re-imported
# with the fixed langfuse.observe identity decorator.
_stale = [k for k in sys.modules if k.startswith("mcp_tools.tools")]
for k in _stale:
    del sys.modules[k]
