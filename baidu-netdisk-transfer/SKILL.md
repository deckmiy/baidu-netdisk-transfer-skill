---
name: baidu-netdisk-transfer
description: Manage Baidu Netdisk files through a Baidu Netdisk Open Platform app. Use when Codex needs to configure AppKey/SecretKey credentials, authorize with device-code OAuth, refresh tokens, upload or download files/directories, list or search Netdisk paths, create directories, delete/copy/move/rename remote files, inspect metadata, check existence, sync local directories to /apps/{app_name}, verify uploads, create share links, or inspect quota/user info using Baidu Netdisk Open Platform APIs.
---

# Baidu Netdisk Transfer

## Quick Start

Use `scripts/baidu_netdisk.py` for all live operations. The script uses only the Python standard library.

```bash
python /path/to/baidu-netdisk-transfer/scripts/baidu_netdisk.py config set --app-key APP_KEY --secret-key SECRET_KEY --app-name APP_NAME
python /path/to/baidu-netdisk-transfer/scripts/baidu_netdisk.py auth device
python /path/to/baidu-netdisk-transfer/scripts/baidu_netdisk.py upload ./local-file.zip
python /path/to/baidu-netdisk-transfer/scripts/baidu_netdisk.py download /apps/APP_NAME/local-file.zip ./downloads/
python /path/to/baidu-netdisk-transfer/scripts/baidu_netdisk.py list /apps/APP_NAME --recursive
```

Store configuration in the user config directory:

- Windows: `%APPDATA%/baidu-netdisk-transfer/config.json`
- Other systems: `~/.config/baidu-netdisk-transfer/config.json`

For tests or isolated runs, set `BAIDU_NETDISK_TRANSFER_CONFIG` to a custom config file path.

## Workflow

1. Configure credentials with `config set`. The `--app-name` must match the product name registered in Baidu Netdisk Open Platform because uploaded files must live under `/apps/{app_name}`.
2. Run `auth device`. Show the user the printed verification URL, user code, and QR code URL. The command polls until Baidu returns tokens or the device code expires.
3. Use `upload`, `download`, `list`, `search`, `sync`, or file-management commands. The script refreshes tokens automatically before API calls when a refresh token is available.
4. Keep remote paths under `/apps/{app_name}` unless the user explicitly knows their app has broader access. The script enforces this app-root boundary for all path-based operations.
5. Treat `delete`, `sync --delete-remote`, `move`, `rename`, and `share` as live remote mutations. Use `--dry-run` where available and ask before destructive operations unless the user clearly requested them.

## Commands

```bash
python scripts/baidu_netdisk.py config set --app-key APP_KEY --secret-key SECRET_KEY --app-name APP_NAME
python scripts/baidu_netdisk.py auth device
python scripts/baidu_netdisk.py auth status
python scripts/baidu_netdisk.py upload LOCAL_PATH [--remote REMOTE_PATH] [--rtype 1|2|3] [--skip-same]
python scripts/baidu_netdisk.py download REMOTE_PATH LOCAL_PATH [--resume] [--overwrite]
python scripts/baidu_netdisk.py list REMOTE_DIR [--recursive] [--method list|doclist|imagelist|videolist] [--json]
python scripts/baidu_netdisk.py tree REMOTE_DIR
python scripts/baidu_netdisk.py mkdir REMOTE_DIR
python scripts/baidu_netdisk.py stat REMOTE_PATH [--dlink]
python scripts/baidu_netdisk.py exists REMOTE_PATH
python scripts/baidu_netdisk.py search KEY [--remote-dir REMOTE_DIR] [--recursive]
python scripts/baidu_netdisk.py quota [--json]
python scripts/baidu_netdisk.py uinfo
python scripts/baidu_netdisk.py delete REMOTE_PATH... --recursive --dry-run
python scripts/baidu_netdisk.py delete REMOTE_PATH... --recursive --yes
python scripts/baidu_netdisk.py copy SOURCE TARGET [--parents] [--ondup fail|newcopy|overwrite|skip]
python scripts/baidu_netdisk.py move SOURCE TARGET [--parents] [--ondup fail|newcopy|overwrite|skip]
python scripts/baidu_netdisk.py rename REMOTE_PATH NEW_NAME
python scripts/baidu_netdisk.py verify LOCAL_PATH REMOTE_PATH [--md5]
python scripts/baidu_netdisk.py sync LOCAL_DIR REMOTE_DIR [--dry-run] [--delete-remote --yes]
python scripts/baidu_netdisk.py share REMOTE_PATH... [--period DAYS] [--password 1234]
python scripts/baidu_netdisk.py batch upload LIST_FILE REMOTE_DIR
python scripts/baidu_netdisk.py batch download LIST_FILE LOCAL_DIR
python scripts/baidu_netdisk.py batch delete LIST_FILE --recursive --dry-run
python scripts/baidu_netdisk.py selftest
```

Upload defaults:

- Files upload to `/apps/{app_name}/{filename}`.
- Directories upload recursively to `/apps/{app_name}/{directory_name}`.
- Relative `--remote` values are resolved below `/apps/{app_name}`.
- Empty files are rejected because the Open Platform upload API does not support empty-file upload.
- `--skip-same` skips files when the existing remote size already matches. This is a size check, not a byte-for-byte checksum.

Download defaults:

- Files are downloaded through `filemetas&dlink=1`, with `access_token` appended to the dlink and `User-Agent: pan.baidu.com`.
- Directories are listed through `listall&recursion=1` and downloaded while preserving paths relative to the requested remote directory.
- Downloads write to `*.part` first and rename on success. Use `--resume` to continue an existing part file.

Remote management defaults:

- `delete` refuses directories unless `--recursive` is set, refuses `/apps/{app_name}` unless `--allow-app-root` is set, and prompts unless `--yes` or `--dry-run` is used.
- `copy`, `move`, and `rename` use Baidu filemanager `ondup` strategies: `fail`, `newcopy`, `overwrite`, or `skip`.
- `sync` is one-way local-to-remote. It uploads missing or size-changed files. `--delete-remote` removes remote paths absent locally and prompts unless `--yes`.
- `verify` compares file sizes by default and can compare MD5 only when Baidu metadata exposes a plain 32-character content MD5. Some Baidu `md5` fields are internal object hashes and are skipped.
- `share` resolves paths to `fs_id` values, but Baidu's direct `xpan/share` endpoint may return HTTP 404 for standalone Open Platform apps. The official Baidu Netdisk MCP exposes sharing through its remote SSE service; treat CLI sharing as best-effort/permission-dependent.

## References

Read `references/baidu-netdisk-api.md` when changing API behavior, debugging Baidu errors, or explaining limits. It summarizes OAuth, upload, directory listing, file metadata, filemanager operations, search, quota, share links, and key error codes.
