"""Allow `python -m sap_mcp [setup|status|logout]` as an alias for the
console script. The MCP host normally launches `python -m sap_mcp.server`."""
from .server import main

if __name__ == "__main__":
    main()
