# Aura Agent MCP + Databricks External MCP: Integration Gaps

Connecting a Neo4j Aura Agent MCP server to Databricks as an external MCP connection fails due to incompatibilities in both the `databricks-mcp` library and the Databricks Connections API. This document captures the gaps found during the integration attempt and what needs to change for it to work.

## The Blocker: Query Parameters in the MCP URL

Neo4j Aura Agent MCP URLs have this format:

```
https://mcp.neo4j.io/agent?project_id=abc123&agent_id=def456
```

The `project_id` and `agent_id` query parameters are required. Without them, the MCP server returns:

```json
{"error":{"code":-32001,"message":"Missing required query parameters. You can get the agent MCP URL from the Agent context menu in the Aura console."}}
```

Databricks Unity Catalog HTTP connections split URLs into `host` and `base_path`. The `base_path` field rejects query parameters at both the UI and API level:

```
InvalidParameterValue: Invalid base path provided, base path should be something
like /api/resources/v1. Unsupported path: /agent?project_id=...&agent_id=...
```

This means any MCP server whose URL requires query parameters cannot be registered as a Databricks external MCP connection.

## Gap 1: Databricks Connections API Does Not Support Query Parameters

**Component:** Databricks Unity Catalog Connections API (`connections.create`)

**Problem:** The `base_path` option validates against a pattern that rejects `?` and `&` characters. Query parameters are a valid part of HTTP URLs but are not supported.

**Impact:** Any MCP server that routes requests via query parameters (rather than path segments) cannot be registered. Neo4j's Aura Agent MCP is one such server.

**Fix needed:** Accept query parameters in `base_path`, or add a separate `query_params` option that gets appended when the Databricks MCP proxy forwards requests.

## Gap 2: `databricks-mcp` Library Uses GET for OAuth Discovery Probe

**Component:** `databricks_mcp.connector.discover_protected_resource_metadata()` (v0.9.0)

**Problem:** The function sends an HTTP GET to the MCP URL expecting a 401 response with a `WWW-Authenticate` header for OAuth discovery. Neo4j's MCP server only accepts POST on its agent endpoint, returning 405 Method Not Allowed for GET requests.

**Workaround found:** Send a POST with a JSON-RPC `initialize` payload manually, then pass the results to the library's other functions (`discover_authorization_server_metadata`, `perform_dynamic_client_registration`).

**Fix needed:** The probe should try POST (with a minimal MCP initialize payload) if GET returns 405. The MCP specification does not require GET support on the protocol endpoint.

## Gap 3: `databricks-mcp` `create_uc_connection` Strips Query Parameters

**Component:** `databricks_mcp.connector.create_uc_connection()` (v0.9.0)

**Problem:** The function uses `urlparse` to split the MCP URL into components and assigns `parsed.path` to `base_path`. Since `urlparse` separates the query string from the path, query parameters are silently dropped.

```python
# Line 492 in connector.py
base_path = parsed.path or "/"  # Loses ?project_id=...&agent_id=...
```

**Impact:** Even if the Connections API supported query parameters (Gap 1), the library would still lose them.

**Fix needed:** Include `parsed.query` in the base path: `base_path = f"{parsed.path}?{parsed.query}"` when query parameters are present.

## Gap 4: `DatabricksMCPClient` Incompatible with Notebook Event Loops

**Component:** `databricks_mcp.mcp.DatabricksMCPClient.list_tools()` (v0.9.0)

**Problem:** `list_tools()` calls `asyncio.run()`, which fails inside Databricks notebooks (and any environment with a running event loop) with `RuntimeError: asyncio.run() cannot be called from a running event loop`.

**Workaround found:** Install `nest-asyncio` and call `nest_asyncio.apply()` before using the client.

**Fix needed:** Use `asyncio.get_event_loop().run_until_complete()` or detect an existing loop and use it, rather than calling `asyncio.run()`.

## What Worked

The OAuth discovery and authentication flow works correctly when driven manually:

- **`discover-oauth.py`** successfully walks the MCP OAuth discovery chain (RFC 9728 → RFC 8414 → RFC 7591), discovers the Auth0 authorization server, and registers an OAuth client.
- **`test-mcp-auth.py`** completes the full Authorization Code + PKCE flow, exchanges for a token, and verifies the token works against the MCP endpoint.
- The `databricks-mcp` library's `discover_authorization_server_metadata` and `perform_dynamic_client_registration` functions work correctly once the initial POST probe is done manually.
- The Unity Catalog connection is created successfully (with the wrong base path).
- Per-user OAuth login through the Databricks connection page works.

The chain breaks at the last mile: the Databricks MCP proxy sends requests to `/agent` without query parameters, and Neo4j rejects them.

## Summary of Required Changes

| Priority | Component | Gap | Status |
|----------|-----------|-----|--------|
| **Blocker** | Databricks Connections API | Support query parameters in `base_path` | Not supported |
| High | `databricks-mcp` library | POST fallback for OAuth discovery probe | Workaround available |
| High | `databricks-mcp` library | Preserve query parameters in `create_uc_connection` | Workaround available |
| Low | `databricks-mcp` library | `asyncio.run()` in notebook environments | Workaround available |

Until Gap 1 is resolved, Neo4j Aura Agent MCP servers cannot be registered as Databricks external MCP connections.
