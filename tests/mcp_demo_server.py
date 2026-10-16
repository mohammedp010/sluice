"""A tiny MCP tool server (official SDK, stdio) that the MCP proxy tests put behind Sluice."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def secret() -> str:
    """Something only some keys may call."""
    return "s3cr3t"


@mcp.tool()
def fail() -> str:
    """Always raises: a tool error, not a protocol error."""
    raise ValueError("boom")


if __name__ == "__main__":
    mcp.run()  # stdio
