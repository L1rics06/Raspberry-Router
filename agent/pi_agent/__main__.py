"""Agent 入口：python -m pi_agent（在 agent/ 目录，config.toml 同级）。"""
import asyncio
import logging
import sys

from .agent_loop import run_agent_loop  # noqa: F401  (保证模块完整加载)
from .config import ConfigError, load_config
from .llm import ProxyLLM
from .mcp_client import MCPPool
from .mock_llm import mock_chat
from .ws_client import AgentClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("piagent")


class MockLLM:
    """与 ProxyLLM 同接口：chat(messages, tools) -> message dict"""

    def __init__(self):  # noqa: D107
        self.model = None

    async def chat(self, messages, tools=None):
        return await mock_chat(messages, tools)


async def amain() -> None:
    cfg = load_config()
    log.info("PiAgent %s 设备=%s 网关=%s llm_mode=%s",
             __import__("pi_agent").__version__,
             cfg.device_id, cfg.server_url, cfg.llm_mode)

    mcp = MCPPool(cfg.mcp_servers)
    llm = MockLLM() if cfg.llm_mode == "mock" else ProxyLLM(
        cfg.server_url, cfg.device_id, cfg.device_token, cfg.llm_model)
    client = AgentClient(cfg, mcp, llm)

    try:
        await mcp.start()
        await client.run()
    finally:
        # start() 中途异常也要退出已拉起的 stdio 子进程，否则进程清理会死锁
        await mcp.stop()


def main() -> None:
    try:
        asyncio.run(amain())
    except ConfigError as e:
        log.error("配置错误: %s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("退出")


if __name__ == "__main__":
    main()
