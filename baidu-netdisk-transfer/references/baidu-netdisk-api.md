# Baidu Netdisk Open Platform API Notes

Use these notes when modifying `scripts/baidu_netdisk.py` or explaining behavior.

## OAuth Device Flow

- Device code: `GET https://openapi.baidu.com/oauth/2.0/device/code`
- Required params: `response_type=device_code`, `client_id={AppKey}`, `scope=basic,netdisk`
- Token polling: `GET https://openapi.baidu.com/oauth/2.0/token`
- Required params: `grant_type=device_token`, `code={device_code}`, `client_id={AppKey}`, `client_secret={SecretKey}`
- Refresh: same token endpoint with `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`
- Poll no faster than the returned `interval`; never below 5 seconds.
- Access tokens last 30 days. Refresh tokens are single use; save the new refresh token only after a successful refresh response.

## Upload Flow

All upload paths must be under `/apps/{app_name}` for normal third-party apps.

1. Compute MD5 block list using 4 MiB chunks. For a file <= 4 MiB, the block list has one MD5: the whole-file MD5.
2. Precreate:
   - `POST https://pan.baidu.com/rest/2.0/xpan/file?method=precreate&access_token=...`
   - Form body: `path`, `size`, `isdir=0`, `autoinit=1`, `rtype`, `block_list`, optional `content-md5`, `slice-md5`
   - Response gives `uploadid` and a `block_list` of part indexes to upload.
3. Locate upload host:
   - `GET https://d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload&appid=250528&access_token=...&path=...&uploadid=...&upload_version=2.0`
   - Use an HTTPS value from `servers`.
4. Upload parts:
   - `POST {upload_host}/rest/2.0/pcs/superfile2?method=upload&access_token=...&type=tmpfile&path=...&uploadid=...&partseq=N`
   - Multipart form field name is `file`.
5. Create file:
   - `POST https://pan.baidu.com/rest/2.0/xpan/file?method=create&access_token=...`
   - Form body: `path`, `size`, `isdir=0`, `rtype`, `uploadid`, `block_list`

Create directories with the same create endpoint using `isdir=1`. Treat `errno=-8` as "already exists" only when creating expected directories.

## List, Metadata, and Download

- List direct children:
  - `GET https://pan.baidu.com/rest/2.0/xpan/file?method=list&dir=...&access_token=...`
- Typed list helpers:
  - `GET https://pan.baidu.com/rest/2.0/xpan/file?method=doclist&parent_path=...&recursion=0|1&access_token=...`
  - `GET https://pan.baidu.com/rest/2.0/xpan/file?method=imagelist&parent_path=...&recursion=0|1&access_token=...`
  - `GET https://pan.baidu.com/rest/2.0/xpan/file?method=videolist&parent_path=...&recursion=0|1&access_token=...`
- Recursively list a directory:
  - `GET https://pan.baidu.com/rest/2.0/xpan/multimedia?method=listall&path=...&recursion=1&start=...&limit=1000&access_token=...`
  - Respect the documented frequency suggestion of no more than about 8-10 `listall` calls per minute.
- Get download link:
  - `GET https://pan.baidu.com/rest/2.0/xpan/multimedia?method=filemetas&fsids=[fs_id]&dlink=1&access_token=...`
- Download:
  - Append `access_token` to the returned `dlink`.
  - Set header `User-Agent: pan.baidu.com`.
  - Follow redirects.
  - `dlink` expires after 8 hours.
  - Range requests are supported for resume.

## File Management

All normal third-party app operations should stay below `/apps/{app_name}`.

- Create directory:
  - `POST https://pan.baidu.com/rest/2.0/xpan/file?method=create&access_token=...`
  - Form body: `path`, `isdir=1`, `rtype`
- Filemanager endpoint:
  - `POST https://pan.baidu.com/rest/2.0/xpan/file?method=filemanager&opera={copy|delete|move|rename}&access_token=...`
  - Form body: `async=0|1|2`, `filelist={json}`, optional `ondup=fail|newcopy|overwrite|skip`
- Delete:
  - `filelist` is a JSON string array of absolute paths, for example `["/apps/App/a.txt"]`.
  - The script requires `--recursive` for directories and asks for confirmation unless `--yes` or `--dry-run` is used.
- Copy/move:
  - `filelist` is a JSON array of objects: `{"path": "/apps/App/a.txt", "dest": "/apps/App/archive", "newname": "a.txt"}`.
  - `dest` is the destination directory, not the full destination file path.
- Rename:
  - `filelist` is a JSON array of objects: `{"path": "/apps/App/a.txt", "newname": "b.txt"}`.
  - `newname` must be one path component, not a path.

## Search, Quota, User Info, and Share

- Filename search:
  - `GET https://pan.baidu.com/rest/2.0/xpan/file?method=search&key=...&dir=...&recursion=0|1&access_token=...`
  - Optional `page` and `num` paginate results.
- Quota:
  - `GET https://pan.baidu.com/api/quota?checkfree=1&checkexpire=1&access_token=...`
- User info:
  - `GET https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo&access_token=...`
- Share link:
  - `POST https://pan.baidu.com/rest/2.0/xpan/share?method=rapidshare&access_token=...`
  - Form body: `fsid_list`, `period`, `pwd`
  - `fsid_list` is a JSON string array whose elements are strings, for example `["123","456"]`.
  - In live testing, this direct endpoint returned HTTP 404 for the configured Open Platform app. The official Baidu Netdisk MCP documents a `file_sharelink_set` tool through `https://mcp-pan.baidu.com/sse?access_token=...`; standalone OAuth REST sharing should be treated as best-effort and permission/API-availability dependent.

## Sync and Verification

- `sync` is local-to-remote only. It compares size by default and optionally local mtime.
- `sync --delete-remote` deletes remote paths missing locally; keep the dry-run default workflow for safety.
- `verify` compares sizes and optionally MD5 values only when metadata contains a plain 32-character hex content MD5. Baidu may omit MD5 or return an internal object hash that is not comparable to the local file MD5.

## Common Errors

- `-6`: invalid or expired token, or missing `netdisk` scope.
- `-7`: invalid file or directory name, or no access to path.
- `-8`: path already exists.
- `-9` / `31066`: file or directory does not exist.
- `-10`: cloud capacity is full.
- `31024`: upload permission not enabled.
- `31034`: `listall` frequency limit; slow down.
- `31190`: object key missing, often caused by missing or incorrect part upload.
- `31299`: first part is smaller than 4 MiB for a multipart upload.
- `31326`: anti-hotlink protection; verify `User-Agent: pan.baidu.com`.
- `31355`: usually an invalid `uploadid`.
- `31360`: download link expired; request a fresh dlink.
- `31363`: missing part.
- `31364`: part size exceeds limit.
- `31365`: file size exceeds account limit.
