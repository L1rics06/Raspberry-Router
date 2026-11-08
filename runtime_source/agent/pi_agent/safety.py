"""安全闸门：工具白名单 + 参数 schema 强校验 + 高危工具二次确认。"""
from dataclasses import dataclass

import jsonschema


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""


def check_tool(name: str, args: dict, schema: dict | None,
               allow_tools: list, confirm_tools: list, confirmed: bool) -> CheckResult:
    if allow_tools != ["*"] and name not in allow_tools:
        return CheckResult(False, f"工具 {name} 不在白名单内")

    if name in confirm_tools and not confirmed:
        return CheckResult(
            False,
            f"工具 {name} 属于高危操作，需要用户二次确认（手动控制台传 confirmed=true）",
        )

    if schema is not None:
        try:
            jsonschema.validate(args, schema)
        except jsonschema.ValidationError as e:
            return CheckResult(False, f"参数校验失败: {e.message}")

    return CheckResult(True)
