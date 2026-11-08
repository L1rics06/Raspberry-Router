"""同时支持独立上机包及原仓库根目录的源码位置"""
from pathlib import Path

from .common import ROOT


# server_repo 返回包含server的源码根目录，无参数
# 集成进原仓库时使用当前源码，独立包使用附带的固定版本快照
def server_repo() -> Path:
    return ROOT if (ROOT / "server/app/main.py").is_file() else ROOT / "upstream_snapshot"


# agent_repo 返回包含agent的源码根目录，无参数
# 原仓库需已应用错误传播修复，独立包使用附带的修复版
def agent_repo() -> Path:
    return ROOT if (ROOT / "agent/pi_agent/__main__.py").is_file() else ROOT / "runtime_source"


# device_entry 返回原有五个工具的服务入口，无参数
# 墙灯使用BCM12，原有LED等设备的引脚配置保持原接口
def device_entry() -> Path:
    return server_repo() / "devices/device_mcp_server.py"
