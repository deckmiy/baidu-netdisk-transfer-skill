#!/usr/bin/env python3
"""Baidu Netdisk Open Platform transfer helper.

This script intentionally uses only the Python standard library so the skill can
run in a fresh Codex environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable


USER_AGENT = "pan.baidu.com"
CHUNK_SIZE = 4 * 1024 * 1024
SLICE_MD5_SIZE = 256 * 1024
REFRESH_MARGIN_SECONDS = 300
CONFIG_ENV = "BAIDU_NETDISK_TRANSFER_CONFIG"
ONDUP_CHOICES = ("fail", "newcopy", "overwrite", "skip")
LIST_METHODS = ("list", "listall", "doclist", "imagelist", "videolist")


class BaiduNetdiskError(RuntimeError):
    """Raised for recoverable user-facing failures."""


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "baidu-netdisk-transfer" / "config.json"
    return Path.home() / ".config" / "baidu-netdisk-transfer" / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaiduNetdiskError(f"Invalid config JSON at {path}: {exc}") from exc


def save_config(config: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def require_config(*keys: str) -> dict[str, Any]:
    config = load_config()
    missing = [key for key in keys if not config.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise BaiduNetdiskError(
            f"Missing config fields: {joined}. Run 'config set' first."
        )
    return config


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "(not set)"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def print_json(obj: Any) -> None:
    print(json_dumps(obj))


def format_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def human_size(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{int(size)} {units[unit]}"
    return f"{size:.2f} {units[unit]}"


def item_path(item: dict[str, Any]) -> str:
    return str(item.get("path") or item.get("server_filename") or item.get("filename") or "")


def item_fsid(item: dict[str, Any]) -> Any:
    return item.get("fs_id", item.get("fsid", ""))


def print_item(item: dict[str, Any]) -> None:
    kind = "dir " if int(item.get("isdir") or 0) == 1 else "file"
    size = int(item.get("size") or 0)
    path = item_path(item)
    fsid = item_fsid(item)
    mtime = format_timestamp(item.get("server_mtime", item.get("mtime")))
    print(f"{kind}\t{size}\t{fsid}\t{mtime}\t{path}")


def read_text_lines(path: str) -> list[str]:
    source = Path(path).expanduser()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaiduNetdiskError(f"Could not read list file {source}: {exc}") from exc
    lines = []
    for line in text.splitlines():
        cleaned = line.lstrip("\ufeff").strip()
        if cleaned and not cleaned.lstrip().startswith("#"):
            lines.append(cleaned)
    return lines


def split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def collect_values(values: list[str] | None, list_file: str | None = None) -> list[str]:
    collected: list[str] = []
    for value in values or []:
        collected.extend(split_csv_values(value))
    if list_file:
        collected.extend(read_text_lines(list_file))
    return collected


def load_json_file(path: str) -> Any:
    source = Path(path).expanduser()
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaiduNetdiskError(f"Could not read JSON file {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BaiduNetdiskError(f"Invalid JSON in {source}: {exc}") from exc


def require_safe_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise BaiduNetdiskError(f"Name must be a single path component: {name!r}")
    return name


def confirm_dangerous(message: str, yes: bool) -> None:
    if yes:
        return
    print(message)
    response = input("Type yes to continue: ").strip().lower()
    if response != "yes":
        raise BaiduNetdiskError("Operation cancelled.")


def as_query(params: dict[str, Any]) -> str:
    clean = {key: str(value) for key, value in params.items() if value is not None}
    return urllib.parse.urlencode(clean)


def url_with_params(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{as_query(params)}"


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "REDACTED" if key == "access_token" else value) for key, value in pairs]
    query = urllib.parse.urlencode(redacted)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def parse_json_body(body: bytes, url: str) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        snippet = text[:500].replace("\n", " ")
        raise BaiduNetdiskError(f"Expected JSON from {redact_url(url)}, got: {snippet}") from exc
    if not isinstance(obj, dict):
        raise BaiduNetdiskError(
            f"Expected JSON object from {redact_url(url)}, got {type(obj).__name__}"
        )
    return obj


def request_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)

    body = None
    if data is not None:
        body = as_query(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse_json_body(response.read(), url)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if payload:
            try:
                obj = parse_json_body(payload, url)
                obj["_http_status"] = exc.code
                return obj
            except BaiduNetdiskError:
                pass
        raise BaiduNetdiskError(f"HTTP {exc.code} from {redact_url(url)}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BaiduNetdiskError(f"Network error for {redact_url(url)}: {exc.reason}") from exc


def error_text(obj: dict[str, Any]) -> str:
    for key in ("errmsg", "error_msg", "error_description", "error"):
        if obj.get(key):
            return str(obj[key])
    return json.dumps(obj, ensure_ascii=False)


def check_api_success(obj: dict[str, Any], action: str) -> None:
    if "errno" in obj and int(obj.get("errno") or 0) != 0:
        raise BaiduNetdiskError(f"{action} failed: errno={obj.get('errno')} {error_text(obj)}")
    if "error_code" in obj and int(obj.get("error_code") or 0) != 0:
        raise BaiduNetdiskError(
            f"{action} failed: error_code={obj.get('error_code')} {error_text(obj)}"
        )
    if obj.get("error"):
        raise BaiduNetdiskError(f"{action} failed: {error_text(obj)}")


def retry(label: str, func: Callable[[], Any], attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except BaiduNetdiskError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            eprint(f"{label} failed on attempt {attempt}/{attempts}: {exc}; retrying...")
            time.sleep(min(8, 2 ** attempt))
    raise last_error or BaiduNetdiskError(f"{label} failed")


def token_expires_at(expires_in: Any) -> int:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 2592000
    return int(time.time()) + max(0, seconds)


def save_token_response(config: dict[str, Any], obj: dict[str, Any]) -> None:
    if not obj.get("access_token") or not obj.get("refresh_token"):
        raise BaiduNetdiskError(f"Token response missing required fields: {error_text(obj)}")
    config["access_token"] = obj["access_token"]
    config["refresh_token"] = obj["refresh_token"]
    config["expires_at"] = token_expires_at(obj.get("expires_in"))
    save_config(config)


def refresh_access_token(config: dict[str, Any]) -> str:
    expires_at = int(config.get("expires_at") or 0)
    access_token = config.get("access_token")
    if access_token and expires_at > int(time.time()) + REFRESH_MARGIN_SECONDS:
        return str(access_token)

    refresh_token = config.get("refresh_token")
    if not refresh_token:
        raise BaiduNetdiskError("No refresh_token found. Run 'auth device' first.")

    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config["app_key"],
        "client_secret": config["secret_key"],
    }
    obj = request_json(url_with_params("https://openapi.baidu.com/oauth/2.0/token", params))
    check_api_success(obj, "Refresh access token")
    save_token_response(config, obj)
    return str(obj["access_token"])


def require_access_token() -> tuple[dict[str, Any], str]:
    config = require_config("app_key", "secret_key", "app_name")
    token = refresh_access_token(config)
    return load_config(), token


def validate_app_name(app_name: str) -> None:
    if not app_name or "/" in app_name or "\\" in app_name:
        raise BaiduNetdiskError("app_name must be non-empty and must not contain slashes.")


def app_root(app_name: str) -> str:
    validate_app_name(app_name)
    return f"/apps/{app_name}"


def normalize_remote_path(path: str, app_name: str | None = None) -> str:
    if not path:
        raise BaiduNetdiskError("Remote path must not be empty.")
    raw = path.replace("\\", "/")
    if not raw.startswith("/"):
        if not app_name:
            raise BaiduNetdiskError("Relative remote paths require app_name.")
        raw = f"{app_root(app_name)}/{raw}"
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def ensure_under_app(path: str, app_name: str) -> str:
    normalized = normalize_remote_path(path, app_name)
    root = app_root(app_name)
    if normalized != root and not normalized.startswith(root + "/"):
        raise BaiduNetdiskError(f"Remote path must be under {root}: {normalized}")
    return normalized


def join_remote(base: str, *parts: str) -> str:
    result = base
    for part in parts:
        safe = part.replace("\\", "/").strip("/")
        if safe:
            result = posixpath.join(result, safe)
    return normalize_remote_path(result)


def default_remote_for_local(local_path: Path, app_name: str) -> str:
    return join_remote(app_root(app_name), local_path.name)


def file_hashes(path: Path) -> tuple[int, list[str], str, str]:
    size = path.stat().st_size
    if size <= 0:
        raise BaiduNetdiskError(f"Empty files are not supported by the upload API: {path}")

    whole = hashlib.md5()
    blocks: list[str] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            whole.update(chunk)
            blocks.append(hashlib.md5(chunk).hexdigest())

    with path.open("rb") as handle:
        slice_md5 = hashlib.md5(handle.read(SLICE_MD5_SIZE)).hexdigest()

    return size, blocks, whole.hexdigest(), slice_md5


def whole_file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_plain_md5(value: str) -> bool:
    return len(value) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def comparable_remote_md5(item: dict[str, Any]) -> str:
    remote_md5 = str(item.get("md5") or "")
    if is_plain_md5(remote_md5):
        return remote_md5
    if remote_md5:
        eprint(f"warning: remote md5 is not a plain content MD5, skipping MD5 compare: {item_path(item)}")
    return ""


def precreate_file(
    token: str,
    remote_path: str,
    size: int,
    block_list: list[str],
    rtype: int,
    content_md5: str,
    slice_md5: str,
) -> dict[str, Any]:
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/file",
        {"method": "precreate", "access_token": token},
    )
    data = {
        "path": remote_path,
        "size": size,
        "isdir": 0,
        "block_list": json.dumps(block_list, separators=(",", ":")),
        "autoinit": 1,
        "rtype": rtype,
        "content-md5": content_md5,
        "slice-md5": slice_md5,
    }
    obj = request_json(url, method="POST", data=data)
    check_api_success(obj, "Precreate upload")
    if not obj.get("uploadid"):
        raise BaiduNetdiskError(f"Precreate response missing uploadid: {obj}")
    return obj


def locate_upload_host(token: str, remote_path: str, uploadid: str) -> str:
    url = url_with_params(
        "https://d.pcs.baidu.com/rest/2.0/pcs/file",
        {
            "method": "locateupload",
            "appid": 250528,
            "access_token": token,
            "path": remote_path,
            "uploadid": uploadid,
            "upload_version": "2.0",
        },
    )
    obj = request_json(url)
    check_api_success(obj, "Locate upload host")
    for collection_key in ("servers", "quic_servers", "bak_servers"):
        for item in obj.get(collection_key) or []:
            server = item.get("server") if isinstance(item, dict) else None
            if server and str(server).startswith("https://"):
                return str(server).rstrip("/")
    for key in ("host",):
        if obj.get(key):
            return "https://" + str(obj[key]).strip("/")
    raise BaiduNetdiskError(f"Could not find HTTPS upload host in response: {obj}")


def read_part(path: Path, partseq: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(partseq * CHUNK_SIZE)
        return handle.read(CHUNK_SIZE)


def multipart_body(field_name: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = "----baidu-netdisk-transfer-" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + payload + tail, boundary


def upload_part(
    token: str,
    upload_host: str,
    local_path: Path,
    remote_path: str,
    uploadid: str,
    partseq: int,
    expected_md5: str,
) -> None:
    payload = read_part(local_path, partseq)
    body, boundary = multipart_body("file", local_path.name, payload)
    url = url_with_params(
        f"{upload_host}/rest/2.0/pcs/superfile2",
        {
            "method": "upload",
            "access_token": token,
            "type": "tmpfile",
            "path": remote_path,
            "uploadid": uploadid,
            "partseq": partseq,
        },
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            obj = parse_json_body(response.read(), url)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if payload:
            obj = parse_json_body(payload, url)
            check_api_success(obj, f"Upload part {partseq}")
        raise BaiduNetdiskError(f"HTTP {exc.code} while uploading part {partseq}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BaiduNetdiskError(f"Network error while uploading part {partseq}: {exc.reason}") from exc

    check_api_success(obj, f"Upload part {partseq}")
    returned_md5 = obj.get("md5")
    if returned_md5 and str(returned_md5).lower() != expected_md5.lower():
        raise BaiduNetdiskError(
            f"Part {partseq} md5 mismatch: local={expected_md5} remote={returned_md5}"
        )


def create_file(
    token: str,
    remote_path: str,
    size: int,
    block_list: list[str],
    uploadid: str,
    rtype: int,
) -> dict[str, Any]:
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/file",
        {"method": "create", "access_token": token},
    )
    data = {
        "path": remote_path,
        "size": size,
        "isdir": 0,
        "rtype": rtype,
        "uploadid": uploadid,
        "block_list": json.dumps(block_list, separators=(",", ":")),
    }
    obj = request_json(url, method="POST", data=data)
    check_api_success(obj, "Create remote file")
    return obj


def create_directory(token: str, remote_path: str) -> None:
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/file",
        {"method": "create", "access_token": token},
    )
    obj = request_json(url, method="POST", data={"path": remote_path, "isdir": 1, "rtype": 0})
    errno = int(obj.get("errno") or 0)
    if errno == -8:
        return
    check_api_success(obj, f"Create directory {remote_path}")


def ensure_remote_directories(token: str, remote_dir: str, app_name: str) -> None:
    remote_dir = ensure_under_app(remote_dir, app_name)
    root = app_root(app_name)
    if remote_dir == root:
        create_directory(token, root)
        return
    if not remote_dir.startswith(root + "/"):
        raise BaiduNetdiskError(f"Remote directory must be under {root}: {remote_dir}")

    current = root
    create_directory(token, current)
    remainder = remote_dir[len(root) :].strip("/")
    for part in [item for item in remainder.split("/") if item]:
        current = join_remote(current, part)
        create_directory(token, current)


def needed_parts(precreate_response: dict[str, Any], block_count: int) -> list[int]:
    raw = precreate_response.get("block_list")
    if raw is None:
        return list(range(block_count))
    if raw == []:
        return [0] if block_count == 1 else list(range(block_count))
    try:
        parts = [int(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise BaiduNetdiskError(f"Unexpected precreate block_list: {raw}") from exc
    for part in parts:
        if part < 0 or part >= block_count:
            raise BaiduNetdiskError(f"Precreate requested invalid part index {part}")
    return parts


def upload_file(token: str, local_path: Path, remote_path: str, rtype: int) -> dict[str, Any]:
    size, blocks, content_md5, slice_md5 = file_hashes(local_path)
    print(f"Uploading file: {local_path} -> {remote_path} ({size} bytes, {len(blocks)} part(s))")
    precreated = precreate_file(token, remote_path, size, blocks, rtype, content_md5, slice_md5)
    uploadid = str(precreated["uploadid"])
    host = locate_upload_host(token, remote_path, uploadid)
    parts = needed_parts(precreated, len(blocks))
    for index, partseq in enumerate(parts, start=1):
        print(f"  part {partseq} ({index}/{len(parts)})")
        retry(
            f"upload part {partseq}",
            lambda partseq=partseq: upload_part(
                token, host, local_path, remote_path, uploadid, partseq, blocks[partseq]
            ),
        )
    created = create_file(token, remote_path, size, blocks, uploadid, rtype)
    print(f"Uploaded: {created.get('path', remote_path)}")
    return created


def upload_directory(
    token: str,
    local_dir: Path,
    remote_dir: str,
    rtype: int,
    *,
    skip_same: bool = False,
) -> None:
    print(f"Uploading directory: {local_dir} -> {remote_dir}")
    create_directory(token, remote_dir)
    existing_files = remote_file_index(token, remote_dir) if skip_same else {}
    for root, dirs, files in os.walk(local_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(local_dir)
        current_remote = remote_dir if str(rel_root) == "." else join_remote(remote_dir, rel_root.as_posix())
        for dirname in sorted(dirs):
            create_directory(token, join_remote(current_remote, dirname))
        for filename in sorted(files):
            local_file = root_path / filename
            remote_file = join_remote(current_remote, filename)
            rel_file = local_file.relative_to(local_dir).as_posix()
            if skip_same and not should_upload(local_file, existing_files.get(rel_file), compare_mtime=False):
                print(f"Skipped unchanged: {remote_file}")
                continue
            upload_file(token, local_file, remote_file, rtype)


def list_directory(token: str, remote_dir: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = 0
    limit = 1000
    while True:
        url = url_with_params(
            "https://pan.baidu.com/rest/2.0/xpan/file",
            {
                "method": "list",
                "dir": remote_dir,
                "order": "name",
                "start": start,
                "limit": limit,
                "web": 0,
                "folder": 0,
                "access_token": token,
            },
        )
        obj = request_json(url)
        check_api_success(obj, f"List directory {remote_dir}")
        batch = obj.get("list") or []
        if not isinstance(batch, list):
            raise BaiduNetdiskError(f"Unexpected list response: {obj}")
        items.extend(batch)
        if len(batch) < limit:
            break
        start += limit
    return items


def resolve_remote_path(token: str, remote_path: str, app_name: str) -> dict[str, Any]:
    remote_path = ensure_under_app(remote_path, app_name)
    if remote_path == app_root(app_name):
        return {
            "path": remote_path,
            "server_filename": posixpath.basename(remote_path),
            "isdir": 1,
            "size": 0,
        }
    parent = posixpath.dirname(remote_path) or "/"
    name = posixpath.basename(remote_path)
    for item in list_directory(token, parent):
        if item.get("server_filename") == name or item.get("path") == remote_path:
            return item
    raise BaiduNetdiskError(f"Remote path not found: {remote_path}")


def listall(token: str, remote_dir: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = 0
    limit = 1000
    while True:
        url = url_with_params(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia",
            {
                "method": "listall",
                "path": remote_dir,
                "recursion": 1,
                "start": start,
                "limit": limit,
                "web": 0,
                "access_token": token,
            },
        )
        obj = request_json(url)
        check_api_success(obj, f"Recursively list {remote_dir}")
        batch = obj.get("list") or []
        if not isinstance(batch, list):
            raise BaiduNetdiskError(f"Unexpected listall response: {obj}")
        items.extend(batch)
        if int(obj.get("has_more") or 0) != 1:
            break
        start = int(obj.get("cursor") or (start + limit))
        time.sleep(6)
    return items


def list_by_category(
    token: str,
    method: str,
    parent_path: str,
    *,
    recursion: bool,
    page: int | None,
    num: int,
    order: str,
    desc: bool,
) -> list[dict[str, Any]]:
    if method == "list":
        return list_directory(token, parent_path)
    if method not in {"doclist", "imagelist", "videolist"}:
        raise BaiduNetdiskError(f"Unsupported list method: {method}")
    params: dict[str, Any] = {
        "method": method,
        "access_token": token,
        "parent_path": parent_path,
        "recursion": 1 if recursion else 0,
        "num": num,
        "order": order,
        "desc": 1 if desc else 0,
        "web": 0,
    }
    if page is not None:
        params["page"] = page
    url = url_with_params("https://pan.baidu.com/rest/2.0/xpan/file", params)
    obj = request_json(url)
    check_api_success(obj, f"{method} {parent_path}")
    items = obj.get("list") or []
    if not isinstance(items, list):
        raise BaiduNetdiskError(f"Unexpected {method} response: {obj}")
    return items


def check_file_manager_success(obj: dict[str, Any], action: str) -> None:
    check_api_success(obj, action)
    failures: list[str] = []
    for key in ("info", "list"):
        values = obj.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            raw_errno = item.get("errno", item.get("err_no", 0))
            try:
                errno = int(raw_errno or 0)
            except (TypeError, ValueError):
                errno = 0
            if errno != 0:
                failures.append(json.dumps(item, ensure_ascii=False))
    if failures:
        raise BaiduNetdiskError(f"{action} had item failures: {'; '.join(failures)}")


def file_manager(
    token: str,
    opera: str,
    filelist: list[Any],
    *,
    async_mode: int = 1,
    ondup: str | None = "newcopy",
) -> dict[str, Any]:
    if async_mode not in {0, 1, 2}:
        raise BaiduNetdiskError("--async-mode must be 0, 1, or 2")
    data: dict[str, Any] = {
        "async": async_mode,
        "filelist": json.dumps(filelist, ensure_ascii=False, separators=(",", ":")),
    }
    if ondup:
        data["ondup"] = ondup
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/file",
        {"method": "filemanager", "opera": opera, "access_token": token},
    )
    obj = request_json(url, method="POST", data=data)
    check_file_manager_success(obj, f"{opera} remote files")
    return obj


def search_files(
    token: str,
    key: str,
    remote_dir: str,
    *,
    recursion: bool,
    page: int | None,
    num: int,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "method": "search",
        "access_token": token,
        "key": key,
        "dir": remote_dir,
        "recursion": 1 if recursion else 0,
        "web": 0,
        "num": num,
    }
    if page is not None:
        params["page"] = page
    url = url_with_params("https://pan.baidu.com/rest/2.0/xpan/file", params)
    obj = request_json(url)
    check_api_success(obj, f"Search {remote_dir}")
    items = obj.get("list") or []
    if not isinstance(items, list):
        raise BaiduNetdiskError(f"Unexpected search response: {obj}")
    return items


def quota_info(token: str) -> dict[str, Any]:
    url = url_with_params(
        "https://pan.baidu.com/api/quota",
        {"access_token": token, "checkfree": 1, "checkexpire": 1},
    )
    obj = request_json(url)
    check_api_success(obj, "Get quota")
    return obj


def user_info(token: str) -> dict[str, Any]:
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/nas",
        {"method": "uinfo", "access_token": token},
    )
    obj = request_json(url)
    check_api_success(obj, "Get user info")
    return obj


def create_share_link(
    token: str,
    fsids: list[Any],
    *,
    period: int,
    password: str,
) -> dict[str, Any]:
    if not fsids:
        raise BaiduNetdiskError("No fs_id values to share.")
    if period <= 0:
        raise BaiduNetdiskError("Share period must be a positive day count.")
    if len(password) != 4 or not all(ch.isdigit() or ("a" <= ch <= "z") for ch in password):
        raise BaiduNetdiskError("Share password must be exactly 4 lowercase letters or digits.")
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/share",
        {"method": "rapidshare", "access_token": token},
    )
    data = {
        "fsid_list": json.dumps([str(fsid) for fsid in fsids], separators=(",", ":")),
        "period": period,
        "pwd": password,
    }
    try:
        obj = request_json(url, method="POST", data=data)
    except BaiduNetdiskError as exc:
        if "HTTP 404" in str(exc):
            raise BaiduNetdiskError(
                "Create share link failed: Baidu's direct xpan/share rapidshare "
                "endpoint returned HTTP 404 for this app/account. The official "
                "Baidu Netdisk MCP exposes sharing through its remote SSE service, "
                "but this standalone Open Platform CLI cannot complete that call."
            ) from exc
        raise
    check_api_success(obj, "Create share link")
    return obj


def file_metadata(token: str, fs_id: Any, *, dlink: bool = False) -> dict[str, Any]:
    url = url_with_params(
        "https://pan.baidu.com/rest/2.0/xpan/multimedia",
        {
            "method": "filemetas",
            "access_token": token,
            "fsids": json.dumps([int(fs_id)], separators=(",", ":")),
            "dlink": 1 if dlink else 0,
        },
    )
    obj = request_json(url)
    check_api_success(obj, f"Get metadata for fs_id {fs_id}")
    items = obj.get("list") or []
    if not items:
        raise BaiduNetdiskError(f"No metadata returned for fs_id {fs_id}")
    item = items[0]
    if not isinstance(item, dict):
        raise BaiduNetdiskError(f"Unexpected metadata response for fs_id {fs_id}: {obj}")
    return item


def file_metadata_with_dlink(token: str, fs_id: Any) -> dict[str, Any]:
    item = file_metadata(token, fs_id, dlink=True)
    if not item.get("dlink"):
        raise BaiduNetdiskError(f"No dlink returned for fs_id {fs_id}")
    return item


def append_access_token(dlink: str, token: str) -> str:
    parsed = urllib.parse.urlsplit(dlink)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    pairs = [(key, value) for key, value in pairs if key != "access_token"]
    pairs.append(("access_token", token))
    query = urllib.parse.urlencode(pairs)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)
    )


def download_headers(part_path: Path, resume: bool) -> tuple[dict[str, str], int]:
    headers = {"User-Agent": USER_AGENT}
    start = 0
    if resume and part_path.exists():
        start = part_path.stat().st_size
        if start > 0:
            headers["Range"] = f"bytes={start}-"
    return headers, start


def stream_download(
    url: str,
    destination: Path,
    *,
    expected_size: int | None,
    resume: bool,
    overwrite: bool,
) -> None:
    if destination.exists():
        if expected_size is not None and destination.stat().st_size == expected_size and resume:
            print(f"Already downloaded: {destination}")
            return
        if not overwrite:
            raise BaiduNetdiskError(f"Destination exists, use --overwrite: {destination}")
        destination.unlink()

    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = Path(str(destination) + ".part")
    if part_path.exists() and not resume:
        part_path.unlink()

    headers, start = download_headers(part_path, resume)
    request = urllib.request.Request(url, headers=headers, method="GET")
    mode = "ab" if start else "wb"
    print(f"Downloading: {destination}")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            status = int(getattr(response, "status", response.getcode()))
            if start and status == 200:
                # Server ignored Range; restart instead of appending a full copy.
                mode = "wb"
            with part_path.open(mode + "") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise BaiduNetdiskError(f"HTTP {exc.code} while downloading {destination}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise BaiduNetdiskError(f"Network error while downloading {destination}: {exc.reason}") from exc

    if expected_size is not None and part_path.stat().st_size != expected_size:
        raise BaiduNetdiskError(
            f"Downloaded size mismatch for {destination}: got {part_path.stat().st_size}, expected {expected_size}"
        )
    os.replace(part_path, destination)


def download_file_item(
    token: str,
    item: dict[str, Any],
    destination: Path,
    *,
    resume: bool,
    overwrite: bool,
) -> None:
    metadata = file_metadata_with_dlink(token, item["fs_id"])
    url = append_access_token(str(metadata["dlink"]), token)
    size_value = metadata.get("size", item.get("size"))
    expected_size = int(size_value) if size_value is not None else None
    stream_download(url, destination, expected_size=expected_size, resume=resume, overwrite=overwrite)


def local_root_for_directory(remote_dir: str, local_path: Path) -> Path:
    if local_path.exists() and local_path.is_dir():
        return local_path / posixpath.basename(remote_dir.rstrip("/"))
    return local_path


def relative_remote_path(base: str, path: str) -> Path:
    base_clean = base.rstrip("/") + "/"
    if path == base.rstrip("/"):
        return Path(posixpath.basename(path))
    if not path.startswith(base_clean):
        raise BaiduNetdiskError(f"Remote item is outside requested directory: {path}")
    return Path(*Path(path[len(base_clean):]).parts)


def download_directory(
    token: str,
    remote_dir: str,
    local_path: Path,
    *,
    resume: bool,
    overwrite: bool,
) -> None:
    root = local_root_for_directory(remote_dir, local_path)
    root.mkdir(parents=True, exist_ok=True)
    print(f"Downloading directory: {remote_dir} -> {root}")
    for item in listall(token, remote_dir):
        item_path = str(item.get("path") or "")
        if not item_path:
            continue
        rel = relative_remote_path(remote_dir, item_path)
        target = root / rel
        if int(item.get("isdir") or 0) == 1:
            target.mkdir(parents=True, exist_ok=True)
        else:
            download_file_item(token, item, target, resume=resume, overwrite=overwrite)


def remote_tree(token: str, remote_dir: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        root_item: dict[str, Any] | None = None
        for item in list_directory(token, posixpath.dirname(remote_dir) or "/"):
            if str(item.get("path") or "") == remote_dir:
                root_item = item
                break
        if root_item:
            result["."] = root_item
    except BaiduNetdiskError:
        pass
    for item in listall(token, remote_dir):
        path = str(item.get("path") or "")
        if not path:
            continue
        rel = relative_remote_path(remote_dir, path).as_posix()
        result[rel] = item
    return result


def local_tree(local_dir: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for root, _dirs, files in os.walk(local_dir):
        root_path = Path(root)
        for filename in files:
            path = root_path / filename
            rel = path.relative_to(local_dir).as_posix()
            result[rel] = path
    return result


def remote_file_index(token: str, remote_dir: str) -> dict[str, dict[str, Any]]:
    return {
        rel: item
        for rel, item in remote_tree(token, remote_dir).items()
        if int(item.get("isdir") or 0) != 1
    }


def should_upload(local_path: Path, remote_item: dict[str, Any] | None, *, compare_mtime: bool) -> bool:
    if remote_item is None:
        return True
    try:
        remote_size = int(remote_item.get("size") or 0)
    except (TypeError, ValueError):
        return True
    if local_path.stat().st_size != remote_size:
        return True
    if compare_mtime:
        try:
            remote_mtime = int(remote_item.get("server_mtime", remote_item.get("mtime", 0)) or 0)
        except (TypeError, ValueError):
            remote_mtime = 0
        if int(local_path.stat().st_mtime) > remote_mtime:
            return True
    return False


def verify_local_against_remote(
    token: str,
    local_path: Path,
    remote_path: str,
    *,
    check_md5: bool = False,
) -> tuple[int, list[str]]:
    errors: list[str] = []
    checked = 0
    if local_path.is_file():
        app_name = str(require_config("app_name")["app_name"])
        item = resolve_remote_path(token, remote_path, app_name)
        checked += 1
        local_size = local_path.stat().st_size
        remote_size = int(item.get("size") or 0)
        if local_size != remote_size:
            errors.append(f"size mismatch: {local_path} ({local_size}) != {remote_path} ({remote_size})")
        if check_md5:
            remote_md5 = comparable_remote_md5(item)
            if not remote_md5 and item_fsid(item):
                remote_md5 = comparable_remote_md5(file_metadata(token, item_fsid(item)))
            if remote_md5 and whole_file_md5(local_path).lower() != remote_md5.lower():
                errors.append(f"md5 mismatch: {local_path} != {remote_path}")
        return checked, errors

    remote_files = remote_file_index(token, remote_path)
    local_files = local_tree(local_path)
    for rel, path in local_files.items():
        checked += 1
        item = remote_files.get(rel)
        if item is None:
            errors.append(f"missing remote file: {join_remote(remote_path, rel)}")
            continue
        local_size = path.stat().st_size
        remote_size = int(item.get("size") or 0)
        if local_size != remote_size:
            errors.append(f"size mismatch: {rel} local={local_size} remote={remote_size}")
        if check_md5:
            remote_md5 = comparable_remote_md5(item)
            if remote_md5 and whole_file_md5(path).lower() != remote_md5.lower():
                errors.append(f"md5 mismatch: {rel}")
    for rel in sorted(set(remote_files) - set(local_files)):
        checked += 1
        errors.append(f"extra remote file: {join_remote(remote_path, rel)}")
    return checked, errors


def cmd_config_set(args: argparse.Namespace) -> int:
    config = load_config()
    if args.app_key:
        config["app_key"] = args.app_key
    if args.secret_key:
        config["secret_key"] = args.secret_key
    if args.app_name:
        validate_app_name(args.app_name)
        config["app_name"] = args.app_name
    save_config(config)
    print(f"Saved config: {config_path()}")
    return 0


def cmd_auth_device(args: argparse.Namespace) -> int:
    config = require_config("app_key", "secret_key")
    params = {
        "response_type": "device_code",
        "client_id": config["app_key"],
        "scope": "basic,netdisk",
    }
    obj = request_json(url_with_params("https://openapi.baidu.com/oauth/2.0/device/code", params))
    check_api_success(obj, "Request device code")

    device_code = str(obj["device_code"])
    user_code = str(obj["user_code"])
    verification_url = str(obj.get("verification_url") or "https://openapi.baidu.com/device")
    qrcode_url = str(obj.get("qrcode_url") or "")
    interval = max(5, int(obj.get("interval") or 5))
    deadline = time.time() + int(obj.get("expires_in") or 300)

    print("Authorize this app with Baidu Netdisk:")
    print(f"  verification_url: {verification_url}")
    print(f"  user_code: {user_code}")
    if qrcode_url:
        print(f"  qrcode_url: {qrcode_url}")
    print("Waiting for authorization...")

    while time.time() < deadline:
        time.sleep(interval)
        token_params = {
            "grant_type": "device_token",
            "code": device_code,
            "client_id": config["app_key"],
            "client_secret": config["secret_key"],
        }
        token_obj = request_json(
            url_with_params("https://openapi.baidu.com/oauth/2.0/token", token_params)
        )
        if token_obj.get("access_token"):
            save_token_response(config, token_obj)
            print(f"Authorized. Tokens saved to {config_path()}")
            return 0
        error = str(token_obj.get("error") or "")
        if error in {"authorization_pending", "authorization_pending_user_confirm"}:
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise BaiduNetdiskError(f"Authorization failed: {error_text(token_obj)}")
        check_api_success(token_obj, "Poll device token")

    raise BaiduNetdiskError("Device code expired before authorization completed.")


def cmd_auth_status(args: argparse.Namespace) -> int:
    config = load_config()
    if not config:
        print(f"No config found at {config_path()}")
        return 0
    print(f"Config: {config_path()}")
    print(f"app_name: {config.get('app_name') or '(not set)'}")
    print(f"app_key: {mask(config.get('app_key'))}")
    print(f"secret_key: {mask(config.get('secret_key'))}")
    print(f"access_token: {mask(config.get('access_token'))}")
    print(f"refresh_token: {mask(config.get('refresh_token'))}")
    expires_at = int(config.get("expires_at") or 0)
    if expires_at:
        remaining = expires_at - int(time.time())
        print(f"expires_at: {expires_at} ({remaining} seconds remaining)")
    else:
        print("expires_at: (not set)")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    local_path = Path(args.local_path).expanduser().resolve()
    if not local_path.exists():
        raise BaiduNetdiskError(f"Local path not found: {local_path}")

    if args.remote:
        raw_remote = args.remote
        remote = normalize_remote_path(raw_remote, app_name)
        if local_path.is_file() and raw_remote.replace("\\", "/").endswith("/"):
            remote = join_remote(remote, local_path.name)
    else:
        remote = default_remote_for_local(local_path, app_name)
    remote = ensure_under_app(remote, app_name)

    if local_path.is_dir():
        ensure_remote_directories(token, remote, app_name)
        upload_directory(token, local_path, remote, args.rtype, skip_same=args.skip_same)
    else:
        ensure_remote_directories(token, posixpath.dirname(remote), app_name)
        if args.skip_same:
            try:
                existing = resolve_remote_path(token, remote, app_name)
            except BaiduNetdiskError:
                existing = None
            if existing and int(existing.get("isdir") or 0) != 1 and not should_upload(
                local_path, existing, compare_mtime=False
            ):
                print(f"Skipped unchanged: {remote}")
                return 0
        upload_file(token, local_path, remote, args.rtype)
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_path, app_name)
    local_path = Path(args.local_path).expanduser()
    item = resolve_remote_path(token, remote, app_name)
    if int(item.get("isdir") or 0) == 1:
        download_directory(token, remote, local_path, resume=args.resume, overwrite=args.overwrite)
    else:
        destination = local_path
        local_raw = str(args.local_path)
        is_directory_target = local_raw.endswith(("/", "\\")) or (
            local_path.exists() and local_path.is_dir()
        )
        if is_directory_target:
            local_path.mkdir(parents=True, exist_ok=True)
            destination = local_path / str(item.get("server_filename") or posixpath.basename(remote))
        download_file_item(token, item, destination, resume=args.resume, overwrite=args.overwrite)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_dir, app_name)
    if args.recursive and args.method == "list":
        items = listall(token, remote)
    else:
        items = list_by_category(
            token,
            args.method,
            remote,
            recursion=args.recursive,
            page=args.page,
            num=args.num,
            order=args.order,
            desc=args.desc,
        )
    if args.json:
        print_json(items)
    else:
        for item in items:
            print_item(item)
    return 0


def print_tree(remote_dir: str, items: list[dict[str, Any]]) -> None:
    print(remote_dir.rstrip("/") + "/")
    for item in sorted(items, key=lambda value: str(value.get("path") or "")):
        path = str(item.get("path") or "")
        if not path:
            continue
        rel = relative_remote_path(remote_dir, path).as_posix()
        if rel == ".":
            continue
        depth = rel.count("/")
        name = posixpath.basename(path)
        suffix = "/" if int(item.get("isdir") or 0) == 1 else ""
        print(f"{'  ' * depth}{name}{suffix}")


def remote_target_payload(
    token: str,
    source: str,
    target: str,
    app_name: str,
) -> tuple[str, str, dict[str, str]]:
    source_path = ensure_under_app(source, app_name)
    target_path = ensure_under_app(target, app_name)
    raw_target = target.replace("\\", "/")
    target_is_dir = raw_target.endswith("/")
    if not target_is_dir:
        try:
            target_item = resolve_remote_path(token, target_path, app_name)
            target_is_dir = int(target_item.get("isdir") or 0) == 1
        except BaiduNetdiskError:
            target_is_dir = False
    if target_is_dir:
        dest = target_path.rstrip("/")
        newname = posixpath.basename(source_path)
        final_path = join_remote(dest, newname)
    else:
        dest = posixpath.dirname(target_path) or "/"
        newname = posixpath.basename(target_path)
        final_path = target_path
    require_safe_name(newname)
    ensure_under_app(dest, app_name)
    payload = {"path": source_path, "dest": dest, "newname": newname}
    return source_path, final_path, payload


def collapse_paths(paths: list[str]) -> list[str]:
    collapsed: list[str] = []
    for path in sorted(set(paths), key=lambda item: (item.count("/"), item)):
        if any(path == existing or path.startswith(existing.rstrip("/") + "/") for existing in collapsed):
            continue
        collapsed.append(path)
    return collapsed


def cmd_tree(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_dir, app_name)
    items = listall(token, remote)
    if args.json:
        print_json(items)
    else:
        print_tree(remote, items)
    return 0


def cmd_mkdir(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_dir, app_name)
    if args.parents:
        ensure_remote_directories(token, remote, app_name)
    else:
        create_directory(token, remote)
    print(f"Created directory: {remote}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    root = app_root(app_name)
    raw_paths = collect_values(args.remote_path, args.list_file)
    if not raw_paths:
        raise BaiduNetdiskError("No remote paths provided.")
    paths = [ensure_under_app(path, app_name) for path in raw_paths]
    items: list[dict[str, Any]] = []
    for path in paths:
        if path == root and not args.allow_app_root:
            raise BaiduNetdiskError(f"Refusing to delete app root without --allow-app-root: {root}")
        item = resolve_remote_path(token, path, app_name)
        if int(item.get("isdir") or 0) == 1 and not args.recursive:
            raise BaiduNetdiskError(f"Refusing to delete directory without --recursive: {path}")
        items.append(item)
    delete_paths = collapse_paths([ensure_under_app(str(item.get("path") or path), app_name) for item, path in zip(items, paths)])
    if args.dry_run:
        for path in delete_paths:
            print(f"would delete: {path}")
        return 0
    confirm_dangerous(f"About to delete {len(delete_paths)} remote path(s) under {root}.", args.yes)
    obj = file_manager(token, "delete", delete_paths, async_mode=args.async_mode, ondup=args.ondup)
    print_json(obj)
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    _source, final_path, payload = remote_target_payload(token, args.source, args.target, app_name)
    if args.parents:
        ensure_remote_directories(token, payload["dest"], app_name)
    obj = file_manager(token, "copy", [payload], async_mode=args.async_mode, ondup=args.ondup)
    print(f"Copied to: {final_path}")
    if args.json:
        print_json(obj)
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    source, final_path, payload = remote_target_payload(token, args.source, args.target, app_name)
    if source == app_root(app_name):
        raise BaiduNetdiskError("Refusing to move app root.")
    if args.parents:
        ensure_remote_directories(token, payload["dest"], app_name)
    obj = file_manager(token, "move", [payload], async_mode=args.async_mode, ondup=args.ondup)
    print(f"Moved to: {final_path}")
    if args.json:
        print_json(obj)
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    source = ensure_under_app(args.remote_path, app_name)
    if source == app_root(app_name):
        raise BaiduNetdiskError("Refusing to rename app root.")
    newname = require_safe_name(args.new_name)
    payload = {"path": source, "newname": newname}
    obj = file_manager(token, "rename", [payload], async_mode=args.async_mode, ondup=args.ondup)
    print(f"Renamed to: {join_remote(posixpath.dirname(source), newname)}")
    if args.json:
        print_json(obj)
    return 0


def cmd_stat(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_path, app_name)
    item = resolve_remote_path(token, remote, app_name)
    if item_fsid(item) and (args.dlink or int(item.get("isdir") or 0) != 1):
        try:
            item = {**item, **file_metadata(token, item_fsid(item), dlink=args.dlink)}
        except BaiduNetdiskError as exc:
            if args.dlink:
                raise
            eprint(f"warning: metadata lookup failed: {exc}")
    print_json(item)
    return 0


def cmd_exists(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_path, app_name)
    try:
        item = resolve_remote_path(token, remote, app_name)
    except BaiduNetdiskError:
        print(f"missing\t{remote}")
        return 2
    if args.json:
        print_json(item)
    else:
        print(f"exists\t{remote}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote = ensure_under_app(args.remote_dir or app_root(app_name), app_name)
    items = search_files(
        token,
        args.key,
        remote,
        recursion=args.recursive,
        page=args.page,
        num=args.num,
    )
    if args.json:
        print_json(items)
    else:
        for item in items:
            print_item(item)
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    _config, token = require_access_token()
    obj = quota_info(token)
    if args.json:
        print_json(obj)
    else:
        total = int(obj.get("total") or 0)
        used = int(obj.get("used") or 0)
        free = int(obj.get("free") or max(0, total - used))
        print(f"total\t{total}\t{human_size(total)}")
        print(f"used\t{used}\t{human_size(used)}")
        print(f"free\t{free}\t{human_size(free)}")
        if "expire" in obj:
            print(f"expire\t{obj.get('expire')}")
    return 0


def cmd_uinfo(args: argparse.Namespace) -> int:
    _config, token = require_access_token()
    print_json(user_info(token))
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    fsids: list[Any] = []
    for fsid in collect_values(args.fs_id, args.fsid_list_file):
        fsids.append(fsid)
    for path in collect_values(args.remote_path, args.path_list_file):
        item = resolve_remote_path(token, ensure_under_app(path, app_name), app_name)
        fsid = item_fsid(item)
        if not fsid:
            raise BaiduNetdiskError(f"No fs_id for share path: {path}")
        fsids.append(fsid)
    obj = create_share_link(token, fsids, period=args.period, password=args.password)
    print_json(obj)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    local_path = Path(args.local_path).expanduser().resolve()
    if not local_path.exists():
        raise BaiduNetdiskError(f"Local path not found: {local_path}")
    remote = ensure_under_app(args.remote_path, app_name)
    checked, errors = verify_local_against_remote(token, local_path, remote, check_md5=args.md5)
    for error in errors:
        print(f"mismatch\t{error}")
    if errors:
        print(f"verify failed: checked={checked}, mismatches={len(errors)}")
        return 1
    print(f"verify ok: checked={checked}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    local_dir = Path(args.local_dir).expanduser().resolve()
    if not local_dir.is_dir():
        raise BaiduNetdiskError(f"Local directory not found: {local_dir}")
    remote_dir = ensure_under_app(args.remote_dir, app_name)
    ensure_remote_directories(token, remote_dir, app_name)
    remote_files = remote_file_index(token, remote_dir)
    local_files = local_tree(local_dir)

    uploads: list[tuple[Path, str]] = []
    for rel, local_file in sorted(local_files.items()):
        remote_file = join_remote(remote_dir, rel)
        if should_upload(local_file, remote_files.get(rel), compare_mtime=args.compare_mtime):
            uploads.append((local_file, remote_file))

    if args.dry_run:
        for local_file, remote_file in uploads:
            print(f"would upload: {local_file} -> {remote_file}")
    else:
        for root, dirs, _files in os.walk(local_dir):
            root_path = Path(root)
            rel_root = root_path.relative_to(local_dir)
            current_remote = remote_dir if str(rel_root) == "." else join_remote(remote_dir, rel_root.as_posix())
            for dirname in sorted(dirs):
                create_directory(token, join_remote(current_remote, dirname))
        for local_file, remote_file in uploads:
            upload_file(token, local_file, remote_file, args.rtype)

    remote_tree_items = remote_tree(token, remote_dir) if args.delete_remote else {}
    local_rels = set(local_files)
    local_dirs = {
        path.relative_to(local_dir).as_posix()
        for path in local_dir.rglob("*")
        if path.is_dir()
    }
    delete_paths = []
    if args.delete_remote:
        for rel, item in remote_tree_items.items():
            if rel == ".":
                continue
            isdir = int(item.get("isdir") or 0) == 1
            if (isdir and rel not in local_dirs) or ((not isdir) and rel not in local_rels):
                path = str(item.get("path") or "")
                if path:
                    delete_paths.append(path)
        delete_paths = collapse_paths(delete_paths)
        if args.dry_run:
            for path in delete_paths:
                print(f"would delete remote: {path}")
        elif delete_paths:
            confirm_dangerous(
                f"About to delete {len(delete_paths)} remote path(s) not present in {local_dir}.",
                args.yes,
            )
            file_manager(token, "delete", delete_paths, async_mode=args.async_mode, ondup="fail")

    print(
        f"sync complete: uploaded={len(uploads)}, "
        f"deleted={len(delete_paths) if args.delete_remote else 0}, dry_run={args.dry_run}"
    )
    return 0


def cmd_batch_delete(args: argparse.Namespace) -> int:
    args.remote_path = []
    return cmd_delete(args)


def cmd_batch_download(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    local_dir = Path(args.local_dir).expanduser()
    local_dir.mkdir(parents=True, exist_ok=True)
    for remote_raw in read_text_lines(args.list_file):
        remote = ensure_under_app(remote_raw, app_name)
        item = resolve_remote_path(token, remote, app_name)
        target = local_dir / posixpath.basename(remote.rstrip("/"))
        if int(item.get("isdir") or 0) == 1:
            download_directory(token, remote, target, resume=args.resume, overwrite=args.overwrite)
        else:
            download_file_item(token, item, target, resume=args.resume, overwrite=args.overwrite)
    return 0


def cmd_batch_upload(args: argparse.Namespace) -> int:
    config, token = require_access_token()
    app_name = str(config["app_name"])
    remote_dir = ensure_under_app(args.remote_dir, app_name)
    ensure_remote_directories(token, remote_dir, app_name)
    for local_raw in read_text_lines(args.list_file):
        local_path = Path(local_raw).expanduser().resolve()
        if not local_path.exists():
            raise BaiduNetdiskError(f"Local path not found: {local_path}")
        remote = join_remote(remote_dir, local_path.name)
        if local_path.is_dir():
            upload_directory(token, local_path, remote, args.rtype, skip_same=args.skip_same)
        else:
            upload_file(token, local_path, remote, args.rtype)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        os.environ[CONFIG_ENV] = str(tmp / "config.json")
        save_config({"app_key": "ak", "secret_key": "sk", "app_name": "AppName"})
        loaded = load_config()
        assert loaded["app_key"] == "ak"
        assert ensure_under_app("nested/file.txt", "AppName") == "/apps/AppName/nested/file.txt"
        try:
            ensure_under_app("/other/file.txt", "AppName")
        except BaiduNetdiskError:
            pass
        else:
            raise AssertionError("outside app path was accepted")

        small = tmp / "small.bin"
        small.write_bytes(b"abc")
        size, blocks, content_md5, slice_md5 = file_hashes(small)
        assert size == 3
        assert blocks == [hashlib.md5(b"abc").hexdigest()]
        assert content_md5 == blocks[0]
        assert slice_md5 == blocks[0]

        large = tmp / "large.bin"
        large.write_bytes(b"x" * (CHUNK_SIZE + 17))
        size, blocks, _, _ = file_hashes(large)
        assert size == CHUNK_SIZE + 17
        assert len(blocks) == 2

        url = append_access_token("https://d.pcs.baidu.com/file/x?fid=1", "token value")
        assert "access_token=token+value" in url
        assert "access_token=REDACTED" in redact_url(url)
        assert is_plain_md5(hashlib.md5(b"abc").hexdigest())
        assert not is_plain_md5("b845a1f16l257d8cbe5673ff6c9ec03c")

        list_file = tmp / "list.txt"
        list_file.write_text("\ufeff/apps/AppName/a.txt\n# comment\n/apps/AppName/b.txt\n", encoding="utf-8")
        assert read_text_lines(str(list_file)) == ["/apps/AppName/a.txt", "/apps/AppName/b.txt"]

        part = tmp / "download.bin.part"
        part.write_bytes(b"12345")
        headers, start = download_headers(part, True)
        assert start == 5
        assert headers["Range"] == "bytes=5-"

        assert whole_file_md5(small) == hashlib.md5(b"abc").hexdigest()
        assert collapse_paths(["/apps/AppName/a/b", "/apps/AppName/a", "/apps/AppName/c"]) == [
            "/apps/AppName/a",
            "/apps/AppName/c",
        ]

        parser = build_parser()
        assert parser.parse_args(["upload", "x", "--skip-same"]).skip_same is True
        assert parser.parse_args(["delete", "a", "--recursive", "--dry-run"]).recursive is True
        assert parser.parse_args(["sync", "local", "remote", "--delete-remote", "--yes"]).delete_remote is True

        calls: list[tuple[str, dict[str, Any]]] = []
        original_request_json = globals()["request_json"]

        def fake_request_json(url: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((url, kwargs))
            return {"errno": 0, "info": [{"errno": 0}]}

        globals()["request_json"] = fake_request_json
        try:
            file_manager("token", "delete", ["/apps/AppName/a"], async_mode=1, ondup="fail")
        finally:
            globals()["request_json"] = original_request_json
        assert "method=filemanager" in calls[0][0]
        assert "opera=delete" in calls[0][0]
        assert calls[0][1]["data"]["filelist"] == '["/apps/AppName/a"]'

    print("selftest ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Baidu Netdisk transfer helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    config_parser = subparsers.add_parser("config", help="Manage local configuration")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="Set app credentials")
    config_set.add_argument("--app-key", help="Baidu Open Platform AppKey")
    config_set.add_argument("--secret-key", help="Baidu Open Platform SecretKey")
    config_set.add_argument("--app-name", help="Baidu Open Platform product name")
    config_set.set_defaults(func=cmd_config_set)

    auth_parser = subparsers.add_parser("auth", help="Authorize or inspect tokens")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_device = auth_sub.add_parser("device", help="Run device-code authorization")
    auth_device.set_defaults(func=cmd_auth_device)
    auth_status = auth_sub.add_parser("status", help="Show saved credential status")
    auth_status.set_defaults(func=cmd_auth_status)

    upload = subparsers.add_parser("upload", help="Upload a local file or directory")
    upload.add_argument("local_path")
    upload.add_argument("--remote", help="Remote path, absolute or relative to /apps/{app_name}")
    upload.add_argument("--rtype", type=int, choices=(1, 2, 3), default=1, help="Baidu name conflict strategy")
    upload.add_argument("--skip-same", action="store_true", help="Skip files whose remote size already matches")
    upload.set_defaults(func=cmd_upload)

    download = subparsers.add_parser("download", help="Download a remote file or directory")
    download.add_argument("remote_path")
    download.add_argument("local_path")
    download.add_argument("--resume", action="store_true", help="Resume from an existing .part file")
    download.add_argument("--overwrite", action="store_true", help="Overwrite existing destination files")
    download.set_defaults(func=cmd_download)

    list_parser = subparsers.add_parser("list", help="List a remote directory")
    list_parser.add_argument("remote_dir")
    list_parser.add_argument("--recursive", action="store_true", help="Recursively list descendants")
    list_parser.add_argument(
        "--method",
        choices=("list", "doclist", "imagelist", "videolist"),
        default="list",
        help="List API method",
    )
    list_parser.add_argument("--page", type=int, help="Page number for typed list methods")
    list_parser.add_argument("--num", type=int, default=1000, help="Page size for typed list methods")
    list_parser.add_argument("--order", default="name", help="Sort field")
    list_parser.add_argument("--desc", action="store_true", help="Sort descending")
    list_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    list_parser.set_defaults(func=cmd_list)

    tree = subparsers.add_parser("tree", help="Print a recursive remote tree")
    tree.add_argument("remote_dir")
    tree.add_argument("--json", action="store_true", help="Print raw JSON")
    tree.set_defaults(func=cmd_tree)

    mkdir = subparsers.add_parser("mkdir", help="Create a remote directory")
    mkdir.add_argument("remote_dir")
    mkdir.add_argument("--parents", action="store_true", default=True, help="Create parent directories")
    mkdir.add_argument("--single", dest="parents", action="store_false", help="Create only the final directory")
    mkdir.set_defaults(func=cmd_mkdir)

    delete = subparsers.add_parser("delete", aliases=["rm"], help="Delete remote files or directories")
    delete.add_argument("remote_path", nargs="*")
    delete.add_argument("--list-file", help="UTF-8 file containing one remote path per line")
    delete.add_argument("--recursive", action="store_true", help="Allow deleting directories")
    delete.add_argument("--yes", action="store_true", help="Do not prompt before deleting")
    delete.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    delete.add_argument("--allow-app-root", action="store_true", help="Allow deleting /apps/{app_name}")
    delete.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu filemanager async mode")
    delete.add_argument("--ondup", choices=ONDUP_CHOICES, default="fail", help="Baidu duplicate handling")
    delete.set_defaults(func=cmd_delete)

    copy = subparsers.add_parser("copy", aliases=["cp"], help="Copy a remote file or directory")
    copy.add_argument("source")
    copy.add_argument("target")
    copy.add_argument("--parents", action="store_true", help="Create target parent directories")
    copy.add_argument("--ondup", choices=ONDUP_CHOICES, default="newcopy", help="Baidu duplicate handling")
    copy.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu filemanager async mode")
    copy.add_argument("--json", action="store_true", help="Print raw JSON response")
    copy.set_defaults(func=cmd_copy)

    move = subparsers.add_parser("move", aliases=["mv"], help="Move a remote file or directory")
    move.add_argument("source")
    move.add_argument("target")
    move.add_argument("--parents", action="store_true", help="Create target parent directories")
    move.add_argument("--ondup", choices=ONDUP_CHOICES, default="fail", help="Baidu duplicate handling")
    move.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu filemanager async mode")
    move.add_argument("--json", action="store_true", help="Print raw JSON response")
    move.set_defaults(func=cmd_move)

    rename = subparsers.add_parser("rename", help="Rename a remote file or directory")
    rename.add_argument("remote_path")
    rename.add_argument("new_name")
    rename.add_argument("--ondup", choices=ONDUP_CHOICES, default="fail", help="Baidu duplicate handling")
    rename.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu filemanager async mode")
    rename.add_argument("--json", action="store_true", help="Print raw JSON response")
    rename.set_defaults(func=cmd_rename)

    stat = subparsers.add_parser("stat", aliases=["meta"], help="Show metadata for a remote path")
    stat.add_argument("remote_path")
    stat.add_argument("--dlink", action="store_true", help="Request a download link for files")
    stat.set_defaults(func=cmd_stat)

    exists = subparsers.add_parser("exists", aliases=["check"], help="Check whether a remote path exists")
    exists.add_argument("remote_path")
    exists.add_argument("--json", action="store_true", help="Print item JSON when found")
    exists.set_defaults(func=cmd_exists)

    search = subparsers.add_parser("search", help="Search remote filenames")
    search.add_argument("key")
    search.add_argument("--remote-dir", help="Search directory, default /apps/{app_name}")
    search.add_argument("--recursive", action="store_true", help="Search recursively")
    search.add_argument("--page", type=int, help="Page number")
    search.add_argument("--num", type=int, default=50, help="Page size")
    search.add_argument("--json", action="store_true", help="Print raw JSON")
    search.set_defaults(func=cmd_search)

    quota = subparsers.add_parser("quota", help="Show account quota")
    quota.add_argument("--json", action="store_true", help="Print raw JSON")
    quota.set_defaults(func=cmd_quota)

    uinfo = subparsers.add_parser("uinfo", aliases=["whoami"], help="Show authorized user info")
    uinfo.set_defaults(func=cmd_uinfo)

    share = subparsers.add_parser("share", help="Create a share link from remote paths or fs_ids")
    share.add_argument("remote_path", nargs="*")
    share.add_argument("--path-list-file", help="UTF-8 file containing one remote path per line")
    share.add_argument("--fs-id", action="append", help="fs_id to share; may be repeated or comma-separated")
    share.add_argument("--fsid-list-file", help="UTF-8 file containing one fs_id per line")
    share.add_argument("--period", type=int, default=7, help="Share validity in days")
    share.add_argument("--password", default="1234", help="4 lowercase letters/digits")
    share.set_defaults(func=cmd_share)

    verify = subparsers.add_parser("verify", help="Verify local file or directory against remote sizes")
    verify.add_argument("local_path")
    verify.add_argument("remote_path")
    verify.add_argument("--md5", action="store_true", help="Also compare MD5 when remote metadata provides it")
    verify.set_defaults(func=cmd_verify)

    sync = subparsers.add_parser("sync", help="Upload changed files from a local directory to a remote directory")
    sync.add_argument("local_dir")
    sync.add_argument("remote_dir")
    sync.add_argument("--dry-run", action="store_true", help="Show actions without changing remote files")
    sync.add_argument("--delete-remote", action="store_true", help="Delete remote files missing locally")
    sync.add_argument("--yes", action="store_true", help="Do not prompt before remote deletions")
    sync.add_argument("--compare-mtime", action="store_true", help="Upload when local mtime is newer even if size matches")
    sync.add_argument("--rtype", type=int, choices=(1, 2, 3), default=1, help="Baidu upload conflict strategy")
    sync.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu delete async mode")
    sync.set_defaults(func=cmd_sync)

    batch = subparsers.add_parser("batch", help="Run batch upload, download, or delete from a text list")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)

    batch_delete = batch_sub.add_parser("delete", help="Delete remote paths from a list file")
    batch_delete.add_argument("list_file")
    batch_delete.add_argument("--recursive", action="store_true", help="Allow deleting directories")
    batch_delete.add_argument("--yes", action="store_true", help="Do not prompt before deleting")
    batch_delete.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    batch_delete.add_argument("--allow-app-root", action="store_true", help="Allow deleting /apps/{app_name}")
    batch_delete.add_argument("--async-mode", type=int, choices=(0, 1, 2), default=1, help="Baidu filemanager async mode")
    batch_delete.add_argument("--ondup", choices=ONDUP_CHOICES, default="fail", help="Baidu duplicate handling")
    batch_delete.set_defaults(func=cmd_batch_delete)

    batch_download = batch_sub.add_parser("download", help="Download remote paths from a list file")
    batch_download.add_argument("list_file")
    batch_download.add_argument("local_dir")
    batch_download.add_argument("--resume", action="store_true", help="Resume from existing .part files")
    batch_download.add_argument("--overwrite", action="store_true", help="Overwrite existing destination files")
    batch_download.set_defaults(func=cmd_batch_download)

    batch_upload = batch_sub.add_parser("upload", help="Upload local paths from a list file")
    batch_upload.add_argument("list_file")
    batch_upload.add_argument("remote_dir")
    batch_upload.add_argument("--rtype", type=int, choices=(1, 2, 3), default=1, help="Baidu upload conflict strategy")
    batch_upload.add_argument("--skip-same", action="store_true", help="Skip directory files whose remote size matches")
    batch_upload.set_defaults(func=cmd_batch_upload)

    selftest = subparsers.add_parser("selftest", help="Run local non-network tests")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BaiduNetdiskError as exc:
        eprint(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
