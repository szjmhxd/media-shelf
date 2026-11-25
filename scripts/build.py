"""一条命令完成打包：同步版本号 → 生成版本资源 → 调用 PyInstaller。

用法：
    python scripts/build.py
产物：
    dist/MediaShelf.exe
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "scripts" / "version_info.txt"


def read_version() -> str:
    text = (ROOT / "mediashelf" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("config.py 里找不到 APP_VERSION")
    return match.group(1)


def write_version_info(version: str) -> None:
    parts = [int(x) for x in re.split(r"[.\-+]", version) if x.isdigit()][:4]
    parts += [0] * (4 - len(parts))
    quad = ", ".join(str(x) for x in parts)
    VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}),
    prodvers=({quad}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'MediaShelf'),
         StringStruct('FileDescription', '影视库整理工具'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'MediaShelf'),
         StringStruct('LegalCopyright', 'MIT License'),
         StringStruct('OriginalFilename', 'MediaShelf.exe'),
         StringStruct('ProductName', 'MediaShelf'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def main() -> int:
    if not (ROOT / "assets" / "icon.ico").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_icon.py")], check=True)

    version = read_version()
    write_version_info(version)
    print(f"== 版本 {version}，开始打包 ==")

    for stale in (ROOT / "build", ROOT / "dist"):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    # 在本地临时目录构建：网络盘（SMB 映射）上 rmtree 会踩到回收站机制而失败，
    # 本地盘也快得多。产物最后再拷回来。
    work = Path(tempfile.gettempdir()) / "mediashelf-build"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    build_dir = work / "build"
    dist_dir = work / "dist"

    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--workpath",
            str(build_dir),
            "--distpath",
            str(dist_dir),
            str(ROOT / "media-shelf.spec"),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )

    built = dist_dir / "MediaShelf.exe"
    if not built.exists():
        raise SystemExit("打包失败：临时目录里没有 MediaShelf.exe")

    target = ROOT / "dist"
    target.mkdir(parents=True, exist_ok=True)
    final = target / "MediaShelf.exe"
    shutil.copy2(built, final)
    print(f"== 完成 {final} ({final.stat().st_size / 1024 / 1024:.1f} MB) ==")
    print(f"== 构建缓存保留在 {work} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
