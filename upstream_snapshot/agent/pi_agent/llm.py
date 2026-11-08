"""经 VPS 代理调用大模型（OpenAI 兼容 /chat/completions）。

密钥保存在 VPS 端，树莓派只持有设备 token。
"""
import logging

import httpx

log = logging.getLogger("piagent.llm")


class LLMError(Exception):
    pass


class ProxyLLM:
    def __init__(self, server_url: str, device_id: str, device_token: str,
                 model: str | None = None):
        # ws://host:port → http://host:port
        self.base = server_url.replace("ws://", "http://").replace("wss://", "https://")
        self.base = self.base.rstrip("/")
        self.headers = {
            "X-Device-Id": device_id,
            "Authorization": f"Bearer {device_token}",
        }
        self.model = model

    async def chat(self, messages: list, tools: list | None = None) -> dict:
        """返回 choices[0].message（含 content / tool_calls）。"""
        body: dict = {"messages": messages}
        if self.model:
            body["model"] = self.model
        if tools:
            body["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    f"{self.base}/api/llm/chat/completions",
                    json=body, headers=self.headers,
                )
        except httpx.HTTPError as e:
            raise LLMError(f"无法连接 VPS LLM 代理: {e}") from e

        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text[:200])
            except Exception:
                detail = r.text[:200]
            raise LLMError(f"LLM 代理返回 {r.status_code}: {detail}")
        try:
            return r.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"LLM 响应格式异常: {r.text[:200]}") from e
