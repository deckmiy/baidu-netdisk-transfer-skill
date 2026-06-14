# Baidu Netdisk Transfer Skill

这个目录包含 Codex Skill：`baidu-netdisk-transfer`。它通过百度网盘开放平台应用管理 `/apps/{app_name}` 下的网盘文件，脚本只依赖 Python 3 标准库。

## 功能状态

已在真实百度网盘账号下端到端测试：

- 配置与授权：`config set`、`auth device`、`auth status`
- 上传下载：单文件上传、目录递归上传、`--skip-same`、单文件下载、目录下载、`--resume`、`--overwrite`
- 浏览查询：`list`、`list --recursive`、`tree`、`search`、`stat`、`stat --dlink`、`exists`
- 分类列表：`doclist`、`imagelist`、`videolist`
- 远端管理：`mkdir`、`copy`、`move`、`rename`、`delete`
- 批量操作：`batch upload`、`batch download`、`batch delete`
- 同步校验：`sync --dry-run`、`sync --delete-remote`、`verify`、`verify --md5`
- 账号信息：`quota`、`uinfo`

已知限制：

- `share` 会解析路径到 `fs_id` 并尝试创建分享链接，但当前测试账号直连 `xpan/share` 返回 HTTP 404。官方百度网盘 MCP 文档里的分享能力走远程 SSE 服务， standalone Open Platform CLI 里先视为权限或接口可用性受限。
- `verify --md5` 只在百度返回标准 32 位十六进制内容 MD5 时比较。若返回的是百度内部对象 hash，脚本会跳过 MD5 比较，只保留大小校验。
- 空文件上传不受当前开放平台上传流程支持，脚本会直接报错。

## 安装

如果使用压缩包，先解压 `baidu-netdisk-transfer-skill.zip`，然后复制 `baidu-netdisk-transfer` 文件夹到用户级 Codex skills 目录。

Windows:

```powershell
$skills="$env:USERPROFILE\.codex\skills"
New-Item -ItemType Directory -Force $skills
Copy-Item ".\baidu-netdisk-transfer" $skills -Recurse -Force
```

macOS / Linux:

```bash
mkdir -p ~/.codex/skills
cp -R ./baidu-netdisk-transfer ~/.codex/skills/
```

复制后重启 Codex。

## 首次配置

应用名称必须和百度网盘开放平台控制台里的产品名称一致。普通第三方应用的文件通常只能放在 `/apps/{app_name}` 下。

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py config set --app-key "你的AppKey" --secret-key "你的SecretKey" --app-name "AgentDrive"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py auth device
```

`auth device` 会输出 `verification_url`、`user_code` 和可能的 `qrcode_url`。打开链接或扫码完成授权后，token 会保存到：

- Windows: `%APPDATA%\baidu-netdisk-transfer\config.json`
- macOS / Linux: `~/.config/baidu-netdisk-transfer/config.json`

不要提交或分享这个配置文件。

## 常用命令

上传和下载：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py upload "D:\data\report.pdf"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py upload "D:\data\project" --remote "/apps/AgentDrive/project" --skip-same
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py download "/apps/AgentDrive/project" "D:\downloads\project" --resume
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py download "/apps/AgentDrive/report.pdf" "D:\downloads\" --overwrite
```

查看、搜索和元信息：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py list "/apps/AgentDrive"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py list "/apps/AgentDrive" --recursive
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py tree "/apps/AgentDrive"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py search "report" --remote-dir "/apps/AgentDrive" --recursive
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py stat "/apps/AgentDrive/report.pdf"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py exists "/apps/AgentDrive/report.pdf"
```

远端文件管理：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py mkdir "/apps/AgentDrive/archive"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py copy "/apps/AgentDrive/report.pdf" "/apps/AgentDrive/archive/"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py move "/apps/AgentDrive/report.pdf" "/apps/AgentDrive/archive/report.pdf"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py rename "/apps/AgentDrive/archive/report.pdf" "report-final.pdf"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py delete "/apps/AgentDrive/archive/report-final.pdf" --dry-run
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py delete "/apps/AgentDrive/archive/report-final.pdf" --yes
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py delete "/apps/AgentDrive/archive" --recursive --dry-run
```

同步、校验和批量：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py sync "D:\data\project" "/apps/AgentDrive/project" --dry-run
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py sync "D:\data\project" "/apps/AgentDrive/project" --delete-remote --yes
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py verify "D:\data\project" "/apps/AgentDrive/project"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py batch upload ".\upload-list.txt" "/apps/AgentDrive/batch"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py batch download ".\download-list.txt" "D:\downloads\batch"
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py batch delete ".\delete-list.txt" --dry-run
```

账号信息和分享尝试：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py quota
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py uinfo
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py share "/apps/AgentDrive/report.pdf" --period 7 --password 1234
```

## 安全边界

- 所有路径型操作默认限制在 `/apps/{app_name}` 下。
- `delete` 删除目录必须加 `--recursive`，删除前默认确认。先用 `--dry-run` 查看将要删除的路径。
- `delete` 默认拒绝删除 `/apps/{app_name}` 根目录，除非显式加 `--allow-app-root`。
- `sync` 是本地到远端的单向同步。`sync --delete-remote` 会删除网盘中本地不存在的路径，建议先执行 `--dry-run`。
- `copy`、`move`、`rename` 支持百度的冲突策略：`--ondup fail|newcopy|overwrite|skip`。
- HTTP 错误里的 `access_token` 会被脚本脱敏，测试日志也不要公开上传。

## 自检和实测

本地无网络自检：

```powershell
python .\baidu-netdisk-transfer\scripts\baidu_netdisk.py selftest
```

Skill 结构校验：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "$env:USERPROFILE\.codex\skills\baidu-netdisk-transfer"
```

最近一次真实网盘测试结果：

- 测试时间：2026-06-14
- 结果：`48/48` 按预期通过，`failed=0`
- 其中 `share` 为预期不可用项，原因是当前直连 `xpan/share` 返回 HTTP 404
- 测试远端目录已删除并确认不存在
- 日志：`live-test-logs/baidu-live-test-20260614-005436.log`

## 打包

修改 skill 或 README 后重新生成压缩包：

```powershell
Compress-Archive -Path "$env:USERPROFILE\.codex\skills\baidu-netdisk-transfer",".\README.md" -DestinationPath ".\baidu-netdisk-transfer-skill.zip" -Force
```

压缩包应包含：

```text
baidu-netdisk-transfer/SKILL.md
baidu-netdisk-transfer/agents/openai.yaml
baidu-netdisk-transfer/references/baidu-netdisk-api.md
baidu-netdisk-transfer/scripts/baidu_netdisk.py
README.md
```

更多接口细节见 `baidu-netdisk-transfer/references/baidu-netdisk-api.md`。
