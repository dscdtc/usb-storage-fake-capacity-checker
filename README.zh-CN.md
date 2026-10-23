# USB 虚标容量检测工具

[English](README.md) | **简体中文**

两个零依赖的 Windows 命令行工具，用于检测**虚标容量的 U 盘 / 存储卡**——那种标称 2 TB、实际只有 64 GB 的盘——并把它们安全地恢复成真实容量。

纯 Python 标准库，无需安装任何第三方包。

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)
[![在线体验](https://img.shields.io/badge/demo-%E5%9C%A8%E7%BA%BF%E4%BD%93%E9%AA%8C-4f46e5)](https://usb-health-check.app.workbuddy.host/?sharecode=B63UHQ3HVT)

---

## 为什么需要它

虚标盘的主控会在容量上撒谎。写入超过真实闪存容量的地址时，控制器要么把地址**回绕**到起始位置，要么**直接丢弃**写入——但操作系统一律报告"写入成功"。你往往在几个月后才发现：某个位置之后的文件全部损坏或消失。

2 TB 的 U 盘不可能只卖几十块。如果一个盘的容量可疑，且设备名是 `VendorCo ProductCode`、`Flash Disk`、`Generic Mass Storage` 这类无品牌签名，用之前先测一下。

| 标称 | 实际 | 典型症状 |
|---|---|---|
| 2 TB | 32 / 64 / 128 GB | 超出真实容量的文件消失或变成乱码 |
| 250 GB | 64 GB | 写入到 60 GB 左右文件系统开始损坏 |
| 512 GB | 8 GB | 写入速度骤降，随后丢数据 |

---

## 两个工具

| 工具 | 作用 | "2TB" 盘耗时 | 写入量 |
|---|---|---|---|
| `usb_quickprobe.py` | 随机抽样检测地址映射，二分定位精确容量边界 | **10 秒 ~ 3 分钟** | 约 2 MB |
| `usb_fake_capacity.py` | 顺序写满 + 回读校验（h2testw 式），并可重新分区 | 40 ~ 80 小时 | 全盘容量 |

用 `usb_quickprobe.py` 判断盘**是否虚标、真实多大**；只有在需要逐字节证明时才用 `usb_fake_capacity.py` 的全量校验，它真正的价值在 `restore` 子命令。

### 网页版 —— 不用装东西，也不用开终端

`web/` 是一个零依赖的单页应用，把整个流程搬进浏览器：
风险速诊 → 取证命令生成 → **真实容量计算** → 恢复方案 → 可分享的结果卡片。

![U盘体检台 网页版](docs/shot-home.png)

直接打开 `web/index.html` 即可，也可以访问在线版：**<https://usb-health-check.app.workbuddy.host/?sharecode=B63UHQ3HVT>**

它**不能**读裸扇区——浏览器没有这个能力，假装能就是骗人。所以网页做的是它擅长的事：解析命令行工具产出的 JSON 报告，在浏览器里跑同一套容量数学（二分区间、抽样的 Wilson 95% 区间），并把计算结果直接变成可执行的 `diskpart` 脚本和 `restore` 命令。

第 ④ 步把一次测量变成可以直接执行的东西：

![生成的 diskpart 脚本与恢复命令](docs/shot-restore.png)

---

## 原理

### 为什么"写文件再读回来"证明不了什么

写完立刻读，读到的是 Windows 文件缓存；即使是真实回读，虚标盘也会通过同一套映射把数据还给你——逻辑块 `L` 映射到物理块 `L mod R`（`R` 为真实容量），读取走同样的映射，自然"完全一致"。

### 1. 稀疏别名探测 —— 快的那条路

向全盘随机挑选的 N 个 4 KiB 扇区各写入一个**唯一且可自识别的标记**，然后全部回读：

* **真盘**：每个扇区读回的正是自己那份标记。
* **虚标盘（回绕型）**：超过 `R` 的写入落在已属于低位逻辑扇区的物理块上，那个低位扇区就会返回*别人的*标记。解析标记头即可得知 `a ≡ b (mod R)`，对所有差值取 `gcd` 就能反推 `R`。
* **虚标盘（丢写型）**：超过 `R` 的写入被直接丢弃，扇区读回全零或旧数据。

2 TB 地址空间上抽 514 个点，几乎必然命中虚假区域，而写入量只有 **约 2 MB**。

### 2. 二分定位 —— 拿到精确数字

当盘属于*丢写型*时，`写入(LBA) → 回读 → 比对` 这个判断是**单调**的：真实容量以下成立，以上不成立。于是找容量变成约 30 次单扇区探测（即 log₂ 扇区数）：

```
lo = 32 MiB（必定可读写）
hi = 最后一个扇区（必定失效）
while hi - lo > 1:
    mid = (lo + hi) // 2
    if 写入并回读成功(mid): lo = mid
    else:                   hi = mid
真实容量 = hi * 4096
```

几秒出结果，精度到一个 4 KiB 扇区。如果发现该盘其实是回绕型，工具会自动识别并提示改用抽样模式。

### 3. 必须绕过系统缓存

这是多数脚本踩的坑：Windows 上写完立刻读会命中文件缓存，虚标盘看起来完美无缺。两个工具都用 `FILE_FLAG_NO_BUFFERING` 打开文件/设备，并用 4 KiB 对齐缓冲区，强制直接从设备读。

同一支盘实测：**缓存读 263 MiB/s，直读 20 MiB/s** —— 13 倍的差距，足以把整个骗局掩盖掉。

---

## 环境要求

* Windows 10 / 11（依赖 `diskpart`、`ctypes` Win32 API、PowerShell 枚举磁盘）
* Python 3.9+（无需任何第三方包）
* **管理员权限**：裸设备访问与重新分区需要（`usb_quickprobe.py` 与 `restore`）

`usb_fake_capacity.py check` 走文件层，**不需要**管理员权限。

---

## 快速开始

```bash
git clone https://github.com/dscdtc/usb-storage-fake-capacity-checker.git
cd usb-storage-fake-capacity-checker
```

```bash
# 只读识别：这是什么盘，设备名可疑吗？
python usb_fake_capacity.py identify E

# 快速判断：是不是虚标，大概真实多大？
python usb_quickprobe.py --letter E --dry-run     # 只输出计划，不写盘
python usb_quickprobe.py --letter E --yes        # 约 2MB 写入，10 秒~3 分钟

# 精确到扇区的容量边界
python usb_quickprobe.py --letter E --locate --yes

# 恢复真实容量（先预览！）
python usb_fake_capacity.py restore --disk 3 --size-mb 59387 --letter E --dry-run
python usb_fake_capacity.py restore --disk 3 --size-mb 59387 --letter E --yes
```

> `usb_quickprobe.py` 和 `restore` 需要**管理员终端**。权限不足时会干净地报错退出，不会写出半截数据。

---

## 用法

### `usb_quickprobe.py` —— 快速探测

```
python usb_quickprobe.py --letter E [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--letter L` | — | 要探测的盘符 |
| `--disk N` | — | 用物理磁盘号代替盘符 |
| `--count N` | `512` | 抽样扇区数量 |
| `--locate` | 关 | 二分查找精确容量边界 |
| `--backup FILE` | — | 先备份被写扇区，结束后写回 |
| `--dry-run` | 关 | 只打印计划，不写盘 |
| `--seed N` | 随机 | 固定抽样种子，便于复现 |
| `--force` | 关 | 允许非 USB 设备 |
| `--yes`, `-y` | 关 | 跳过交互确认 |

退出码：`0` 未发现异常 · `3` 检测到虚标 · `2` 拒绝执行 / 权限不足。

### `usb_fake_capacity.py` —— 全量校验与修复

```
python usb_fake_capacity.py identify D: E:
python usb_fake_capacity.py check  E [--quick | --max-gb N] [--resume]
python usb_fake_capacity.py restore --disk N --size-mb M --letter L
```

| 子命令 | 用途 |
|---|---|
| `identify` | 只读：型号、总线、上报/可用容量、风险提示 |
| `check` | 顺序写入 + 回读校验。`--quick` 只测前 16 GiB，`--max-gb N` 限制测试量，`--resume` 对已写入的数据只做回读 |
| `restore` | 用 `diskpart` 清盘并按真实容量创建单个分区 |

`check` 会写入 `_wbchk_XXXXX.bin`，每 128 MiB 落盘一次，结束后自动删除；进度状态存在 `%TEMP%`，中断后可用 `--resume` 续测。

---

## 实测案例

一支标称 **250 GB**、型号 `Flash Disk 3.0`、exFAT、空盘：

![usb_quickprobe.py --locate 在虚标 250GB U 盘上的实际输出：真实容量 63.54 GB，仅为标称的 23.67%](docs/locate-demo.svg)

二分把边界锁定到单个 4 KiB 扇区：真实闪存 **63.54 GB（59.18 GiB）**，仅为标称容量的 23.67%。超过这个位置写入的数据全部被静默丢弃。

另一支标称 **2 TB**（`VendorCo ProductCode`）：514 个抽样中 501 个异常，首个失效位置在 68.44 GiB —— 一颗 64 GB 级闪存。

两支盘都没有跑全量写入，各自耗时不到两分钟。

---

## 与同类工具对比

| 工具 | 速度 | 开源 | Windows 原生 | 能给出精确容量 |
|---|---|---|---|---|
| **usb_quickprobe.py** | 分钟级 | ✅ MIT | ✅ | ✅（二分） |
| h2testw | 小时级 | ❌ | ✅ | ✅ |
| [f3](https://github.com/AltraMayor/f3)（`f3probe`、`f3fix`） | 分钟级 | ✅ GPLv3 | ❌ 需 Linux/WSL | ✅ |
| ValiDrive（GRC） | 分钟级 | ❌ 免费软件 | ✅ | ❌ 只能定性 |
| ChipGenius | 秒级 | ❌ 免费软件 | ✅ | ❌ 读闪存 ID，需人工判读 |

如果你在 Linux 上，请优先用 `f3probe`——成熟且久经考验。本项目存在的意义，是让 Windows 用户不必装 WSL 或做 Linux 启动盘，也能获得同样的速度。

---

## 安全须知

动手前请先读这一段。

* **`usb_quickprobe.py` 会写裸扇区。** 它会在全盘伪随机偏移处写入 `--count` 个 4 KiB 扇区（默认约 2 MB）。盘里有数据时，这些数据有风险。可用 `--backup` 备份并尽量写回（尽力而为——虚标盘写回去未必落在原处）。请优先在空盘或可格式化的盘上使用。
* **`restore` 会清空整块磁盘。** 它执行 `diskpart` → `clean` → `create partition` → `format`。非 USB 磁盘、超上报容量的分区、32GB 以上的 FAT32 都会被拒绝（除非 `--force`）。务必先 `--dry-run` 看清它要执行的脚本。
* **重新分区不会创造容量。** 缺失的闪存物理上就不存在。`restore` 只是把卷缩小到真实值，让盘不再毁数据——这就是全部收益。
* **探测期间卷会被临时卸载。** 盘符会从资源管理器消失几秒，工具退出后自动恢复。请先关闭占用该盘的程序。
* 执行 `restore` 前务必核对**磁盘号**。可用 `Get-Disk` 查看。擦错盘不可恢复。

---

## 局限性

* **抽样不等于证明。** 抽样通过只意味着"514 个采样点里没发现作弊"，不代表"真的是 2 TB"。只有顺序写满全盘才能证明，因此保留了 `usb_fake_capacity.py check`。
* **二分法依赖丢写特性。** 工具会在两端先验证该假设，若不符合会以退出码 `3` 中止并提示改用抽样模式。
* **回绕型靠碰撞反推容量。** 若该盘回绕且两个采样点没有碰撞，就推不出精确 `R`，可加大 `--count`。
* **仅支持 Windows。** 裸设备路径（`\\.\PHYSICALDRIVEn`）、`FSCTL_LOCK_VOLUME`、`diskpart` 都是 Windows 专有。代码不长，欢迎提交移植 PR。
* **并非所有假盘都能这样测出。** 少数主控会缓存最后写入的块或读取时编造数据。结果自相矛盾时请回到全量校验。

---

## 常见问题

**能把 2TB 找回来吗？**
不能。闪存本来就不存在，只能让盘如实反映自己。

**测完要格式化吗？**
建议。探测会在盘上散布标记，文件系统并不知情。`restore` 本来就会重新格式化。

**会不会把盘刷成砖？**
探测和重新分区都是常规操作，不碰固件。真正刷固件的"修复"（量产工具如 AlcorMP、ChipsBank APTool、SMI MPTool）才有变砖风险——本项目刻意不涉及。

**盘是真的，但容量比标称少一点？**
正常。厂商用十进制 GB（10⁹），Windows 显示 GiB（2³⁰）。"64 GB" 的盘显示约 59.6 GiB。

**能用它对机械硬盘/SSD 跑吗？**
加 `--force` 技术上可以。但别这么干，它会写扇区。

---

## 参与贡献

欢迎提交 Issue 和 PR——尤其是其他假盘主控的实测数据，以及 macOS/Linux 移植。提交假盘报告时请附上：设备型号字符串、上报容量、探测 JSON，如果跑过 `--locate` 再附上精确边界。

---

## 许可证

[MIT](LICENSE)

本工具用于检测与恢复**你自己的**存储设备。它会向磁盘写入数据，指错盘就可能造成数据损毁。不提供任何担保；执行 `restore` 前请再次核对磁盘号。
