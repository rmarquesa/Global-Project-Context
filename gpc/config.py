import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]

load_dotenv(ROOT_DIR / ".env")


PROJECT_NAME = "GPC"
PROJECT_DESCRIPTION = "Global Project Context"

COLLECTION_NAME = "gpc_memory"
VECTOR_SIZE = int(os.getenv("GPC_VECTOR_SIZE", "0"))

EMBEDDING_PROVIDER = os.getenv("GPC_EMBEDDING_PROVIDER", "ollama").strip().lower()
OLLAMA_HOST = os.getenv("GPC_OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_EMBEDDING_MODEL = os.getenv(
    "GPC_OLLAMA_EMBEDDING_MODEL",
    "nomic-embed-text:latest",
)
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("GPC_OLLAMA_TIMEOUT_SECONDS", "60"))

QDRANT_HOST = os.getenv("GPC_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("GPC_QDRANT_PORT", "6333"))

POSTGRES_DSN = os.getenv(
    "GPC_POSTGRES_DSN",
    "postgresql://gpc:gpcpass@localhost:5433/gpc",
)

NEO4J_URI = os.getenv("GPC_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("GPC_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("GPC_NEO4J_PASSWORD", "gpcneo4jpass")

MCP_HTTP_HOST = os.getenv("GPC_MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_PORT = int(os.getenv("GPC_MCP_HTTP_PORT", "8765"))
MCP_HTTP_PATH = os.getenv("GPC_MCP_HTTP_PATH", "/mcp")
