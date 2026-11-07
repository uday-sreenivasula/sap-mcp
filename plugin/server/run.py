"""Plugin entry point: put the connector's private packages first, then start the server.

``site.addsitedir`` is used rather than PYTHONPATH because it also processes
``.pth`` files, which pywin32 (a dependency of mcp on Windows) needs.
``--check`` only verifies that everything imports (used by setup.cmd).
"""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.environ.get("SAP_MCP_LIB") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "sap-mcp-plugin", "lib")
#: The server module to start (a build may point this at another server).
SERVER_MODULE = "sap_mcp.server"

os.environ["SAP_MCP_PLUGIN"] = "1"   # lets the server know it runs as the plugin

before = list(sys.path)
site.addsitedir(LIB)
added = [p for p in sys.path if p not in before]
sys.path[:] = [HERE] + added + [p for p in before if p != HERE]

import importlib  # noqa: E402

server = importlib.import_module(SERVER_MODULE)

if "--check" in sys.argv:
    from importlib.metadata import version

    print(SERVER_MODULE, server.__version__, "| mcp", version("mcp"))
    sys.exit(0)

sys.argv = sys.argv[:1]
server.main()
