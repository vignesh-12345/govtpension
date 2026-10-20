# Deployment Guide — Sewa Setu + MCP Servers

## What was added

| Endpoint | Purpose | Auth |
|---|---|---|
| `POST /mcp/citizen` | Citizen AI agent MCP | Bearer JWT, scope=citizen |
| `POST /mcp/officer` | Block officer AI agent MCP | Bearer JWT, scope=officer |
| `GET  /eval-dashboard` | Eval dashboard | Public |
| `POST /eval-seed` | Seed P2/P3 pre-conditions | Public (run once) |

## Quickstart (local)

```bash
cp .env.example .env          # edit ADMIN_PASSWORD
docker compose up --build
# In a second terminal, seed the eval pre-conditions:
curl -X POST http://localhost:8000/eval-seed
```

Portal: http://localhost:8000  
Eval:   http://localhost:8000/eval-dashboard

## Production deployment (VM / VPS)

```bash
# 1. Clone and enter repo
git clone https://github.com/<you>/sewasetu-assistant
cd sewasetu-assistant

# 2. Create .env
cat > .env <<'ENV'
PUBLIC_BASE_URL=https://sewasetu.yourdomain.in
ADMIN_PASSWORD=choose-a-strong-password
SECRET_KEY=generate-with-python-secrets-token-hex-32
ENV

# 3. Launch
docker compose up --build -d

# 4. Seed eval pre-conditions (idempotent, safe to re-run)
curl -X POST https://sewasetu.yourdomain.in/eval-seed

# 5. Verify MCP servers are up
curl -s -X POST https://sewasetu.yourdomain.in/mcp/citizen \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}}}' | python3 -m json.tool

curl -s -X POST https://sewasetu.yourdomain.in/mcp/officer \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","clientInfo":{"name":"test","version":"1.0"},"capabilities":{}}}' | python3 -m json.tool
```

## Getting tokens for testing

### Citizen token (OAuth flow)
1. Register an MCP client:
```bash
curl -X POST https://sewasetu.yourdomain.in/oauth/register \
  -H "Content-Type: application/json" \
  -d '{"client_name":"Test MCP Client","redirect_uris":["http://localhost:8080/callback"]}'
```
2. Initiate OAuth authorize flow in a browser with scope=citizen, resource=https://sewasetu.yourdomain.in/mcp/citizen

### Officer token (program token — easiest for testing)
1. Log in to admin: https://sewasetu.yourdomain.in/admin  
2. Go to: https://sewasetu.yourdomain.in/admin/tokens  
3. Fill in label="MCP test", audience=https://sewasetu.yourdomain.in, days=90  
4. Copy the token

Then test:
```bash
TOKEN="<the program token>"
curl -X POST https://sewasetu.yourdomain.in/mcp/officer \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## MCP client config (Claude Code)

```json
{
  "mcpServers": {
    "seva-setu-citizen": {
      "url": "https://sewasetu.yourdomain.in/mcp/citizen",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer <citizen_token>"
      }
    },
    "seva-setu-officer": {
      "url": "https://sewasetu.yourdomain.in/mcp/officer",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer <officer_token>"
      }
    }
  }
}
```

## OAuth discovery

The portal advertises its OAuth server metadata at:
```
GET /.well-known/oauth-authorization-server
```
MCP clients that implement RFC 8414 discovery will auto-configure from there.
