"""生成脱离开发环境的交付压缩包和文件指纹清单"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".venv", ".runtime", "__pycache__", ".git", "private", "evidence"}


# include 表示判断文件能否交付，path是包目录内的文件
# 排除私有配置、运行日志、现场视频及本机解释器
def include(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    name = path.name.lower()
    return path.is_file() and not any(part in EXCLUDED_DIRS for part in rel.parts) and ".local." not in name and ".private." not in name and path.suffix not in (".pyc", ".log", ".zip") and name != "release_manifest.json"


# main 表示生成交付ZIP，无参数
# 压缩包保存在创客父目录，私有配置不会打包
def main() -> int:
    paths = sorted(path for path in ROOT.rglob("*") if include(path))
    manifest = {"package": ROOT.name, "files": [{"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths]}
    manifest_path = ROOT / "RELEASE_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = ROOT.parent / "分工4_SG90_5V_上机包.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in [*paths, manifest_path]:
            output.write(path, (Path(ROOT.name) / path.relative_to(ROOT)).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256.txt").write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    legacy = ROOT.parent / (ROOT.name + "_交付.zip")
    shutil.copyfile(archive, legacy)
    legacy.with_suffix(".zip.sha256.txt").write_text(digest + "  " + legacy.name + "\n", encoding="utf-8")
    print(f"ZIP: {archive}\nFiles: {len(paths) + 1}\nSHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
