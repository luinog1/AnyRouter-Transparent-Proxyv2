"""
Configuração central da aplicação.

Carrega variáveis de ambiente e configurações de headers.
"""

from dotenv import load_dotenv
import json
import os

load_dotenv(dotenv_path="env/.env", override=True)

# ===== Basic configuration =====
# AgentRouter API base URL.
# Keep this configurable so the deployment can override it when necessary.
# The user-facing provider endpoint is https://agentrouter.org.
TARGET_BASE_URL = os.getenv("API_BASE_URL", "https://agentrouter.org").rstrip("/")
PRESERVE_HOST = False

SYSTEM_PROMPT_REPLACEMENT = os.getenv("SYSTEM_PROMPT_REPLACEMENT")
SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST = os.getenv("SYSTEM_PROMPT_BLOCK_INSERT_IF_NOT_EXIST", "false").lower() in ("true", "1", "yes")
CLAUDE_CODE_KEYWORD = "Claude Code"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes")
PORT = int(os.getenv("PORT", "8088"))
ENABLE_DASHBOARD = os.getenv("ENABLE_DASHBOARD", "false").lower() in ("true", "1", "yes")
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "")

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def load_custom_headers() -> dict:
    headers_file = "env/.env.headers.json"
    if not os.path.exists(headers_file):
        print(f"[Custom Headers] Config file '{headers_file}' not found, using default empty dict {{}}")
        return {}
    try:
        with open(headers_file, "r", encoding="utf-8") as f:
            headers = json.load(f)
        if not isinstance(headers, dict):
            print(f"[Custom Headers] Config file content is not a dict, using default empty dict {{}}")
            return {}
        filtered_headers = {k: v for k, v in headers.items() if not k.startswith("__")}
        print(f"[Custom Headers] Successfully loaded {len(filtered_headers)} custom headers")
        return filtered_headers
    except json.JSONDecodeError as e:
        print(f"[Custom Headers] Failed to parse JSON: {e}")
        return {}
    except Exception as e:
        print(f"[Custom Headers] Failed to load '{headers_file}': {e}")
        return {}


CUSTOM_HEADERS = load_custom_headers()
