"""MCP (Model Context Protocol) client support.

Layout:
  client.py  -- the only module that knows JSON-RPC/MCP wire format
  naming.py  -- pure: qualified tool names + JSON Schema sanitizing
  result.py  -- pure: CallToolResult content blocks -> a plain string
  manager.py -- connection pool, discovery cache and policy
"""
