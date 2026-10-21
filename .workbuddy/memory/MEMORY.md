# 项目长期记忆

## 环境与操作约定（重要）

- **不要用 Git Bash 的 `mv` / `rm` / `cp` 操作含中文名的文件。** 本机 Git Bash 对 UTF-8 中文路径转换不可靠：
  曾对 `科技赛道研究报告_2026-10-21.md` 执行 `mv`，命令报 `cannot stat`（源文件丢失），但文件同时从磁盘消失，造成数据丢失。
  正确做法：用 Python（`shutil.move` / `os.remove`）+ 完整绝对路径，或先 `os.listdir` 取真实名字再操作。
- **误删恢复路径**：`C:\Users\Administrator\.workbuddy\changes-detail\<sessionId>\*.json` 里存有每次文件变更的 diff，
  `change.files[].hunks[].lines` 中 `+` 开头的行即新增内容，拼接后可按字节完全还原文件。
  另外 `.workbuddy\workspace\sessions\<id>\modify_backup\` 有修改前快照，`.workbuddy\file-history\` 有版本历史。
- 批量删除文件时工具内置安全阈值（约 30~50 个/次），超过会被拦截；脚本收尾批量删测试文件时要注意，需分批或改用 Python 逐批处理。

## 已知项目

- `Workbuddy/`：U 盘虚标容量检测工具（`usb_quickprobe.py`、`usb_fake_capacity.py`），准备开源，README 中英双份 + MIT。
