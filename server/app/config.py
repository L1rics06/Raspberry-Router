"""网关配置：全部来自环境变量，敏感值绝不写入代码/仓库。

部署时（systemd EnvironmentFile）示例见 server/piagent-server.service。
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("PIAGENT_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "piagent.db"


def _load_or_create_jwt_secret() -> str:
    """JWT 密钥：优先环境变量；否则首次生成并落盘（重启不失效），权限 600。"""
    secret_file = DATA_DIR / ".jwt_secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    s = secrets.token_hex(32)
    secret_file.write_text(s)
    secret_file.chmod(0o600)
    return s


JWT_SECRET = os.getenv("PIAGENT_JWT_SECRET") or _load_or_create_jwt_secret()
JWT_EXPIRE_HOURS = int(os.getenv("PIAGENT_JWT_EXPIRE_HOURS", "168"))  # 默认 7 天

# LLM 出口代理：密钥只存在 VPS，终端（树莓派）不持有
# PIAGENT_LLM_BASE_URL 需带版本路径，如 https://api.deepseek.com/v1
LLM_BASE_URL = os.getenv("PIAGENT_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("PIAGENT_LLM_API_KEY", "")
LLM_MODEL = os.getenv("PIAGENT_LLM_MODEL", "deepseek-chat")

# 心跳与超时（与 docs/接口文档/protocol_pi_vps.md 保持一致）
HEARTBEAT_INTERVAL = 15        # 设备心跳周期（秒），register_ack 会下发
HEARTBEAT_TIMEOUT = 45         # 超过该时长无心跳判定离线
CMD_TIMEOUT_CHAT = 90          # chat 指令（走 LLM 循环）的响应超时
CMD_TIMEOUT_TOOL = 30          # tool 直控指令的响应超时
