FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gpc ./gpc
COPY scripts ./scripts
COPY gpc_mcp_server.py ./
COPY migrations ./migrations

ENV GPC_POSTGRES_DSN=postgresql://gpc:gpcpass@postgres:5432/gpc
ENV GPC_QDRANT_HOST=qdrant
ENV GPC_QDRANT_PORT=6333
ENV GPC_OLLAMA_HOST=http://host.docker.internal:11434
ENV GPC_MCP_HTTP_HOST=0.0.0.0
ENV GPC_MCP_HTTP_PORT=8765
ENV GPC_MCP_HTTP_PATH=/mcp

CMD ["python", "-m", "gpc.cli", "mcp-http", "--host", "0.0.0.0", "--port", "8765"]

