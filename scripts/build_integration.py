"""从已校验的上游文本基线生成仓库补丁及集成源码，不访问或推送远程仓库"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_release import include


# git 执行受控Git命令，repo为本次新建的集成目录，args是独立参数
# 禁止交互凭证和外部钩子，raw用于保留补丁的原始LF字节
def git(repo: Path, *args: str, raw: bool = False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    result = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false",
        "-c", "core.hooksPath=" + str(repo / ".git/no-hooks"), "-c", "commit.gpgsign=false",
        "-c", "user.name=Task4 Delivery", "-c", "user.email=task4@local.invalid", *args],
        cwd=repo, env=env, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout if raw else result.stdout.decode("utf-8").strip()


# write 保存UTF-8文本，repo是集成目录，name是相对文件名，text是正文
# 文件只写入本次新目录，不覆盖用户原仓库
def write(repo: Path, name: str, text: str) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


# update_repo_docs 对齐仓库现有说明，repo是本次集成副本
# 保留其他分工原有介绍，明确SG90的软件完成范围与实物验证边界
def update_repo_docs(repo: Path) -> None:
    readme = (repo / "README.md").read_text(encoding="utf-8")
    readme = readme.replace("## 当前进度（2026-11-02）", "## 当前进度（分工4集成版，2026-11-04）")
    readme = readme.replace("✅ mock 版 + **正式版已交付**（`device_mcp_server.py`：SG90 舵机走真实 GPIO，其余工具模拟，无硬件自动降级）",
        "软件及离线验收已完成：SG90墙灯硬件PWM、标定、MCP、部署和E2E；真机接线、标定及现场验收待执行")
    start = readme.index("**全链路已跑通（真实大模型）**")
    end = readme.index("> LLM 工具名兼容说明", start)
    readme = readme[:start] + "分工4本次验证：86项单元测试、50项本机链路检查通过；真实网关代码、修复版Agent、正式舵机算法的sim后端和规则模型参与测试，不代表实际GPIO或团队真实模型已验收。其余模块状态沿用原项目自述。\n\n" + readme[end:]
    readme = readme.replace("devices/    分工4：mock_mcp_server.py 全模拟版；device_mcp_server.py 真机版（SG90 舵机 GPIO，其余模拟）",
        "devices/    分工4：wall_light_mcp.py 为SG90入口；all_devices_mcp.py 合并原五工具；原device_mcp_server.py继续提供LED等设备")
    readme = readme.replace("scripts/    e2e_test.py 端到端验收（12 断言）· sync_frontend.sh 前端同步",
        "scripts/    e2e_test.py 明确设备的SG90验收，默认只读；verify.py 软件回归；sync_frontend.sh 前端同步")
    readme = readme.replace("- 端到端验收：`python scripts/e2e_test.py <网关地址> <访问令牌>`",
        "- SG90验收：`python scripts/e2e_test.py --profile profiles/hardware.local.toml`，令牌从TASK4_ACCESS_TOKEN环境变量读取；动作另加--actuate")
    anchor = "## 架构"
    entry = """## 分工4：SG90寝室开关

先看 [完整上机步骤](docs/01_使用步骤.md) 与 [交付及协作](docs/09_最终交付与仓库集成.md)。

- SG90红线接Pi5物理2脚5V，棕/黑线接物理6脚GND，橙/白线接物理32脚BCM12
- 使用RP1 PWM0通道0、50Hz硬件PWM，启动不输出；先空载，再现场标定home/on/off
- 开灯调用light.set({"on":true})，关灯为false；没有灯光反馈，lamp_state保持unknown
- 单舵机只启动wall_light_mcp.py；同时用原设备则只启动all_devices_mcp.py，避免devices.status重名
- 独立上机包及本集成版均已包含Agent错误传播修复；不需要改VPS或前端业务代码
- `python scripts/verify.py` 可复验全部软件检查；实际接线后才做GPIO动作、机构标定、10轮及现场网关联调

原项目PDF/PPT是早期材料，当前SG90接口与验收以本README和链接的Markdown说明为准。

"""
    write(repo, "README.md", readme.replace(anchor, entry + anchor, 1))

    devices = (repo / "devices/README.md").read_text(encoding="utf-8")
    devices = devices.replace("本目录是分工4 的交付物", "SG90更新：先看 [上机步骤](../docs/01_使用步骤.md)；本页原有五工具的GPIO/I2C说明继续保留。wall_light_mcp.py只提供墙灯与状态，all_devices_mcp.py增加墙灯并保留原五工具；两者显式选择--gpio或--sim，硬件失败不降级。\n\n本目录是分工4 的交付物", 1)
    old_start = devices.index("舵机（SG90）注意：")
    devices = devices[:old_start] + """舵机（SG90）已在wall_light_mcp.py实现，使用Pi5 RP1硬件PWM和内核sysfs，不依赖pigpio或RPi.GPIO。GPIO12为物理32脚，原LED继续使用BCM17，二者不冲突。

## 12. SG90交付与验收

| 项目 | 入口与状态 |
|---|---|
| PWM、动作及标定 | task4/pwm_driver.py、servo_control.py、servo_cli.py，软件完成 |
| MCP与兼容 | wall_light_mcp.py、all_devices_mcp.py，仿真通过 |
| 测试与文档 | 86项单元测试、50项本地链路检查、原32项契约均通过 |
| 只读与动作验收 | scripts/e2e_test.py，指定device_id，默认只读，动作另加--actuate |
| 真实设备 | 接线、环境部署、机构标定、实际开关及网关现场验收待执行 |

接线为SG90红线→物理2脚5V、棕/黑线→物理6脚GND、信号→BCM12物理32脚。先空载确认，带负载欠压或重启时切换独立5V、负极共地，不能并联两个电源正极。

完整命令见[使用步骤](../docs/01_使用步骤.md)，对接见[其他分工配合](../docs/02_与其他三人配合.md)。本页第10节未勾选的实际硬件项保留，不能以软件仿真代填。
"""
    write(repo, "devices/README.md", devices)

    contract = (repo / "docs/接口文档/mcp_tools_schema.md").read_text(encoding="utf-8")
    contract = contract.replace("> 版本：v1.1 · 2026-11（v1.0 契约不变，仅补充实现与验收说明）",
        "> 版本：v1.2 · 2026-11-04，原五工具契约保留，SG90新增light.set，当前说明见第7节")
    contract = contract.replace("只用 `gpiozero`（Pi 5 兼容，lgpio 后端），**禁用 RPi.GPIO**",
        "原数字外设用gpiozero/lgpio；SG90使用RP1硬件PWM sysfs，禁用旧RPi.GPIO方案")
    contract += """
## 7. SG90墙灯扩展契约与当前验收

light.set(on:bool)按压指定开/关端后退回并停PWM，不暴露任意角度。只有完成普通单控翘板机构标定后才能执行；GPIO模式失败不会降级仿真。

```json
{"ok":true,"mode":"gpio","requested_on":true,"lamp_state":"unknown","trace":["home","press_on","home"]}
```

devices.status保留原有led/fan/relay/pins及顶层errors，同时新增舵机ready、calibrated、command_count、requested_on及other_devices_ready。合并错误时保留原有键，舵机重名错误加servo前缀；ready仅表示舵机。

单墙灯入口提供light.set和devices.status；all_devices_mcp.py同时保留原五工具，共六个。只运行一个MCP入口，避免devices.status同名。

Agent同时检查MCP协议isError和设备JSON的严格布尔ok。设备失败使直控外层ok=false；聊天在失败处终止，不自动重试按压。

新版scripts/e2e_test.py从TASK4_ACCESS_TOKEN读取PAT，明确device_id，默认只读；--chat必须配--actuate。开/关聊天分别验证布尔方向、恰好一次动作及设备执行计数增加一次，不能只检查模型文字。

软件验收已完成；原第5节的实际GPIO、前端控制和真机E2E仍须接线后验证。全部上机步骤见[SG90指南](../01_使用步骤.md)，协作见[分工接口](../02_与其他三人配合.md)。
"""
    write(repo, "docs/接口文档/mcp_tools_schema.md", contract)

    for name in ("docs/分工4-MCP搭建说明.md", "docs/分工4-工作成果汇总.md"):
        text = (repo / name).read_text(encoding="utf-8")
        text = text.replace("python scripts/e2e_test.py <网关地址> <用户名> <密码>",
            ".venv/bin/python scripts/e2e_test.py --profile profiles/hardware.local.toml --base-url <网关地址> --device-id <设备ID> --actuate --chat")
        text = text.replace("`10 通过, 0 失败`", "新版SG90报告检查全部PASS，真实动作另需现场确认")
        text = text.replace("不修改分工2/3 的业务代码", "不修改VPS业务；Agent两处错误传播修复作为必要接口配套提供")
        text = text.replace("1. **舵机**：Pi 5 上舵机 PWM 需要额外处理——`gpiozero.Servo` 依赖 pigpio，而 pigpio\n   对 Pi 5 支持有限，lgpio 后端也不直接提供舵机所需 PWM；可改用 `lgpio.tx_pwm` 软件\n   PWM 或外接 PWM 驱动板。建议作为进阶项，先把现有 5 个工具跑稳。",
            "1. **舵机**：已新增SG90 RP1硬件PWM、标定和light.set，见上机步骤；真实按压仍待现场测试。原五工具继续保留。")
        banner = "\n> 2026-11-04 SG90更新：86项单元测试、50项本机闭环检查通过，前次两处验收缺口已修复；[当前上机步骤](01_使用步骤.md)、[交付与协作](09_最终交付与仓库集成.md)为最终执行依据。本文下方保留原五工具说明，真实GPIO/网关验收项不能由模拟结果代填。E2E凭证使用TASK4_ACCESS_TOKEN环境变量，不用用户名和密码。\n\n"
        heading, rest = text.split("\n", 1)
        write(repo, name, heading + "\n" + banner + rest)

    intro = (repo / "docs/project_intro.md").read_text(encoding="utf-8")
    intro = intro.replace("servo.angle", "light.set").replace("`servo.set_angle(deg)`", "`light.set(on:bool)`")
    intro = intro.replace('"caps": ["relay", "dht", "servo"]', '"caps": ["relay.set", "dht.read", "light.set", "devices.status"]')
    intro = intro.replace("VPS 缓存离线指令待补发", "墙灯离线拒绝指令，恢复后不补发旧按压")
    intro = intro.replace("统一用 gpiozero（lgpio 后端）", "数字外设用gpiozero/lgpio，SG90用RP1硬件PWM sysfs")
    write(repo, "docs/project_intro.md", intro)
    presentation = (repo / "docs/presentation.html").read_text(encoding="utf-8").replace("relay.set · dht.read · servo…", "relay.set · dht.read · light.set")
    write(repo, "docs/presentation.html", presentation)

    write(repo, "agent/config.example.toml", '''# SG90寝室灯Agent模板，真实部署优先用prepare-agent生成绝对路径配置
# 复制为agent/config.toml并填入真实网关与dev令牌，不能提交私有配置
server_url = "ws://CHANGE_ME"
device_id = "CHANGE_ME"
device_token = "CHANGE_ME"
llm_mode = "proxy"
max_steps = 6
allow_tools = ["light.set", "devices.status"]
confirm_tools = ["relay.set"]
telemetry_tool = ""

# 以下是明确的模拟模式，从agent目录启动才能使用这些相对路径
# 真机需先标定，再用profiles/hardware.local.toml生成服务，不直接删除--sim
[[mcp.servers]]
name = "wall-light"
command = "python3"
args = ["../devices/wall_light_mcp.py", "--sim", "--config", "../configs/servo-simulation.toml"]
env = { PYTHONUTF8 = "1", PYTHONDONTWRITEBYTECODE = "1" }
''')


# main 构建本地集成副本，--output可指定尚不存在的新目录
# 保留本地基线历史供审阅，补丁在全新基线上实测后才交付，不执行远程写入
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "Raspberry-Router_分工4集成")
    args = parser.parse_args()
    repo = args.output.resolve()
    if repo.is_relative_to(ROOT) or not repo.is_relative_to(ROOT.parent):
        raise ValueError("集成输出需放在创客父目录内，并位于当前上机包目录之外")
    if repo.exists():
        raise FileExistsError("集成目录已存在，请使用新的--output，不覆盖已有工作：" + str(repo))
    manifest = json.loads((ROOT / "INTEGRATION_BASE_MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        raw = (ROOT / "upstream_snapshot" / entry["path"]).read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if blob != entry["blob"]:
            raise ValueError("集成基线Git对象不一致：" + entry["path"])
    repo.mkdir(parents=True)
    for entry in manifest["files"]:
        target = repo / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "upstream_snapshot" / entry["path"], target)
    git(repo, "init", "-b", "task4-sg90-integration")
    git(repo, "add", "-f", "--", *[e["path"] for e in manifest["files"]])
    git(repo, "update-index", "--chmod=+x", "scripts/sync_frontend.sh")
    git(repo, "commit", "-m", "Local text baseline for upstream " + manifest["commit"])
    base = git(repo, "rev-parse", "HEAD")

    for source in ROOT.rglob("*"):
        if not include(source):
            continue
        rel = source.relative_to(ROOT)
        if rel.as_posix() in ("文件清单.txt", "integration_patches/分工4_完整集成.patch"):
            continue
        if rel.parts[:2] == ("reference", "历史资料_众灵PM_非SG90"):
            continue
        if rel.as_posix() in ("README.md", ".gitignore"):
            continue
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    write(repo, "分工4_独立包说明.md", (ROOT / "README.md").read_text(encoding="utf-8"))
    original_ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    write(repo, ".gitignore", original_ignore + "\n# SG90私有运行数据\n" + (ROOT / ".gitignore").read_text(encoding="utf-8"))
    for name in ("mcp_client.py", "agent_loop.py"):
        shutil.copyfile(ROOT / "runtime_source/agent/pi_agent" / name, repo / "agent/pi_agent" / name)
    update_repo_docs(repo)
    write(repo, "交付说明_仓库集成.md", """# 分工4仓库集成源码

本目录依据GitHub f9c64f01b4d3d421e862b3acd75f90ac49a44684的54个已核对Git对象的文本文件集成，保留全部运行源码，未附原PDF/PPTX。SG90驱动、配置、工具、Agent必要修复及文档已齐全。

这是可运行的本地集成副本，本地基线提交不是原仓库完整历史，不要直接force-push本目录。向原仓库合并请使用交付上机包内integration_patches/分工4_完整集成.patch；有写权限的负责人检查后应用。

执行python scripts/verify.py可复验软件。实际接线、Pi运行和机构标定按docs/01_使用步骤.md执行；协作见docs/02_与其他三人配合.md。
""")

    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    verify = subprocess.run([sys.executable, str(repo / "scripts/verify.py")], cwd=repo, env=env, timeout=180)
    if verify.returncode:
        raise RuntimeError("仓库集成版软件检查失败，未生成最终补丁")
    # 此报告随成功交付发布，后续补丁检查失败时不会生成源码ZIP
    # 不将补丁自身哈希写入补丁内文件，避免文件自引用
    core_report = {"upstream_commit": manifest["commit"], "base_text_files": len(manifest["files"]),
        "base_git_blobs_verified": True, "integration_software_passed": True,
        "patch_check_passed": True, "patch_apply_files_identical": True,
        "remote_modified": False, "hardware_verified": False}
    write(repo, "reports/integration-validation.json", json.dumps(core_report, ensure_ascii=False, indent=2) + "\n")
    managed = [p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()
        and not any(part in {".git", ".runtime", ".venv", "__pycache__", "private", "evidence"} for part in p.relative_to(repo).parts)
        and ".local." not in p.name and ".private." not in p.name and p.suffix not in (".pyc", ".log", ".zip")]
    managed = sorted(set([*managed, "文件清单.txt"]))
    write(repo, "文件清单.txt", "分工4仓库集成源码文件清单，不含原PDF/PPTX及私有运行数据\n\n" + "\n".join(managed) + "\n")
    git(repo, "add", "-f", "--", *managed)
    patch_path = ROOT.parent / "分工4_GitHub完整补丁.patch"
    patch_path.write_bytes(git(repo, "diff", "--cached", "--binary", "--full-index", base, raw=True))
    changes = git(repo, "diff", "--cached", "--name-only", "-z", base, raw=True).decode("utf-8").strip("\0").split("\0")
    check_repo = ROOT / ".runtime" / ("patch-check-" + uuid.uuid4().hex[:10])
    git(repo, "worktree", "add", "--detach", str(check_repo), base)
    git(check_repo, "apply", "--check", str(patch_path))
    git(check_repo, "apply", str(patch_path))
    mismatches = [name for name in changes if (check_repo / name).read_bytes() != (repo / name).read_bytes()]
    if mismatches:
        raise RuntimeError("补丁应用后的文件不一致：" + repr(mismatches))
    (ROOT / "integration_patches").mkdir(exist_ok=True)
    shutil.copyfile(patch_path, ROOT / "integration_patches/分工4_完整集成.patch")

    archive = ROOT.parent / "分工4_仓库集成源码.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in sorted(managed):
            output.write(repo / name, (Path(repo.name) / name).as_posix())
    for path in (patch_path, archive):
        path.with_suffix(path.suffix + ".sha256.txt").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n", encoding="utf-8")
    report = {"upstream_commit": manifest["commit"], "base_text_files": len(manifest["files"]),
        "base_git_blobs_verified": True, "integration_software_passed": True,
        "patch_check_passed": True, "patch_apply_files_identical": not mismatches,
        "changed_files": len(changes), "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "remote_modified": False, "hardware_verified": False}
    (ROOT / "reports/integration-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print("补丁：" + str(patch_path) + "\n集成源码：" + str(archive))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
