#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将正式源码和经授权的现场配置构建为身份明确的 Docker 构建目录。"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile


ENTRYPOINT = '''# -*- coding: utf-8 -*-
"""校验外置制品身份后启动采集程序。"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import zipfile

artifact = Path(sys.argv[0]).resolve()
with zipfile.ZipFile(artifact) as package:
    manifest = json.loads(package.read("_news_build.json").decode("utf-8"))
expected_version = os.getenv("NEWS_VERSION")
if expected_version and expected_version != manifest["version"]:
    raise SystemExit("配置版本与程序内部版本不一致，已停止启动")
expected_source = os.getenv("NEWS_SOURCE_SHA256")
if expected_source and expected_source.lower() != manifest["source_sha256"]:
    raise SystemExit("配置源码校验值与程序内部清单不一致，已停止启动")
expected_hash = os.getenv("NEWS_ARTIFACT_SHA256")
if expected_hash and hashlib.sha256(artifact.read_bytes()).hexdigest() != expected_hash.lower():
    raise SystemExit("外置程序的 SHA-256 校验失败，已停止启动")
if len(sys.argv) == 2 and sys.argv[1] == "--version":
    print(manifest["version"])
elif len(sys.argv) == 2 and sys.argv[1] == "--manifest":
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
else:
    runpy.run_module("MY", run_name="__main__")
'''


def sha256(data):
    """计算内容校验值。"""
    return hashlib.sha256(data).hexdigest()


def git_value(root, *arguments):
    """读取当前工作目录的 Git 状态，不创建提交或工作树。"""
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], encoding="utf-8", errors="strict"
    ).strip()


def write_zip_entry(package, name, data):
    """固定 ZIP 元数据，使相同源码可以复现相同制品。"""
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o644 << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    package.writestr(entry, data)


def read_configuration(path):
    """读取私有现场配置，只返回内存数据，解析失败时不回显原始内容。"""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("私有现场配置存在重复键")
            result[key] = value
        return result

    try:
        content = Path(path).read_bytes()
        configuration = json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("私有现场配置无法读取或 JSON 格式无效") from None
    if not isinstance(configuration, dict) or not configuration:
        raise ValueError("私有现场配置必须是非空 JSON 对象")
    for key, value in configuration.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("私有现场配置包含无效的配置键")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("私有现场配置只允许字符串、数字、布尔值或空值")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("私有现场配置不能包含非有限数字")
    for key in ("NEWS_HOST_DATA_DIR", "NEWS_DATA_DIR"):
        value = configuration.get(key)
        if not isinstance(value, str) or not value.startswith("/") or any(character in value for character in "\r\n"):
            raise ValueError("私有现场配置必须填写 Linux 绝对归档目录：" + key)
    for key in ("NEWS_RUN_UID", "NEWS_RUN_GID"):
        value = configuration.get(key, 10001)
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)) or not 0 <= int(value) <= 2147483647:
            raise ValueError("私有现场配置中的运行账户编号无效：" + key)
    return content, configuration


def render_compose(content, version, configuration):
    """只替换公开版本和挂载路径，不把数据库密码或令牌写入 Compose。"""
    text = content.decode("utf-8").replace("__BUILD_VERSION__", version)
    for key, placeholder in (("NEWS_HOST_DATA_DIR", "__HOST_ARCHIVE_DIRECTORY__"),
                             ("NEWS_DATA_DIR", "__CONTAINER_ARCHIVE_DIRECTORY__")):
        text = text.replace(placeholder, json.dumps(configuration[key], ensure_ascii=False))
    return text.encode("utf-8")


def main():
    """快照当前正式源码，校验成功后原子发布交付目录。"""
    if hasattr(os.sys.stdout, "reconfigure"):
        os.sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="构建正式程序制品和 Docker 构建目录")
    parser.add_argument("--version", required=True, help="目标版本，例如 1.0.0；实际版本追加源码身份")
    parser.add_argument("--output", default="dist", help="交付目录的父路径，默认为 dist")
    parser.add_argument("--configuration", help="代码和配置内置镜像时必需的私有 JSON 文件；内容不会回显或进入源码 ZIP")
    arguments = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9][a-zA-Z0-9.-]*)?", arguments.version):
        parser.error("版本必须使用 1.0.0 或 1.0.0-名称 格式")

    root = Path(__file__).resolve().parent
    delivery_tools = {"build_artifact.py", "offline_image.py"}
    sources = sorted(path for path in root.glob("*.py") if path.name not in delivery_tools)
    if not sources or not (root / "MY.py").is_file():
        raise SystemExit("未找到正式入口 MY.py，不能构建制品")
    inputs = sources + [
        root / "build_artifact.py", root / "offline_image.py", root / "requirements.txt", root / "README.md",
        root / "docker-compose.yml", root / "docker-compose.build.yml", root / "dockerfile/news/Dockerfile",
        root / "dockerfile/news/.dockerignore", root / "dockerfile/news/news.env.example",
    ]
    inputs += sorted((root / "dockerfile/news/database").rglob("*.xml"))
    snapshot = {path.relative_to(root).as_posix(): path.read_bytes() for path in inputs}
    configuration_path = None
    configuration = None
    if arguments.configuration:
        configuration_path = Path(arguments.configuration).expanduser().resolve()
        try:
            configuration_bytes, configuration = read_configuration(configuration_path)
        except ValueError as error:
            parser.error(str(error))
        snapshot["dockerfile/news/news.json"] = configuration_bytes
    elif b"COPY news.json " in snapshot["dockerfile/news/Dockerfile"]:
        parser.error("代码和配置内置镜像必须提供 --configuration 私有现场配置")
    for source in sources:
        compile(snapshot[source.name], source.name, "exec")

    fingerprint = hashlib.sha256()
    for name, data in sorted(snapshot.items()):
        fingerprint.update(name.encode("utf-8") + b"\0" + data + b"\0")
    source_hash = fingerprint.hexdigest()
    commit = git_value(root, "rev-parse", "HEAD")
    branch = git_value(root, "branch", "--show-current") or "detached"
    dirty = bool(git_value(root, "status", "--porcelain", "--untracked-files=normal"))
    version = f"{arguments.version}-g{commit[:12]}"
    if dirty:
        version += f"-dirty-{source_hash[:12]}"
    elif configuration is not None:
        # 即使仓库干净，不同现场配置也必须生成不同的正式镜像标签。
        version += f"-configured-{source_hash[:12]}"
    # Docker 标签最多 128 个字符，制品名、内部版本和容器名共用该实际版本。
    if len(version) > 120:
        parser.error("版本名称过长，不能生成一致的 Docker 标签")

    manifest = {
        "version": version,
        "requested_version": arguments.version,
        "git_branch": branch,
        "git_commit": commit,
        "git_dirty": dirty,
        "source_sha256": source_hash,
        "python_minimum": "3.12",
        "entrypoint": "MY.py",
        "sources": {source.name: sha256(snapshot[source.name]) for source in sources},
        "build_inputs": {name: sha256(data) for name, data in sorted(snapshot.items())},
    }
    if configuration is not None:
        manifest["embedded_configuration"] = True
        manifest["configuration_sha256"] = sha256(configuration_bytes)
    output_root = Path(arguments.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / version
    deployment_archive = output_root / f"news-{version}-docker.zip"
    archive_checksum = deployment_archive.with_suffix(".zip.sha256")
    if target.exists() or deployment_archive.exists() or archive_checksum.exists():
        raise SystemExit(f"交付目录已存在，保留原制品不覆盖：{target}")
    temporary = Path(tempfile.mkdtemp(prefix=".news-build-", dir=output_root))
    if temporary.resolve().parent != output_root or target.resolve().parent != output_root:
        raise SystemExit("构建临时目录或发布目录超出交付目录，已停止操作")
    temporary_archive = output_root / (temporary.name + ".zip")
    archive_hash = None
    try:
        module_directory = temporary / "dockerfile/news"
        module_directory.mkdir(parents=True)
        artifact_name = f"news-{version}.pyz"
        artifact = module_directory / artifact_name
        with artifact.open("wb") as output:
            output.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(output, "w") as package:
                write_zip_entry(package, "__main__.py", ENTRYPOINT.encode("utf-8"))
                write_zip_entry(package, "_news_build.json", json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True, indent=2
                ).encode("utf-8"))
                for source in sources:
                    write_zip_entry(package, source.name, snapshot[source.name])
        artifact_hash = sha256(artifact.read_bytes())
        with zipfile.ZipFile(artifact) as package:
            if package.testzip() is not None:
                raise SystemExit("制品压缩内容校验失败")
            internal = json.loads(package.read("_news_build.json").decode("utf-8"))
            if internal != manifest:
                raise SystemExit("制品内部版本清单不一致")
        completed = subprocess.check_output(
            [os.sys.executable, str(artifact), "--version"],
            encoding="utf-8", env={key: value for key, value in os.environ.items()
                                   if key not in {"NEWS_VERSION", "NEWS_ARTIFACT_SHA256", "NEWS_SOURCE_SHA256"}}
        ).strip()
        if completed != version:
            raise SystemExit("制品可执行版本校验失败")

        for name in ("Dockerfile", ".dockerignore"):
            (module_directory / name).write_bytes(snapshot[f"dockerfile/news/{name}"])
        (module_directory / "requirements.txt").write_bytes(snapshot["requirements.txt"])
        if configuration is not None:
            configuration_copy = module_directory / "news.json"
            configuration_copy.write_bytes(configuration_bytes)
            configuration_copy.chmod(0o600)
        for name, data in snapshot.items():
            if name.startswith("dockerfile/news/database/"):
                resource = temporary / name
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_bytes(data)
        compose = snapshot["docker-compose.yml"]
        if configuration is not None:
            compose = render_compose(compose, version, configuration)
        (temporary / "docker-compose.yml").write_bytes(compose)
        (temporary / "docker-compose.build.yml").write_bytes(snapshot["docker-compose.build.yml"])
        for name in delivery_tools:
            (temporary / name).write_bytes(snapshot[name])
        (temporary / "README.md").write_bytes(snapshot["README.md"])
        example = snapshot["dockerfile/news/news.env.example"].decode("utf-8")
        example = example.replace("__BUILD_VERSION__", version)
        example = example.replace("__ARTIFACT_SHA256__", artifact_hash)
        example = example.replace("__SOURCE_SHA256__", source_hash)
        (module_directory / "news.env.example").write_text(example, encoding="utf-8", newline="\n")
        (module_directory / "SHA256SUMS").write_text(
            f"{artifact_hash}  {artifact_name}\n", encoding="ascii", newline="\n"
        )
        manifest["artifact_filename"] = artifact_name
        manifest["artifact_sha256"] = artifact_hash
        (temporary / "BUILD-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8", newline="\n"
        )
        for name, data in snapshot.items():
            current = configuration_path if name == "dockerfile/news/news.json" else root / name
            if current.read_bytes() != data:
                raise SystemExit(f"构建期间源码发生变化，请重新构建：{name}")
        if configuration is None:
            with zipfile.ZipFile(temporary_archive, "w") as package:
                for resource in sorted(temporary.rglob("*")):
                    if resource.is_file():
                        write_zip_entry(package, version + "/" + resource.relative_to(temporary).as_posix(),
                                        resource.read_bytes())
            with zipfile.ZipFile(temporary_archive) as package:
                if package.testzip() is not None:
                    raise SystemExit("部署压缩包内容校验失败")
            archive_hash = sha256(temporary_archive.read_bytes())
        temporary.rename(target)
        if configuration is None:
            temporary_archive.rename(deployment_archive)
            archive_checksum.write_text(
                f"{archive_hash}  {deployment_archive.name}\n", encoding="ascii", newline="\n"
            )
    except BaseException:
        if (temporary.exists() and temporary.resolve().parent == output_root
                and temporary.name.startswith(".news-build-")):
            shutil.rmtree(temporary)
        if temporary_archive.exists() and temporary_archive.resolve().parent == output_root:
            temporary_archive.unlink()
        raise
    print(json.dumps({"directory": str(target), "version": version,
                      "artifact_sha256": artifact_hash, "source_sha256": source_hash,
                      "deployment_archive": str(deployment_archive) if configuration is None else None,
                      "deployment_archive_sha256": archive_hash,
                      "git_dirty": dirty}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
