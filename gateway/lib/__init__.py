"""Library modules the gateway kept when tools/, agent/ and core/ were removed (S1.7).

- mcp_client     MCP server connections for gateway.mcp_service
- state          SessionDB, the SQLite session store behind gateway.session
- redact         secret redaction for the gateway log formatters
- transcription  speech-to-text for POST /chat/transcribe
- atomic_write   crash-safe JSON/YAML writes (logos_cli.config.save_config)
"""
