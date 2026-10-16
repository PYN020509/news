#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在联网机准备 AMD64 离线镜像，在现场只校验或加载既有镜像。"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from urllib.parse import urlsplit

from build_artifact import read_configuration, sha256


PLATFORM = "linux/amd64"
LABEL_KEYS = {
    "version": "org.opencontainers.image.version",
    "artifact_sha256": "news.artifact.sha256",
    "source_sha256": "news.source.sha256",
}

STATIC_RUNTIME_CHECK = r'''
import json
import os
from pathlib import Path
import subprocess
import playwright

metadata = Path(playwright.__file__).parent / "driver/package/browsers.json"
browsers = json.loads(metadata.read_text(encoding="utf-8"))["browsers"]
browser = next(item for item in browsers if item["name"] == "chromium-headless-shell")
root = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
folder = root / ("chromium_headless_shell-" + str(browser["revision"]))
executables = [path for path in folder.rglob("*")
               if path.name in {"chrome-headless-shell", "headless_shell"} and path.is_file()]
assert len(executables) == 1, "Chromium 无头运行时文件缺失或不唯一"
executable = executables[0]
assert os.access(executable, os.X_OK), "Chromium 无头运行时缺少执行权限"
# 只读取可执行文件版本和动态库，不创建浏览器或打开页面。
version = subprocess.check_output([str(executable), "--version"], encoding="utf-8")
assert browser["browserVersion"] in version, "Chromium 文件版本与 Playwright 元数据不一致"
libraries = subprocess.check_output(["ldd", str(executable)], encoding="utf-8")
assert "not found" not in libraries, "Chromium 动态库依赖不完整"
fonts = [path for path in Path("/usr/share/fonts").rglob("NotoSansCJK*.ttc")
         if path.is_file() and path.stat().st_size > 0]
assert fonts, "中文字体文件未安装"
print("Chromium 文件版本、动态库和中文字体静态检查通过")
'''


def file_sha256(path):
    """分块计算大镜像校验值，避免把整个镜像载入内存。"""
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_text(path, value):
    """兼容 Python 3.8，以固定换行保存交付清单。"""
    with Path(path).open("w", encoding="utf-8", newline="\n") as output:
        output.write(value)


def write_json(path, value):
    """输出便于现场审阅的中文 JSON 清单。"""
    write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def checked_path(root, name):
    """只接受交付目录内的普通文件，拒绝路径穿越和符号链接。"""
    if not isinstance(name, str) or "\\" in name:
        raise ValueError("清单文件路径格式错误")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("清单引用了交付目录外的文件")
    path = root / relative
    if not path.is_file() or path.is_symlink() or root not in path.resolve().parents:
        raise ValueError("交付文件缺失或不是目录内的普通文件：" + name)
    return path


def read_release(root):
    """核对外部清单、制品内部身份及全部构建输入。"""
    manifest = json.loads(checked_path(root, "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", version):
        raise ValueError("版本不能用作安全的镜像标签")
    artifact = checked_path(root, "dockerfile/news/" + manifest["artifact_filename"])
    configuration = None
    if manifest.get("embedded_configuration"):
        content, configuration = read_configuration(checked_path(root, "dockerfile/news/news.json"))
        if sha256(content) != manifest["configuration_sha256"]:
            raise ValueError("私有现场配置 SHA-256 与构建清单不一致")
    if file_sha256(artifact) != manifest["artifact_sha256"]:
        raise ValueError("外置程序 SHA-256 与构建清单不一致")
    with zipfile.ZipFile(artifact) as package:
        if package.testzip() is not None:
            raise ValueError("外置程序压缩内容损坏")
        internal = json.loads(package.read("_news_build.json").decode("utf-8"))
        expected = {key: value for key, value in manifest.items()
                    if key not in {"artifact_filename", "artifact_sha256"}}
        if internal != expected:
            raise ValueError("外置程序内部版本和交付清单不一致")
        inputs = {}
        for name, expected_hash in manifest["build_inputs"].items():
            if name in manifest["sources"]:
                content = package.read(name)
                if sha256(content) != manifest["sources"][name]:
                    raise ValueError("制品内业务源码校验失败：" + name)
            elif name == "requirements.txt":
                content = checked_path(root, "dockerfile/news/requirements.txt").read_bytes()
            elif name == "docker-compose.yml":
                content = checked_path(root, name).read_bytes().decode("utf-8")
                if configuration is not None:
                    for key, field, placeholder in (("NEWS_HOST_DATA_DIR", "source", "__HOST_ARCHIVE_DIRECTORY__"),
                                                    ("NEWS_DATA_DIR", "target", "__CONTAINER_ARCHIVE_DIRECTORY__")):
                        value = json.dumps(configuration[key], ensure_ascii=False)
                        content = re.sub(r"(?m)^([ \t]*" + field + r":[ \t]*)" + re.escape(value) + r"(?=\r?$)",
                                         lambda match: match[1] + placeholder, content)
                content = content.replace(version, "__BUILD_VERSION__").encode("utf-8")
            elif name == "dockerfile/news/news.env.example":
                content = checked_path(root, name).read_bytes().decode("utf-8")
                for value, placeholder in ((version, "__BUILD_VERSION__"),
                                           (manifest["artifact_sha256"], "__ARTIFACT_SHA256__"),
                                           (manifest["source_sha256"], "__SOURCE_SHA256__")):
                    content = content.replace(value, placeholder)
                # 源码模板可能使用 Windows 换行；只接受清单中原有的换行身份。
                content = content.encode("utf-8")
                if sha256(content) != expected_hash:
                    content = content.replace(b"\n", b"\r\n")
            else:
                content = checked_path(root, name).read_bytes()
            if sha256(content) != expected_hash:
                raise ValueError("构建输入校验失败：" + name)
            inputs[name] = content
        fingerprint = hashlib.sha256()
        for name, content in sorted(inputs.items()):
            fingerprint.update(name.encode("utf-8") + b"\0" + content + b"\0")
        if fingerprint.hexdigest() != manifest["source_sha256"]:
            raise ValueError("实际构建输入与源码身份不一致")
    if manifest.get("external_configuration"):
        if manifest.get("embedded_configuration"):
            raise ValueError("现场配置不能同时标记为外置和镜像内置")
        if checked_path(root, ".env.example").read_bytes() != checked_path(
                root, "dockerfile/news/news.env.example").read_bytes():
            raise ValueError("交付根目录的现场配置模板与模块模板不一致")
    return manifest, artifact


def docker(*arguments):
    """执行明确的 Docker 子命令，现场操作不构建、不拉取镜像。"""
    result = subprocess.run(["docker", *arguments], check=True, encoding="utf-8",
                            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.stdout.strip()


def inspect_loaded(manifest, expected_id=None, expected_user=None):
    """严格核对本地镜像的平台、标签、身份和镜像配置摘要。"""
    image_name = "news-runtime:" + manifest["version"]
    inspection = json.loads(docker("image", "inspect", image_name))
    if len(inspection) != 1:
        raise ValueError("本地镜像检查没有返回唯一结果")
    actual = inspection[0]
    if actual.get("Os") != "linux" or actual.get("Architecture") != "amd64":
        raise ValueError("本地镜像平台不是 linux/amd64")
    if image_name not in (actual.get("RepoTags") or []):
        raise ValueError("本地镜像标签与交付版本不一致")
    labels = actual.get("Config", {}).get("Labels") or {}
    if expected_user is not None and actual.get("Config", {}).get("User") != expected_user:
        raise ValueError("本地镜像运行账户与经授权的现场配置不一致")
    for name, label in LABEL_KEYS.items():
        if labels.get(label) != manifest[name]:
            raise ValueError("本地镜像身份标签不一致：" + label)
    if expected_id:
        accepted = set(manifest.get("image_manifest_digests", []))
        accepted.update((expected_id, manifest.get("image_config_digest")))
        if actual.get("Id") not in accepted:
            raise ValueError("本地镜像 ID 与离线镜像已验证摘要不一致")
        if (actual.get("RootFS", {}).get("Layers") or []) != manifest["rootfs_diff_ids"]:
            raise ValueError("本地镜像层身份与离线镜像不一致")
        for field, name in (("Entrypoint", "image_entrypoint"), ("Cmd", "image_cmd"),
                            ("User", "image_user"), ("WorkingDir", "image_working_dir")):
            if actual.get("Config", {}).get(field) != manifest[name]:
                raise ValueError("本地镜像运行配置与离线镜像不一致：" + field)
    return actual["Id"]


def reject_tag_collision(manifest, expected_id=None):
    """加载前拒绝覆盖同名但实际身份不同的既有镜像。"""
    result = subprocess.run(["docker", "image", "ls", "--quiet", "--no-trunc",
                             "news-runtime:" + manifest["version"]], check=True,
                            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.stdout.strip():
        inspect_loaded(manifest, expected_id)


def inspect_image_tar(path, manifest):
    """直接检查 docker save 归档内部身份，无需加载或运行镜像。"""
    def read_member(archive, name):
        member = archive.getmember(name)
        if not member.isfile() or member.size > 10 * 1024 * 1024:
            raise ValueError("镜像身份文件异常：" + name)
        with archive.extractfile(member) as source:
            return source.read()

    with tarfile.open(path, "r:*") as archive:
        records = json.loads(read_member(archive, "manifest.json").decode("utf-8"))
        expected_tag = "news-runtime:" + manifest["version"]
        if len(records) != 1 or records[0].get("RepoTags") != [expected_tag]:
            raise ValueError("镜像归档必须只包含清单指定的版本标签")
        record = records[0]
        config_bytes = read_member(archive, record["Config"])
        config = json.loads(config_bytes.decode("utf-8"))
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("离线镜像实际平台不是 linux/amd64")
        labels = config.get("config", {}).get("Labels") or {}
        for name, label in LABEL_KEYS.items():
            if labels.get(label) != manifest[name]:
                raise ValueError("离线镜像内部身份标签不一致：" + label)
        layers = record.get("Layers") or []
        if not layers or len(layers) != len(config.get("rootfs", {}).get("diff_ids") or []):
            raise ValueError("离线镜像层清单不完整")
        layer_hashes = []
        diff_ids = config["rootfs"]["diff_ids"]
        for name, diff_id in zip(layers, diff_ids):
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError("离线镜像层文件缺失：" + name)
            digest = hashlib.sha256()
            with archive.extractfile(member) as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            blob_digest = "sha256:" + digest.hexdigest()
            if name.startswith("blobs/sha256/") and name.rsplit("/", 1)[1] != digest.hexdigest():
                raise ValueError("镜像层 blob 摘要与内容不一致：" + name)
            layer_hashes.append(blob_digest)
            with archive.extractfile(member) as source:
                signature = source.read(2)
                source.seek(0)
                expanded = gzip.GzipFile(fileobj=source) if signature == b"\x1f\x8b" else source
                digest = hashlib.sha256()
                for block in iter(lambda: expanded.read(1024 * 1024), b""):
                    digest.update(block)
                if "sha256:" + digest.hexdigest() != diff_id:
                    raise ValueError("镜像层实际内容与 RootFS diffID 不一致：" + name)
                if expanded is not source:
                    expanded.close()
        config_digest = "sha256:" + sha256(config_bytes)
        if record["Config"].startswith("blobs/sha256/") and not record["Config"].endswith(config_digest[7:]):
            raise ValueError("镜像配置 blob 摘要与内容不一致")
        verified = set()
        visited = set()

        def inspect_descriptor(descriptor):
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("OCI 镜像描述摘要格式错误")
            if digest in visited:
                return digest in verified
            visited.add(digest)
            content = read_member(archive, "blobs/sha256/" + digest[7:])
            if "sha256:" + sha256(content) != digest or len(content) != descriptor["size"]:
                raise ValueError("OCI 镜像描述与实际内容不一致")
            document = json.loads(content.decode("utf-8"))
            if "manifests" in document:
                matched = any(inspect_descriptor(item) for item in document["manifests"])
            else:
                matched = (document.get("config", {}).get("digest") == config_digest)
                if matched and [item["digest"] for item in document.get("layers", [])] != layer_hashes:
                    raise ValueError("OCI 镜像层顺序或身份与归档不一致")
            if matched:
                verified.add(digest)
            return matched

        if "index.json" in archive.getnames():
            index_bytes = read_member(archive, "index.json")
            index = json.loads(index_bytes.decode("utf-8"))
            matched = False
            for descriptor in index.get("manifests", []):
                # 签名和证明可以引用其他配置，但只接受连接到目标 AMD64 配置的镜像摘要。
                matched = inspect_descriptor(descriptor) or matched
            if not matched:
                raise ValueError("OCI 索引未连接到已核验的目标镜像配置")
            verified.add("sha256:" + sha256(index_bytes))
        return {
            "image_config_digest": config_digest,
            "image_manifest_digests": sorted(verified),
            "rootfs_diff_ids": diff_ids,
            "image_entrypoint": config.get("config", {}).get("Entrypoint"),
            "image_cmd": config.get("config", {}).get("Cmd"),
            "image_user": config.get("config", {}).get("User"),
            "image_working_dir": config.get("config", {}).get("WorkingDir"),
        }


def verify(root, loaded=False):
    """先校验所有交付文件，再核对镜像归档内部及可选本地镜像。"""
    sums = checked_path(root, "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    listed = set()
    for line in sums:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] in listed:
            raise ValueError("SHA256SUMS 格式错误或存在重复文件")
        listed.add(match[2])
        if file_sha256(checked_path(root, match[2])) != match[1]:
            raise ValueError("离线交付文件校验失败：" + match[2])
    manifest, artifact = read_release(root)
    offline = json.loads(checked_path(root, "OFFLINE-MANIFEST.json").read_text(encoding="utf-8"))
    for name in ("version", "artifact_filename", "artifact_sha256", "source_sha256"):
        if offline[name] != manifest[name]:
            raise ValueError("离线清单与构建清单不一致：" + name)
    for name in ("embedded_configuration", "external_configuration", "configuration_sha256"):
        if offline.get(name) != manifest.get(name):
            raise ValueError("离线清单与构建配置身份不一致：" + name)
    if offline["platform"] != PLATFORM or offline["image"] != "news-runtime:" + manifest["version"]:
        raise ValueError("离线清单平台或标签不一致")
    image_tar = checked_path(root, offline["image_tar"])
    if file_sha256(image_tar) != offline["image_tar_sha256"]:
        raise ValueError("离线镜像归档 SHA-256 不一致")
    metadata = inspect_image_tar(image_tar, manifest)
    for name, value in metadata.items():
        if offline.get(name) != value:
            raise ValueError("离线镜像内部身份与交付清单不一致：" + name)
    if offline["image_id"] not in {metadata["image_config_digest"], *metadata["image_manifest_digests"]}:
        raise ValueError("构建镜像 ID 未连接到归档中的已验证配置")
    required = {"BUILD-MANIFEST.json", "OFFLINE-MANIFEST.json", offline["image_tar"],
                "dockerfile/news/" + manifest["artifact_filename"], "offline_image.py", "build_artifact.py"}
    if manifest.get("external_configuration"):
        required.add(".env.example")
    if not required.issubset(listed):
        raise ValueError("SHA256SUMS 缺少必要交付文件")
    if loaded:
        inspect_loaded(offline, offline["image_id"])
    return offline


def test_runtime(manifest, artifact):
    """在断网容器中检查入口和固定依赖；不打开浏览器或访问业务数据。"""
    image_name = "news-runtime:" + manifest["version"]
    common = ["run", "--rm", "--pull", "never", "--network", "none", "--read-only",
              "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m"]
    program = common[:]
    if not manifest.get("embedded_configuration") and not manifest.get("external_configuration"):
        program += ["--mount", "type=bind,source=" + str(artifact) + ",target=/app/news.pyz,readonly"]
    # 帮助检查不依赖尚未填写的现场配置，也不会启动采集或访问数据库。
    if manifest.get("external_configuration"):
        program += ["-e", "NEWS_CONFIG_FILE="]
    for name, value in (("NEWS_VERSION", manifest["version"]),
                        ("NEWS_ARTIFACT_SHA256", manifest["artifact_sha256"]),
                        ("NEWS_SOURCE_SHA256", manifest["source_sha256"])):
        program += ["-e", name + "=" + value]
    if docker(*program, image_name, "--version") != manifest["version"]:
        raise ValueError("镜像执行的外置制品版本不一致")
    if not docker(*program, image_name, "--help"):
        raise ValueError("镜像入口帮助检查无输出")
    docker(*common, "--entrypoint", "python", image_name, "-m", "pip", "check")
    docker(*common, "--entrypoint", "python", image_name, "-c",
           "import pymysql,requests,lxml.etree,urllib3,playwright.sync_api,tzdata,cryptography;"
           "print('依赖导入检查通过')")
    docker(*common, "--entrypoint", "python", image_name, "-c", STATIC_RUNTIME_CHECK)
    if manifest.get("external_configuration"):
        docker(*common, "--entrypoint", "python", image_name, "-c",
               "import hashlib,os;from pathlib import Path;"
               "assert hashlib.sha256(Path('/app/news.pyz').read_bytes()).hexdigest()==os.environ['NEWS_ARTIFACT_SHA256'],"
               "'镜像内程序制品校验失败';"
               "assert not any(Path('/app/config').iterdir()),'镜像不应包含现场配置';"
               "print('镜像内程序身份和外置配置边界校验通过')")
    if manifest.get("embedded_configuration"):
        docker(*common, "--entrypoint", "python", image_name, "-c",
               "import hashlib,json,os;from pathlib import Path;"
               "assert hashlib.sha256(Path('/app/news.pyz').read_bytes()).hexdigest()==os.environ['NEWS_ARTIFACT_SHA256'],"
               "'镜像内程序制品校验失败';"
               "assert hashlib.sha256(Path('/app/config/news.json').read_bytes()).hexdigest()==" +
               repr(manifest["configuration_sha256"]) + ",'镜像内现场配置校验失败';"
               "print('镜像内程序和现场配置身份校验通过')")


def copy_release(source, target, manifest):
    """复制内部构建审计白名单；含私有配置的目录禁止额外生成源码 ZIP。"""
    names = ["BUILD-MANIFEST.json", "README.md", "docker-compose.yml", "docker-compose.build.yml",
             "offline_image.py", "build_artifact.py", "dockerfile/news/Dockerfile",
             "dockerfile/news/.dockerignore", "dockerfile/news/requirements.txt",
             "dockerfile/news/news.env.example", "dockerfile/news/SHA256SUMS",
             "dockerfile/news/" + manifest["artifact_filename"]]
    if manifest.get("embedded_configuration"):
        names.append("dockerfile/news/news.json")
    if manifest.get("external_configuration"):
        names.append(".env.example")
    names += [name for name in manifest["build_inputs"] if name.startswith("dockerfile/news/database/")]
    for name in names:
        incoming = checked_path(source, name)
        outgoing = target / name
        outgoing.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(incoming, outgoing)
        if name == "dockerfile/news/news.json":
            outgoing.chmod(0o600)


def write_shell_scripts(target, manifest):
    """生成纯 Shell 现场入口，固定真实镜像身份且不执行现场配置内容。"""
    identities = (
        ("IMAGE", manifest["image"]), ("IMAGE_ID", manifest["image_id"]),
        ("IMAGE_TAR", manifest["image_tar"]), ("VERSION", manifest["version"]),
        ("ARTIFACT_SHA", manifest["artifact_sha256"]), ("SOURCE_SHA", manifest["source_sha256"]),
        ("IMAGE_IDS", " ".join(sorted({manifest["image_id"], manifest["image_config_digest"],
                                       *manifest["image_manifest_digests"]}))),
        ("ROOTFS", " ".join(manifest["rootfs_diff_ids"]) + " "),
        ("ENTRYPOINT", json.dumps(manifest["image_entrypoint"], separators=(",", ":"))),
        ("CMD", json.dumps(manifest["image_cmd"], separators=(",", ":"))),
        ("RUN_USER", manifest["image_user"] or ""),
        ("WORKING_DIR", manifest["image_working_dir"] or ""))
    identity = "\n".join(key + "=" + shlex.quote(value) for key, value in identities)
    common = r'''#!/usr/bin/env bash
# 仅核验并加载本地正式镜像；不构建、不拉取、不打印现场凭据。
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RELEASE_DIR=${1:-$SCRIPT_DIR}
cd -- "$RELEASE_DIR"
''' + identity + r'''
fail() { printf '%s\n' "$1" >&2; exit 1; }
[ -f SHA256SUMS ] || fail '缺少交付校验清单'
sha256sum --check --strict SHA256SUMS || fail '离线交付文件校验失败'
ENGINE=$(docker info --format '{{.OSType}} {{.Architecture}}') || fail 'Docker 服务不可用'
case "$ENGINE" in
  'linux amd64'|'linux x86_64') ;;
  *) fail 'Docker 服务端必须使用 Linux AMD64 平台' ;;
esac
verify_loaded() {
  ACTUAL_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE") || fail '尚未加载目标镜像'
  case " $IMAGE_IDS " in
    *" $ACTUAL_ID "*) ;;
    *) fail '镜像 ID 不属于已核验的配置或镜像摘要' ;;
  esac
  [ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE")" = 'linux/amd64' ] || fail '镜像实际平台不是 Linux AMD64'
  [ "$(docker image inspect --format '{{range .RootFS.Layers}}{{.}} {{end}}' "$IMAGE")" = "$ROOTFS" ] || fail '镜像层身份与交付不一致'
  [ "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$IMAGE")" = "$ENTRYPOINT" ] || fail '镜像入口与交付不一致'
  [ "$(docker image inspect --format '{{json .Config.Cmd}}' "$IMAGE")" = "$CMD" ] || fail '镜像默认参数与交付不一致'
  [ "$(docker image inspect --format '{{.Config.User}}' "$IMAGE")" = "$RUN_USER" ] || fail '镜像运行用户与交付不一致'
  [ "$(docker image inspect --format '{{.Config.WorkingDir}}' "$IMAGE")" = "$WORKING_DIR" ] || fail '镜像工作目录与交付不一致'
  [ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$IMAGE")" = "$VERSION" ] || fail '镜像版本标签不一致'
  [ "$(docker image inspect --format '{{index .Config.Labels "news.artifact.sha256"}}' "$IMAGE")" = "$ARTIFACT_SHA" ] || fail '镜像外置制品标签不一致'
  [ "$(docker image inspect --format '{{index .Config.Labels "news.source.sha256"}}' "$IMAGE")" = "$SOURCE_SHA" ] || fail '镜像源码标签不一致'
}
verify_config() {
  [ -z "${COMPOSE_ENV_FILES-}" ] || fail '请清除 COMPOSE_ENV_FILES，确保使用本目录现场配置'
  [ -z "${COMPOSE_DISABLE_ENV_FILE-}" ] || fail '请清除 COMPOSE_DISABLE_ENV_FILE，确保读取本目录现场配置'
  # Compose 的进程环境优先于 .env，任何覆盖身份不一致都停止启动。
  [ "${NEWS_VERSION-$VERSION}" = "$VERSION" ] || fail '进程环境 NEWS_VERSION 与交付身份不一致'
  [ "${NEWS_ARTIFACT_SHA256-$ARTIFACT_SHA}" = "$ARTIFACT_SHA" ] || fail '进程环境 NEWS_ARTIFACT_SHA256 与交付身份不一致'
  [ "${NEWS_SOURCE_SHA256-$SOURCE_SHA}" = "$SOURCE_SHA" ] || fail '进程环境 NEWS_SOURCE_SHA256 与交付身份不一致'
  # 只解析三项公开身份；不 source、不 eval，也不输出数据库密码。
  if [ -f .env ]; then
    for KEY in NEWS_VERSION NEWS_ARTIFACT_SHA256 NEWS_SOURCE_SHA256; do
      VALUE=$(awk -v key="$KEY" '
        /^[[:space:]]*#/ {next}
        { line=$0; sub(/\r$/, "", line)
          if (line ~ "^[[:space:]]*" key "[[:space:]]*=") {
            count++; sub(/^[^=]*=[[:space:]]*/, "", line); sub(/[[:space:]]*$/, "", line)
            if (line ~ /^".*"$/ || line ~ /^\047.*\047$/) { line=substr(line,2,length(line)-2) }
            value=line
          }
        }
        END {if(count != 1) exit 1; print value}' .env) || fail "现场配置缺少或重复身份字段：$KEY"
      case "$KEY" in
        NEWS_VERSION) EXPECTED=$VERSION ;;
        NEWS_ARTIFACT_SHA256) EXPECTED=$ARTIFACT_SHA ;;
        NEWS_SOURCE_SHA256) EXPECTED=$SOURCE_SHA ;;
      esac
      [ "$VALUE" = "$EXPECTED" ] || fail "现场配置与交付身份不一致：$KEY"
    done
  fi
}
'''
    verify_script = common + r'''verify_loaded
verify_config
printf '%s\n' '离线交付文件、镜像身份及已有配置校验通过'
'''
    load_script = common + r'''EXISTING=$(docker image ls --quiet --no-trunc "$IMAGE")
if [ -n "$EXISTING" ]; then
  verify_loaded
fi
verify_config
docker image load --input "$IMAGE_TAR"
verify_loaded
printf '%s\n' '离线镜像加载并核验通过；启动前请填写现场配置'
'''
    for name, content in (("verify-image.sh", verify_script), ("load-image.sh", load_script)):
        path = target / name
        write_text(path, content)
        path.chmod(0o755)


def validate_cache_url(value, description):
    """缓存地址仅用于联网准备，不允许凭据、查询参数或片段进入构建命令。"""
    cache = urlsplit(value)
    if (cache.scheme not in ("http", "https") or not cache.hostname
            or cache.username is not None or cache.password is not None or "?" in value or "#" in value
            or any(character.isspace() for character in value)):
        raise ValueError(description + "必须是无凭据、查询参数或片段的 HTTP 或 HTTPS 服务地址")
    # 显式读取端口，避免把无效端口或不完整 IPv6 地址传给构建进程。
    cache.port
    return value.rstrip("/")


def export(root, browser_cache_url=None, python_cache_url=None, apt_cache_proxy=None):
    """联网构建后原子发布包含运行时、依赖和浏览器的 AMD64 离线交付包。"""
    manifest, artifact = read_release(root)
    embedded = bool(manifest.get("embedded_configuration"))
    expected_user = None
    configuration = None
    if embedded:
        _, configuration = read_configuration(checked_path(root, "dockerfile/news/news.json"))
        expected_user = ":".join(str(int(configuration.get(key, 10001)))
                                 for key in ("NEWS_RUN_UID", "NEWS_RUN_GID"))
    target = root.parent / (manifest["version"] + "-offline")
    archive = root.parent / ("news-" + manifest["version"] + "-linux-amd64-offline.zip")
    checksum = Path(str(archive) + ".sha256")
    if any(path.exists() for path in (target, archive, checksum)):
        raise ValueError("正式离线交付已存在，保留原文件不覆盖：" + str(target))
    image_name = "news-runtime:" + manifest["version"]
    reject_tag_collision(manifest)
    arguments = ["build", "--platform", PLATFORM, "--tag", image_name]
    if embedded:
        for key in ("NEWS_RUN_UID", "NEWS_RUN_GID"):
            arguments += ["--build-arg", key + "=" + str(int(configuration.get(key, 10001)))]
    if browser_cache_url:
        arguments += ["--build-arg", "PLAYWRIGHT_DOWNLOAD_HOST=" +
                      validate_cache_url(browser_cache_url, "浏览器缓存地址")]
    if python_cache_url:
        arguments += ["--build-arg", "NEWS_PYTHON_CACHE_URL=" +
                      validate_cache_url(python_cache_url, "Python 依赖缓存地址")]
    if apt_cache_proxy:
        proxy = validate_cache_url(apt_cache_proxy, "APT 缓存代理地址")
        # Docker 预定义代理参数仅作用于构建，不写入运行时环境和正式镜像配置。
        for name, value in (("HTTP_PROXY", proxy), ("http_proxy", proxy),
                            ("NO_PROXY", "host.docker.internal"), ("no_proxy", "host.docker.internal")):
            arguments += ["--build-arg", name + "=" + value]
    for name, value in (("NEWS_VERSION", manifest["version"]),
                        ("NEWS_ARTIFACT_SHA256", manifest["artifact_sha256"]),
                        ("NEWS_SOURCE_SHA256", manifest["source_sha256"])):
        arguments += ["--build-arg", name + "=" + value]
    print("正在联网构建 AMD64 运行时镜像，请保留构建输出。", flush=True)
    subprocess.run(["docker", *arguments, str(root / "dockerfile/news")], check=True)
    # 长时间依赖构建后重新核对内部输入，拒绝构建期间发生的配置或制品变化。
    read_release(root)
    image_id = inspect_loaded(manifest, expected_user=expected_user)
    print("正在断网校验制品入口和运行依赖。", flush=True)
    test_runtime(manifest, artifact)
    temporary = Path(tempfile.mkdtemp(prefix=".news-offline-", dir=str(root.parent)))
    temporary_archive = root.parent / (temporary.name + ".zip")
    temporary_checksum = root.parent / (temporary.name + ".sha256")
    archive_hash = None
    try:
        copy_release(root, temporary, manifest)
        image_relative = "images/news-runtime-" + manifest["version"] + "-linux-amd64.tar"
        image_tar = temporary / image_relative
        image_tar.parent.mkdir()
        print("正在导出经过校验的正式镜像，采集数据不会进入交付包。", flush=True)
        docker("image", "save", "--output", str(image_tar), image_name)
        metadata = inspect_image_tar(image_tar, manifest)
        if embedded and metadata["image_user"] != expected_user:
            raise ValueError("镜像归档运行账户与经授权的现场配置不一致")
        offline = {key: manifest[key] for key in ("version", "artifact_filename", "artifact_sha256", "source_sha256")}
        offline.update({"platform": PLATFORM, "image": image_name, "image_id": image_id,
                        "image_tar": image_relative, "image_tar_sha256": file_sha256(image_tar),
                        "checks": ["制品内部版本及源码校验", "AMD64 镜像身份校验", "断网入口版本及帮助",
                                   "断网 pip check", "断网依赖导入", "Chromium 文件版本、动态库和中文字体静态检查"]})
        offline.update(metadata)
        if embedded:
            offline["embedded_configuration"] = True
            offline["configuration_sha256"] = manifest["configuration_sha256"]
        if manifest.get("external_configuration"):
            offline["external_configuration"] = True
        inspect_loaded(offline, image_id)
        write_json(temporary / "OFFLINE-MANIFEST.json", offline)
        write_shell_scripts(temporary, offline)
        files = sorted(path for path in temporary.rglob("*") if path.is_file())
        write_text(temporary / "SHA256SUMS", "".join(file_sha256(path) + "  " +
                   path.relative_to(temporary).as_posix() + "\n" for path in files))
        verify(temporary)
        if not embedded:
            # 镜像已包含压缩层，按原样存储大文件，避免离线打包重复占用大量 CPU。
            with zipfile.ZipFile(temporary_archive, "w", allowZip64=True) as package:
                for path in sorted(temporary.rglob("*")):
                    if path.is_file():
                        name = target.name + "/" + path.relative_to(temporary).as_posix()
                        entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        entry.create_system = 3
                        entry.external_attr = 0o644 << 16
                        entry.compress_type = zipfile.ZIP_STORED if path == image_tar else zipfile.ZIP_DEFLATED
                        with path.open("rb") as incoming, package.open(entry, "w", force_zip64=True) as outgoing:
                            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            with zipfile.ZipFile(temporary_archive) as package:
                if package.testzip() is not None:
                    raise ValueError("离线 ZIP 内容校验失败")
            archive_hash = file_sha256(temporary_archive)
            write_text(temporary_checksum, archive_hash + "  " + archive.name + "\n")
        temporary.rename(target)
        if not embedded:
            temporary_archive.rename(archive)
            temporary_checksum.rename(checksum)
    finally:
        if temporary.exists() and temporary.parent == root.parent and temporary.name.startswith(".news-offline-"):
            shutil.rmtree(temporary)
        for path in (temporary_archive, temporary_checksum):
            if path.exists() and path.parent == root.parent:
                path.unlink()
    return {"directory": str(target), "archive": str(archive) if not embedded else None,
            "archive_sha256": archive_hash, **offline}


def main():
    """build 仅供联网准备；verify 和 load 在离线现场绝不下载或构建。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="准备或校验 Linux AMD64 离线交付；需要 Python 3.8 及以上版本")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (("build", "联网构建、断网验证并导出完整离线包"),
                              ("verify", "只读核验离线交付文件和镜像实际身份"),
                              ("load", "先核验离线包，再只加载本地镜像归档")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--release", required=True, help="build 指向原交付目录；verify/load 指向离线交付目录")
        if name == "build":
            command.add_argument("--browser-cache-url", help="联网准备时使用已校验的官方浏览器文件本地缓存")
            command.add_argument("--python-cache-url", help="联网准备时使用已校验 SHA-256 的 Python 依赖本地缓存")
            command.add_argument("--apt-cache-proxy", help="联网准备时使用受限 APT 缓存代理，仅在镜像构建期间生效")
        if name == "verify":
            command.add_argument("--loaded", action="store_true", help="同时检查已载入 Docker 的镜像身份")
    args = parser.parse_args()
    root = Path(args.release).expanduser().resolve()
    try:
        if args.command == "build":
            result = export(root, browser_cache_url=args.browser_cache_url,
                            python_cache_url=args.python_cache_url, apt_cache_proxy=args.apt_cache_proxy)
        else:
            result = verify(root, loaded=getattr(args, "loaded", False))
            if args.command == "load":
                reject_tag_collision(result, result["image_id"])
                docker("image", "load", "--input", str(checked_path(root, result["image_tar"])))
                inspect_loaded(result, result["image_id"])
            result = {"result": "校验通过" if args.command == "verify" else "镜像加载并核验通过", **result}
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, tarfile.TarError,
            zipfile.BadZipFile, json.JSONDecodeError) as error:
        parser.exit(1, "离线交付操作失败：" + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
