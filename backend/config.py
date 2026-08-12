"""
配置管理模块

负责加载和管理所有应用配置，包括环境变量和自定义请求头
"""

from dotenv import load_dotenv
import json
import os

# 加载环境变量
load_dotenv(dotenv_path="env/.env", override=True)

# ===== 基础配置 =====
# AgentRouter 的 Anthropic-compatible API host。
# 注意：AgentRouter 的文档明确区分网站域名与 API 域名：Anthropic 使用
# https://co.agentrouter.org（不在这里追加 /v1）；OpenAI-compatible 使用
# https://co.agentrouter.org/v1。
TARGET_BASE_URL = os.getenv("API_BASE_URL", "https://co.agentrouter.org").rstrip("/")
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
