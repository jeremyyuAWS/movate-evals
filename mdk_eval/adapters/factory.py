"""Build the adapter from RunConfig."""
from __future__ import annotations

from ..config import AdapterConfig
from .base import AgentAdapter


def build_adapter(cfg: AdapterConfig) -> AgentAdapter:
    target = cfg.target.lower()
    if target == "mock":
        from .mock import MockAdapter

        return MockAdapter()

    if target == "rest":
        from .rest import RESTAdapter

        if not cfg.endpoint:
            raise ValueError("REST adapter requires 'endpoint'")
        return RESTAdapter(
            endpoint=cfg.endpoint,
            headers=cfg.headers,
            api_key_env=cfg.api_key_env,
            request_template=cfg.request_template,
            response_text_path=cfg.response_text_path or "$.response",
            response_tools_path=cfg.response_tools_path,
            response_workflow_path=cfg.response_workflow_path,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
        )

    if target == "openai_compat":
        from .openai_compat import OpenAICompatAdapter

        if not cfg.endpoint:
            raise ValueError("openai_compat adapter requires 'endpoint'")
        return OpenAICompatAdapter(endpoint=cfg.endpoint, api_key_env=cfg.api_key_env or "OPENAI_API_KEY")

    if target == "lyzr":
        from .lyzr import LyzrAdapter

        if not cfg.agent_id:
            raise ValueError("Lyzr adapter requires 'agent_id'")
        return LyzrAdapter(agent_id=cfg.agent_id, endpoint=cfg.endpoint or "https://agent-prod.studio.lyzr.ai/v3/inference/chat/")

    if target == "langgraph":
        from .langgraph import LangGraphAdapter

        if not cfg.graph_import_path:
            raise ValueError("langgraph adapter requires 'graph_import_path'")
        return LangGraphAdapter(graph_import_path=cfg.graph_import_path)

    raise ValueError(f"Unknown adapter target: {cfg.target}")
