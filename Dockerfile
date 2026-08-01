# Build spec for Glama's server checks: they build this image, start the server,
# and send it an introspection request. Nothing here is needed to *use* the
# server — normal installs go through `uvx ai-rete-rag-mcp`.
#
# The server must start without credentials: tool definitions are static, so
# introspection succeeds with no API key. Only tool *calls* need one, and
# AI_RETE_RAG_API_KEY is supplied at runtime by the MCP client — never baked in.
FROM python:3.12-slim

# stdio transport carries JSON-RPC over stdout; unbuffered avoids a stalled
# handshake if the runtime decides to block-buffer a pipe.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ai_rete_rag_mcp ./ai_rete_rag_mcp

RUN pip install --no-cache-dir .

# Console script from [project.scripts]; speaks MCP over stdio.
ENTRYPOINT ["ai-rete-rag-mcp"]
