#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb_fake_capacity.py —— U 盘 / SD 卡“虚标容量”检测与真实容量恢复工具（Windows）

原理
----
虚标盘的真实闪存容量远小于控制器上报的容量。控制器收到超出真实容量的写入后，
通常会把 LBA 取模回绕到起始位置，覆盖之前写下的数据（写入“看起来成功”）。

本工具采用 h2testw 同款思路：
  阶段一 顺序写入：从 0 开始按块写入可校验的伪随机数据（每块 fsync 落盘）。
  阶段二 顺序回读：重新打开文件逐块比对。若在偏移 X 处开始不一致 / 读失败 / 文件被截断，
                    说明 X 之后的数据已被回绕覆盖 —— 真实容量约为 X。
  阶段三 可选恢复：按真实容量重新分区格式化（diskpart），让盘变得“小而可靠”。

命令
----
  python usb_fake_capacity.py identify D: E:                     # 只读：识别设备（安全）
  python usb_fake_capacity.py check D: --quick                   # 初筛：写校验前 16GiB
  python usb_fake_capacity.py check E: --max-gb 32               # 只测前 32GiB
  python usb_fake_capacity.py check E:                           # 全量（2TB 盘可能十几小时）
  python usb_fake_capacity.py check E: --resume                  # 中断后只做回读校验
  python usb_fake_capacity.py restore --disk 3 --size-gb 58 --letter E --dry-run
  python usb_fake_capacity.py restore --disk 3 --size-gb 58 --letter E --yes

注意
----
* check 会在盘上写入临时文件 _wbchk_XXXXX.bin，结束后自动删除（--keep 可保留）。
* restore 会清空整块物理磁盘的所有分区，不可逆，需管理员权限并显式 --yes。
* 恢复容量 ≠ 增加容量：只能把盘改成真实可用的容量，虚标部分本就物理不存在。
"""

import argparse
import base64
import ctypes
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime

CHUNK = 4 * 1024 * 1024          # 4 MiB 校验粒度
BLOCK = 128 * 1024 * 1024        # 128 MiB 一个文件（便于 fsync 与断点续测）
MARGIN = 64 * 1024 * 1024        # 写入时预留的安全空间
PFX = "_wbchk_"
KIB, MIB, GIB, TIB = 1024, 1024 ** 2, 1024 ** 3, 1024 ** 4

DRIVE_TYPE = {0: "UNKNOWN", 1: "NO_ROOT", 2: "REMOVABLE", 3: "FIXED",
              4: "REMOTE", 5: "CDROM", 6: "RAMDISK"}


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def fmt_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit, div in (("TiB", TIB), ("GiB", GIB), ("MiB", MIB), ("KiB", KIB)):
        if abs(n) >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


def fmt_gb(n):
    return f"{n / GIB:.2f} GiB"


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def norm_letter(s):
    s = str(s).strip().upper().rstrip(":\\/")
    if len(s) != 1 or not s.isalpha():
        raise argparse.ArgumentTypeError(f"盘符非法: {s}")
    return s


def ps(cmd):
    """执行 PowerShell 片段，返回 stdout（用 -EncodedCommand 避免引号/换行解析问题）。"""
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
              "$ProgressPreference='SilentlyContinue'\n"
              "$ErrorActionPreference='SilentlyContinue'\n") + cmd
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", enc],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    return (p.stdout or "").strip()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 设备识别（只读）
# ----------------------------------------------------------------------------
def volume_info(letter):
    root = f"{letter}:\\"
    if not os.path.exists(root):
        raise RuntimeError(f"盘符 {letter}: 不存在或未插入")

    dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
    label_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint()
    maxlen = ctypes.c_uint()
    flags = ctypes.c_uint()
    ctypes.windll.kernel32.GetVolumeInformationW(
        root, label_buf, 261, ctypes.byref(serial), ctypes.byref(maxlen),
        ctypes.byref(flags), fs_buf, 261)

    try:
        usage = shutil.disk_usage(root)
        total, free = usage.total, usage.free
    except OSError:
        total, free = 0, 0

    return {
        "letter": letter,
        "root": root,
        "drive_type": DRIVE_TYPE.get(dtype, str(dtype)),
        "removable": dtype == 2,
        "label": label_buf.value,
        "fs": fs_buf.value,
        "total": total,
        "free": free,
    }


def disk_map():
    """盘符 -> 物理磁盘信息（型号/总线/容量）。"""
    script = r"""
$rows = @()
foreach ($p in Get-Partition) {
  if ($p.DriveLetter) {
    $d = Get-Disk -Number $p.DiskNumber
    $rows += [pscustomobject]@{
      letter = [string]$p.DriveLetter
      disk   = [int]$p.DiskNumber
      part   = [int]$p.PartitionNumber
      model  = [string]$d.FriendlyName
      bus    = [string]$d.BusType
      size   = [double]$d.Size
      style  = [string]$d.PartitionStyle
    }
  }
}
@($rows) | ConvertTo-Json -Compress
"""
    out = ps(script)
    if not out:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {str(r.get("letter", "")).upper(): r for r in data if r}


SUSPICIOUS_TOKENS = ("vendorco", "productcode", "general", "generic", "usb disk",
                     "flash disk", "mass storage", "sd/mmc", "unknown", "no name")


def fingerprint_warnings(model, bus, total):
    warns = []
    m = (model or "").lower()
    if any(t in m for t in SUSPICIOUS_TOKENS):
        warns.append(f"设备名 “{model}” 属于无品牌主控常见签名，虚标概率高")
    if total >= 1024 * GIB and "vendorco" in m:
        warns.append("无品牌主控 + ≥1TB 容量，是虚标盘的典型组合")
    if bus and bus.upper() != "USB":
        warns.append(f"总线类型为 {bus}，不是 USB 移动设备")
    return warns


# ----------------------------------------------------------------------------
# 数据图案（可确定性重建，用于回读比对）
# ----------------------------------------------------------------------------
def chunk_data(block_idx, chunk_idx, size):
    rng = random.Random((block_idx * 1000003 + chunk_idx) ^ 0x9E3779B9)
    return rng.randbytes(size)


# ----------------------------------------------------------------------------
# 绕过系统文件缓存读取 —— 否则回读命中内存缓存，虚标盘会被误判为正常
# ----------------------------------------------------------------------------
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

GENERIC_READ = 0x80000000
FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
SECTOR = 4096


class RawReader:
    """FILE_FLAG_NO_BUFFERING 顺序读取，数据直接来自设备而非系统缓存。"""

    def __init__(self, path, buf_size=CHUNK):
        h = _k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING,
                             FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, None)
        if h == wintypes.HANDLE(-1).value or not h:
            raise OSError(ctypes.get_last_error(), f"无法以无缓存方式打开 {path}")
        self.h = h
        self.cap = buf_size
        self._raw = ctypes.create_string_buffer(buf_size + SECTOR)
        addr = ctypes.addressof(self._raw)
        self._addr = (addr + SECTOR - 1) // SECTOR * SECTOR

    def read(self, size):
        want = size if size % SECTOR == 0 else (size // SECTOR + 1) * SECTOR
        want = min(want, self.cap)
        got = wintypes.DWORD(0)
        ok = _k32.ReadFile(self.h, ctypes.c_void_p(self._addr), want,
                           ctypes.byref(got), None)
        if not ok:
            raise OSError(ctypes.get_last_error(), "ReadFile 失败")
        return ctypes.string_at(self._addr, got.value)[:size]

    def close(self):
        if getattr(self, "h", None):
            _k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ----------------------------------------------------------------------------
# check：写入 + 回读校验
# ----------------------------------------------------------------------------
def state_path(letter):
    return os.path.join(tempfile.gettempdir(), f"usb_capacity_state_{letter}.json")


def plan_blocks(target):
    sizes, rest = [], target
    while rest > 0:
        s = min(BLOCK, rest)
        sizes.append(s)
        rest -= s
    return sizes


_last_prog = [0.0]


def progress(tag, done, total, t0, force=False):
    now = time.time()
    if not force and now - _last_prog[0] < 0.2:
        return
    _last_prog[0] = now
    pct = (done / total * 100) if total else 0.0
    el = max(1e-6, now - t0)
    sp = done / el
    eta = (total - done) / sp if sp > 0 else 0
    sys.stdout.write(f"\r  {tag} {pct:5.1f}%  {fmt_bytes(done)}/{fmt_bytes(total)}  "
                     f"{sp / MIB:6.1f} MiB/s  ETA {fmt_dur(eta)}      ")
    sys.stdout.flush()


def phase_write(letter, target, keep):
    root = f"{letter}:\\"
    sizes = plan_blocks(target)
    state = {"letter": letter, "target": target, "block_sizes": sizes,
             "written": 0, "files": [], "write_error": None, "started": datetime.now().isoformat()}
    t0 = time.time()
    done = 0
    for idx, bsize in enumerate(sizes):
        path = os.path.join(root, f"{PFX}{idx:05d}.bin")
        partial = 0
        try:
            with open(path, "wb", buffering=0) as f:
                off = 0
                ci = 0
                while off < bsize:
                    n = min(CHUNK, bsize - off)
                    f.write(chunk_data(idx, ci, n))
                    off += n
                    partial = off
                    ci += 1
                    done += n
                    progress("写入", done, target, t0)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            state["write_error"] = {"file_index": idx,
                                    "offset": state["written"] + partial,
                                    "error": str(e)}
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            break
        state["files"].append({"index": idx, "name": os.path.basename(path), "size": bsize})
        state["written"] += bsize
    progress("写入", done, target, t0, force=True)
    print()
    return state


def phase_verify(letter, state, fast_read=False):
    root = f"{letter}:\\"
    total = sum(f["size"] for f in state["files"])
    base = 0
    first_bad = None
    bad_reason = None
    t0 = time.time()
    done = 0

    for f in state["files"]:
        path = os.path.join(root, f["name"])
        idx, fsize = f["index"], f["size"]
        fh = None
        try:
            if not fast_read:
                try:
                    fh = RawReader(path)
                except OSError:
                    fh = None
            if fh is None:
                fh = open(path, "rb", buffering=0)
            off = 0
            ci = 0
            while off < fsize:
                n = min(CHUNK, fsize - off)
                got = fh.read(n)
                if len(got) != n:
                    first_bad = base + off + len(got)
                    bad_reason = f"文件被截断：期望 {n} 字节，实际读到 {len(got)} 字节"
                    break
                exp = chunk_data(idx, ci, n)
                if got != exp:
                    diff = next(i for i in range(n) if got[i] != exp[i])
                    first_bad = base + off + diff
                    bad_reason = "回读内容与写入不一致（数据已被回绕覆盖）"
                    break
                off += n
                ci += 1
                done += n
                progress("校验", done, total, t0)
        except OSError as e:
            first_bad = base
            bad_reason = f"读取失败：{e}"
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

        if first_bad is not None:
            break
        base += fsize

    progress("校验", total, total, t0, force=True)
    print()
    return first_bad, bad_reason, total


def cmd_check(args):
    letter = args.letter
    info = volume_info(letter)
    mapping = disk_map().get(letter, {})
    print(f"\n=== 检测 {letter}: ===")
    print(f"  设备    : {mapping.get('model', '未知')}  (Disk {mapping.get('disk', '?')} / {mapping.get('bus', '?')})")
    print(f"  文件系统: {info['fs'] or '未知'}    卷标: {info['label'] or '(无)'}")
    print(f"  报告容量: {fmt_bytes(info['total'])}     可用: {fmt_bytes(info['free'])}")

    if not info["removable"] and not args.force:
        print("\n[拒绝] 该盘不是可移动设备。若确认无误请加 --force")
        return 2

    for w in fingerprint_warnings(mapping.get("model"), mapping.get("bus"), info["total"]):
        print(f"  [警告] {w}")

    # 目标容量
    free = info["free"]
    if args.quick:
        target = min(16 * GIB, max(0, free - MARGIN))
        print(f"\n  模式    : 初筛（只写校验前 {fmt_bytes(target)}）—— 可能漏检，正式结论请跑全量")
    elif args.max_gb:
        target = min(int(args.max_gb * GIB), max(0, free - MARGIN))
        print(f"\n  模式    : 限量测试 {args.max_gb} GiB")
    else:
        target = max(0, free - MARGIN)
        print(f"\n  模式    : 全量测试（写入 {fmt_bytes(target)}，耗时可能很长）")

    if target < 64 * MIB:
        print("\n[跳过] 可用空间不足 64MiB")
        return 2

    # 对齐到 4MiB，保证无缓存读取时按扇区对齐、不会读到 EOF 之外
    target = (int(target) // CHUNK) * CHUNK

    rec_gb = target / GIB
    print(f"  预计写入: {rec_gb:.2f} GiB；按 20MB/s 约需 {fmt_dur(target / (20 * MIB))}")

    if not args.yes:
        print("\n即将向该盘写入临时文件（会占满可用空间，已存在的文件不受影响）。")
        try:
            ans = input("继续？输入 yes 开始: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("yes", "y"):
            print("已取消，未做任何写入。")
            return 1

    # ---------- 写入 / 续测 ----------
    sp = state_path(letter)
    t_start = time.time()
    if args.resume and os.path.exists(sp):
        with open(sp, "r", encoding="utf-8") as f:
            state = json.load(f)
        print(f"\n  续测：载入状态，已写入 {fmt_bytes(state.get('written', 0))}，直接进入回读校验")
    else:
        if os.path.exists(sp):
            os.remove(sp)
        print()
        state = phase_write(letter, target, args.keep)
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    if not state["files"]:
        print("[失败] 一个块都没写成功，无法判断。可能是盘已损坏或只读。")
        return 2

    # ---------- 回读校验 ----------
    print("  开始回读校验（绕过系统缓存）..." if not args.fast_read
          else "  开始回读校验（使用系统缓存，速度更快但可能漏判）...")
    first_bad, reason, verified_total = phase_verify(letter, state, fast_read=args.fast_read)

    written = state["written"]
    reported = info["total"]

    # ---------- 结论 ----------
    if first_bad is not None:
        verdict, real = "FAKE", first_bad
    elif state.get("write_error"):
        real = state["write_error"].get("offset") or written
        verdict = "FAKE"
        reason = reason or f"写入过程中断：{state['write_error']['error']}"
    else:
        real = verified_total
        verdict = "PASS" if verified_total >= reported * 0.97 else "PARTIAL"

    print("\n" + "=" * 62)
    if verdict == "FAKE":
        print("  结论：虚标（假容量）")
        print(f"  异常位置：{fmt_bytes(real)}")
        print(f"  原因    ：{reason}")
        print(f"  真实容量：约 {fmt_gb(real)}  (写入到 {fmt_gb(written)} 都“成功”，但超出的部分是假的)")
    elif verdict == "PASS":
        print("  结论：在测试范围内未发现虚标")
        print(f"  已完整写入并校验 {fmt_gb(verified_total)} / 报告容量 {fmt_gb(reported)}")
    else:
        print("  结论：部分通过（只测了部分容量，结论不完整）")
        print(f"  已校验 {fmt_gb(verified_total)}，占报告容量 {verified_total / reported * 100:.1f}%")
    print(f"  耗时    ：{fmt_dur(time.time() - t_start)}")
    print("=" * 62)

    # ---------- 报告 ----------
    report = {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "盘符": f"{letter}:",
        "设备": mapping.get("model"),
        "物理磁盘": mapping.get("disk"),
        "总线": mapping.get("bus"),
        "文件系统": info["fs"],
        "报告容量": reported,
        "写入容量": written,
        "校验容量": verified_total,
        "首个异常偏移": first_bad,
        "异常原因": reason,
        "结论": verdict,
        "建议真实容量": real,
        "建议分区大小MB": int((real * 0.98) // MIB),
    }
    rp = os.path.join(os.getcwd(), f"usb_capacity_report_{letter}.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  报告已保存: {rp}")

    # ---------- 收尾 ----------
    if not args.keep:
        print("  清理临时文件 ...", end=" ", flush=True)
        for f in state["files"]:
            try:
                os.remove(os.path.join(f"{letter}:\\", f["name"]))
            except OSError:
                pass
        print("完成")
    else:
        print("  已保留临时文件（--keep），可用 --resume 复查")

    if os.path.exists(sp):
        try:
            os.remove(sp)
        except OSError:
            pass

    if verdict == "FAKE":
        disk = mapping.get("disk")
        size_mb = report["建议分区大小MB"]
        print("\n  恢复真实容量的下一步：")
        if disk is None:
            print("    未能取到物理磁盘号，请用管理员 PowerShell 执行 Get-Partition -DriveLetter " + letter)
        else:
            print(f"    python {os.path.basename(__file__)} restore --disk {disk} "
                  f"--size-mb {size_mb} --letter {letter} --dry-run   # 先预览")
            print(f"    python {os.path.basename(__file__)} restore --disk {disk} "
                  f"--size-mb {size_mb} --letter {letter} --yes       # 确认后执行（需管理员）")
    return 0 if verdict != "FAKE" else 3


# ----------------------------------------------------------------------------
# restore：按真实容量重新分区格式化
# ----------------------------------------------------------------------------
def cmd_restore(args):
    print(f"\n=== 恢复容量：Disk {args.disk} → {args.size_mb} MB ===")
    if args.size_mb < 64:
        print("[拒绝] 分区过小")
        return 2

    # 校验目标磁盘
    script = f"""
$d = Get-Disk -Number {args.disk}
if ($d) {{ [pscustomobject]@{{ model=[string]$d.FriendlyName; bus=[string]$d.BusType; size=[double]$d.Size }} | ConvertTo-Json -Compress }}
"""
    out = ps(script)
    info = json.loads(out) if out else {}
    if not info:
        print(f"[拒绝] 找不到 Disk {args.disk}")
        return 2
    print(f"  目标设备: {info.get('model')}  ({info.get('bus')}, {fmt_bytes(info.get('size', 0))})")

    if str(info.get("bus", "")).upper() != "USB" and not args.force:
        print("[拒绝] 该磁盘不是 USB 设备，为防止误伤系统盘已中止。确认无误请加 --force")
        return 2

    if args.size_mb * MIB > info.get("size", 0) * 1.02 and not args.force:
        print("[拒绝] 指定分区容量超过该磁盘上报容量")
        return 2

    fs = args.fs.lower()
    if fs == "fat32" and args.size_mb > 32 * 1024 and not args.force:
        print("[拒绝] FAT32 单分区上限 32GB，请改用 --fs exfat")
        return 2

    dp = f"""select disk {args.disk}
clean
create partition primary size={args.size_mb}
format fs={fs} quick label="{args.label}"
assign letter={args.letter}
"""
    print("\n  diskpart 脚本：")
    print("  " + "-" * 40)
    for line in dp.strip().splitlines():
        print("    " + line)
    print("  " + "-" * 40)
    print("  警告：此操作会清空该磁盘全部分区与数据，不可恢复。")

    if not args.yes:
        print("\n  [预览模式] 未执行。确认后加 --yes 重新运行。")
        return 0

    if not is_admin():
        print("\n[失败] 需要管理员权限。请右键“以管理员身份运行”终端后重试。")
        return 2

    tmp = os.path.join(tempfile.gettempdir(), "wb_diskpart.txt")
    with open(tmp, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write(dp)
    print("\n  执行 diskpart ...")
    p = subprocess.run(["diskpart", "/s", tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print((p.stdout or "").strip())
    try:
        os.remove(tmp)
    except OSError:
        pass
    if p.returncode != 0:
        print(f"[失败] diskpart 返回 {p.returncode}\n{p.stderr}")
        return 2

    print("\n  完成。当前卷状态：")
    print(ps(f"Get-Volume -DriveLetter {args.letter} | "
             f"Format-List DriveLetter,FileSystemLabel,FileSystem,Size,SizeRemaining"))
    print("\n  建议再跑一次 check 验证恢复后的稳定性：")
    print(f"    python {os.path.basename(__file__)} check {args.letter} --yes")
    return 0


# ----------------------------------------------------------------------------
# identify：只读识别
# ----------------------------------------------------------------------------
def cmd_identify(args):
    dm = disk_map()
    for letter in args.letters:
        try:
            info = volume_info(letter)
        except RuntimeError as e:
            print(f"\n[{letter}:] {e}")
            continue
        m = dm.get(letter, {})
        print(f"\n[{letter}:]")
        print(f"  设备      : {m.get('model', '未知')}")
        print(f"  物理磁盘  : Disk {m.get('disk', '?')} / {m.get('part', '?')}  ({m.get('bus', '?')})")
        print(f"  文件系统  : {info['fs'] or '未知'}   卷标: {info['label'] or '(无)'}")
        print(f"  类型      : {info['drive_type']}")
        print(f"  报告容量  : {fmt_bytes(info['total'])}   可用: {fmt_bytes(info['free'])}")
        for w in fingerprint_warnings(m.get("model"), m.get("bus"), info["total"]):
            print(f"  [风险] {w}")
    print("\n以上为只读信息，未写入任何数据。检测虚标请执行："
          f"\n  python {os.path.basename(__file__)} check {' '.join(args.letters)} --quick")
    return 0


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="U 盘/SD 卡虚标容量检测与真实容量恢复（Windows）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  identify D: E:\n"
               "  check D: --quick\n"
               "  check E: --max-gb 32\n"
               "  restore --disk 3 --size-mb 59000 --letter E --dry-run\n")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("identify", help="只读识别设备信息")
    p1.add_argument("letters", nargs="+", type=norm_letter)
    p1.set_defaults(func=cmd_identify)

    p2 = sub.add_parser("check", help="写入并回读校验，判断是否虚标")
    p2.add_argument("letter", type=norm_letter)
    p2.add_argument("--quick", action="store_true", help="初筛：只测前 16GiB")
    p2.add_argument("--max-gb", type=float, help="最多测试多少 GiB")
    p2.add_argument("--resume", action="store_true", help="沿用上次写入的数据，只做回读校验")
    p2.add_argument("--keep", action="store_true", help="保留临时测试文件")
    p2.add_argument("--fast-read", action="store_true",
                    help="回读时使用系统缓存（快，但可能把虚标盘误判为正常）")
    p2.add_argument("--force", action="store_true", help="允许对非可移动磁盘执行")
    p2.add_argument("--yes", "-y", action="store_true", help="跳过交互确认")
    p2.set_defaults(func=cmd_check)

    p3 = sub.add_parser("restore", help="按真实容量重新分区格式化")
    p3.add_argument("--disk", type=int, required=True, help="物理磁盘号（Get-Disk 的 Number）")
    p3.add_argument("--size-mb", type=int, help="分区大小（MB），取真实容量×0.98")
    p3.add_argument("--size-gb", type=float, help="分区大小（GiB），与 --size-mb 二选一")
    p3.add_argument("--letter", type=norm_letter, required=True, help="要分配的盘符")
    p3.add_argument("--label", default="USB", help="卷标，默认 USB")
    p3.add_argument("--fs", default="exfat", choices=["exfat", "ntfs", "fat32"], help="文件系统")
    p3.add_argument("--force", action="store_true", help="跳过 USB 总线/容量安全检查")
    p3.add_argument("--dry-run", action="store_true", help="只预览 diskpart 脚本，不执行")
    p3.add_argument("--yes", "-y", action="store_true", help="真正执行（否则只预览）")
    p3.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    if getattr(args, "func", None) is cmd_restore and args.size_gb and not args.size_mb:
        args.size_mb = int(args.size_gb * 1024)
    if getattr(args, "func", None) is cmd_restore and not args.size_mb:
        ap.error("restore 需要 --size-mb 或 --size-gb")

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。已写入的数据可用 --resume 继续校验。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
