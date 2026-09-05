---
name: gafdvel
description: >-
  Maintain and package the gafdvel Windows uninstaller (Geek-style, gafdvel.py →
  gafdvel.exe). Use when editing gafdvel.py, fixing install-size/icon/leftover
  scan bugs, packaging with PyInstaller, cleaning build junk, or the user says
  打包 / gafdvel / 卸载器.
---

# gafdvel

Geek 风格便携卸载器。源码单文件 `gafdvel.py`，产物根目录 `gafdvel.exe`。

## 仓库应保持干净

**保留**

| 路径 | 用途 |
|------|------|
| `gafdvel.py` | 唯一源码 |
| `gafdvel.spec` | PyInstaller 配置（相对路径） |
| `file_version_info.txt` | 版本资源，与 `VERSION` 同步 |
| `icons/gafdvel.ico` | 窗口/exe 图标 |
| `icons/gafdvel-source.png` | 图标源图（可再生成 ico） |
| `gafdvel.exe` | 发布产物（根目录一份即可） |
| `启动gafdvel.bat` | 启动 |
| `打包.bat` | 一键打包并清理 |
| `gafdvel.cer` | 自签证书公钥（如有） |
| `.gitignore` | 忽略 build 垃圾 |
| `.cursor/skills/gafdvel/` | 本 Skill |
| `.cursor/rules/gafdvel.mdc` | 项目规则 |

**打包后必须删除**：`build/`、`dist/`、`__pycache__/`。不要把 `dist\gafdvel.exe` 和根目录 exe 留两份。

## 打包流程

用户说「打包」时：

1. 停掉正在运行的 `gafdvel` 进程
2. 确认 `gafdvel.py` 里 `VERSION` 与 `file_version_info.txt` 一致
3. 运行：`打包.bat`（或等价：`pyinstaller --noconfirm gafdvel.spec` → 复制 `dist\gafdvel.exe` → 删 `build`/`dist`/`__pycache__`）
4. 回报根目录 `gafdvel.exe` 大小与修改时间

依赖：本机 `E:\ruanjian\Python` + PyInstaller；`pywin32`、`Pillow`。

## 改代码时注意

- **路径**：WinGet `Packages`/`Links` 是真安装，不是 ephemeral；InstallShield Installation Information 才是缓存。图标/卸载 exe 真实存在时，其父目录可直接作安装根（勿因中文名 vs `NXY_70_*` 代码目录名亲和度低而拒绝）
- **图标**：`extract_icon_image` 用黑白双底算 alpha，禁止品红抠图（会红边）
- **体积显示**：找不到安装目录 → 显示「未知」+ `_locate_failed`，禁止假「0 B（磁盘实测）」
- **UI**：tkinter + ttk；图标异步逐个加载，勿启动时卡死
- **权限**：`uac_admin=True` / `ensure_admin()`；双击应以管理员运行
- 改版本：同时改 `VERSION` 与 `file_version_info.txt` 四处版本号

## 调试

```powershell
python gafdvel.py
# 或
.\启动gafdvel.bat
```

幽灵条目（注册表在、文件没了）属正常；可用「强制删除并清理」，不要硬猜错误 InstallLocation。
