#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usb_quickprobe.py —— U 盘「映射型虚标」快速探测（Windows，需管理员）

为什么快
--------
顺序写满整盘（h2testw 式）是唯一能“证明”容量正常的办法，但耗时与容量成正比。
虚标控制器的作弊方式是地址映射：逻辑 LBA 超过真实容量后回绕到物理起点。
于是只要往盘上**散布少量互不相同的标记**，再回读：
  * 真盘：每个扇区读回来的就是自己写进去的那份标记。
  * 假盘：高位地址的写入会覆盖低位物理块，回读时某些扇区返回的是**别人**的标记。
一次随机抽样即可定性，代价只有几百个 4KiB 扇区（约 2MB 写入），几秒到几分钟。

发现互相覆盖后，用差值求最大公约数即可反推真实容量：
若扇区 a 读到的是扇区 b 的标记，则 a ≡ b (mod R)，即真实容量 R 整除 (a-b)；
多个碰撞的差值取 gcd 就得到 R。

风险与前提
----------
* 需要管理员权限（要打开 \\\\.\\PHYSICALDRIVEn 并强制卸载卷）。
* 会向盘上随机写入 N 个 4KiB 扇区（默认 512 个）。请确保盘内数据可丢弃；
  加 --backup 会先备份这些扇区，探测结束后尽量写回。
* 探测期间该盘会从资源管理器里临时消失，结束后自动恢复。

用法
----
  python usb_quickprobe.py --letter E --dry-run          # 只算计划，不写盘
  python usb_quickprobe.py --letter E                    # 交互确认后探测
  python usb_quickprobe.py --letter E --count 1024 --yes # 更密的抽样
  python usb_quickprobe.py --disk 3 --backup sectors.bin --yes
"""

import argparse
import hashlib
import json
import math
import os
import random
import struct
import sys
import time
from ctypes import wintypes
from datetime import datetime

SECTOR = 4096                 # 探测粒度，同时满足 512/4096 物理扇区对齐
SKIP_HEAD = 32 * 1024 * 1024  # 跳过前 32MiB（引导/文件系统元数据区）
MARK_MAGIC = b"WBQP\x01"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140

_k32 = __import__("ctypes").WinDLL("kernel32", use_last_error=True)
_ct = __import__("ctypes")

_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             _ct.c_void_p, wintypes.DWORD, wintypes.DWORD, _ct.c_void_p]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, _ct.c_void_p, wintypes.DWORD,
                                 _ct.c_void_p, wintypes.DWORD, _ct.POINTER(wintypes.DWORD),
                                 _ct.c_void_p]
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, _ct.c_longlong,
                                  _ct.POINTER(_ct.c_longlong), wintypes.DWORD]
_k32.SetFilePointerEx.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [wintypes.HANDLE, _ct.c_void_p, wintypes.DWORD,
                          _ct.POINTER(wintypes.DWORD), _ct.c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [wintypes.HANDLE, _ct.c_void_p, wintypes.DWORD,
                           _ct.POINTER(wintypes.DWORD), _ct.c_void_p]
_k32.WriteFile.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def fmt_bytes(n):
    n = float(n)
    for unit, div in (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


def is_admin():
    try:
        return bool(_ct.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class RawDisk:
    """裸磁盘读写封装，4KiB 对齐、无缓存。"""

    def __init__(self, disk_no=None, letter=None):
        self.disk_no = disk_no
        self.letter = letter
        self.handle = None
        self.vol_handles = []
        self.buf = _ct.create_string_buffer(SECTOR * 2)
        addr = _ct.addressof(self.buf)
        self.aligned = (addr + SECTOR - 1) // SECTOR * SECTOR

    # ---- 打开/关闭 ----
    def open(self, write=False):
        if self.letter and self.disk_no is None:
            self.disk_no = disk_number_of(self.letter)
        path = f"\\\\.\\PHYSICALDRIVE{self.disk_no}"
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        flags = FILE_FLAG_NO_BUFFERING | (FILE_FLAG_WRITE_THROUGH if write else 0)
        h = _k32.CreateFileW(path, access, FILE_SHARE_ALL, None, OPEN_EXISTING, flags, None)
        if h == wintypes.HANDLE(-1).value or not h:
            err = _ct.get_last_error()
            hint = "（需要管理员权限）" if err == 5 and write else ""
            raise OSError(err, f"无法打开 {path}{hint}")
        self.handle = h
        return self

    def lock_volumes(self):
        """锁定并卸载该磁盘上的卷，否则 Windows 会拒绝裸写。"""
        for letter in volumes_of(self.disk_no):
            h = _k32.CreateFileW(f"\\\\.\\{letter}:", GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None)
            if h == wintypes.HANDLE(-1).value or not h:
                continue
            n = wintypes.DWORD(0)
            if _k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, _ct.byref(n), None):
                _k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, _ct.byref(n), None)
            self.vol_handles.append(h)

    def unlock_volumes(self):
        for h in self.vol_handles:
            try:
                n = wintypes.DWORD(0)
                _k32.DeviceIoControl(h, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, _ct.byref(n), None)
                _k32.CloseHandle(h)
            except Exception:
                pass
        self.vol_handles = []
        if self.handle:
            try:
                n = wintypes.DWORD(0)
                _k32.DeviceIoControl(self.handle, IOCTL_DISK_UPDATE_PROPERTIES,
                                     None, 0, None, 0, _ct.byref(n), None)
            except Exception:
                pass

    def close(self):
        self.unlock_volumes()
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 几何信息 ----
    def geometry(self):
        out = _ct.create_string_buffer(256)
        n = wintypes.DWORD(0)
        if not _k32.DeviceIoControl(self.handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
                                    None, 0, out, 256, _ct.byref(n), None):
            raise OSError(_ct.get_last_error(), "IOCTL_DISK_GET_DRIVE_GEOMETRY_EX 失败")
        bytes_per_sector = struct.unpack_from("<I", out.raw, 20)[0]
        disk_size = struct.unpack_from("<q", out.raw, 24)[0]
        return bytes_per_sector, disk_size

    # ---- 读写 ----
    def _seek(self, offset):
        if not _k32.SetFilePointerEx(self.handle, offset, None, 0):
            raise OSError(_ct.get_last_error(), f"seek 到 {offset} 失败")

    def read_sector(self, lba):
        self._seek(lba * SECTOR)
        n = wintypes.DWORD(0)
        if not _k32.ReadFile(self.handle, _ct.c_void_p(self.aligned), SECTOR,
                             _ct.byref(n), None):
            raise OSError(_ct.get_last_error(), f"读 LBA {lba} 失败")
        return _ct.string_at(self.aligned, n.value)

    def write_sector(self, lba, data):
        assert len(data) == SECTOR
        _ct.memmove(self.aligned, data, SECTOR)
        self._seek(lba * SECTOR)
        n = wintypes.DWORD(0)
        if not _k32.WriteFile(self.handle, _ct.c_void_p(self.aligned), SECTOR,
                              _ct.byref(n), None):
            raise OSError(_ct.get_last_error(), f"写 LBA {lba} 失败")

    def flush(self):
        _k32.FlushFileBuffers(self.handle)


# ----------------------------------------------------------------------------
# 盘符 <-> 磁盘号
# ----------------------------------------------------------------------------
def _ps(cmd):
    import base64
    import subprocess
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" \
             "$ProgressPreference='SilentlyContinue'\n$ErrorActionPreference='SilentlyContinue'\n" + cmd
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "").strip()


def disk_number_of(letter):
    out = _ps(f"[string](Get-Partition -DriveLetter {letter}).DiskNumber")
    if not out.isdigit():
        raise RuntimeError(f"无法确定 {letter}: 所在的物理磁盘")
    return int(out)


def volumes_of(disk_no):
    out = _ps(f"@(Get-Partition -DiskNumber {disk_no} | "
              f"Where-Object {{$_.DriveLetter}} | ForEach-Object {{[string]$_.DriveLetter}}) "
              f"| ConvertTo-Json -Compress")
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [data] if isinstance(data, str) else [str(x) for x in data]


def disk_info(disk_no):
    out = _ps(f"$d=Get-Disk -Number {disk_no}; if($d){{"
              f"[pscustomobject]@{{model=[string]$d.FriendlyName;bus=[string]$d.BusType;"
              f"size=[double]$d.Size}} | ConvertTo-Json -Compress}}")
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {}


# ----------------------------------------------------------------------------
# 标记
# ----------------------------------------------------------------------------
def make_marker(lba, nonce):
    head = MARK_MAGIC + struct.pack("<Q", lba) + nonce
    body = head + b"\x00" * (SECTOR - len(head))
    return body


def parse_marker(data):
    if len(data) < 33 or data[:5] != MARK_MAGIC:
        return None
    return struct.unpack_from("<Q", data, 5)[0]


# ----------------------------------------------------------------------------
def plan(lba_total, count, seed):
    lo, hi = SKIP_HEAD // SECTOR, lba_total - 1
    if hi - lo < count + 8:
        raise RuntimeError("可探测扇区太少")
    rng = random.Random(seed)
    picked = set(rng.sample(range(lo, hi + 1), count))
    picked.add(hi)          # 一定覆盖最末尾
    picked.add(lo)          # 一定覆盖数据区起点
    return sorted(picked)


def cmd_probe(args):
    letter = args.letter
    disk_no = args.disk
    if disk_no is None:
        if not letter:
            print("[错误] 需要 --letter 或 --disk")
            return 2
        disk_no = disk_number_of(letter)

    info = disk_info(disk_no)
    print(f"\n=== 快速探测 Disk {disk_no} ===")
    print(f"  设备    : {info.get('model', '未知')}  ({info.get('bus', '?')})")
    print(f"  上报容量: {fmt_bytes(info.get('size', 0))}")

    if str(info.get("bus", "")).upper() != "USB" and not args.force:
        print("[拒绝] 不是 USB 设备。确认无误请加 --force")
        return 2

    # 只读打开取几何信息（无管理员权限时退化为用 PowerShell 的上报容量）
    bps, total = 0, int(info.get("size") or 0)
    try:
        with RawDisk(disk_no=disk_no) as rd:
            rd.open(write=False)
            bps, total = rd.geometry()
    except OSError as e:
        if not args.dry_run:
            print(f"\n[失败] 无法访问裸设备：{e}")
            print("       请右键“以管理员身份运行”终端后重试。")
            return 2
        print(f"  [提示] 无管理员权限，使用上报容量规划（{e.strerror if hasattr(e, 'strerror') else e}）")
    if not total:
        print("[失败] 取不到磁盘容量")
        return 2
    lba_total = total // SECTOR
    probes = plan(lba_total, args.count, args.seed)
    span = probes[-1] - probes[0]
    print(f"  扇区大小: {bps if bps else '未知'} B   总扇区: {lba_total:,} (按 {SECTOR} B 探测)")
    print(f"  抽样数量: {len(probes)} 个扇区（约 {fmt_bytes(len(probes) * SECTOR)} 写入）")
    print(f"  覆盖范围: LBA {probes[0]:,} ~ {probes[-1]:,}")
    print(f"  预计耗时: 数秒 ~ 数分钟（取决于盘速，无整盘写入）")

    if args.dry_run:
        print("\n  [dry-run] 未写盘。")
        return 0

    if not is_admin():
        print("\n[失败] 需要管理员权限。请右键“以管理员身份运行”终端后重试。")
        return 2

    if not args.yes:
        print("\n将向该盘随机写入上述扇区（会临时卸载该卷，盘符短暂消失）。")
        ans = input("继续？输入 yes 开始: ").strip().lower()
        if ans not in ("yes", "y"):
            print("已取消，未做任何写入。")
            return 1

    backup = {}
    t0 = time.time()
    with RawDisk(disk_no=disk_no) as rd:
        rd.open(write=True)
        rd.lock_volumes()

        if args.backup:
            for lba in probes:
                backup[lba] = rd.read_sector(lba)
            with open(args.backup, "wb") as f:
                for lba in probes:
                    f.write(struct.pack("<Q", lba) + backup[lba])
            print(f"  已备份 {len(backup)} 个扇区 → {args.backup}")

        nonces = {lba: os.urandom(16) for lba in probes}
        expected = {lba: make_marker(lba, nonces[lba]) for lba in probes}

        print("  写入标记 ...", end=" ", flush=True)
        for lba in probes:
            rd.write_sector(lba, expected[lba])
        rd.flush()
        print("完成")

        print("  回读校验 ...", end=" ", flush=True)
        got = {lba: rd.read_sector(lba) for lba in probes}
        print("完成")

        restored = False
        if args.backup and backup:
            print("  写回原始扇区 ...", end=" ", flush=True)
            try:
                for lba in probes:
                    rd.write_sector(lba, backup[lba])
                rd.flush()
                restored = True
                print("完成")
            except OSError as e:
                print(f"失败：{e}")

    # ---------- 分析 ----------
    matched = [lba for lba in probes if got[lba] == expected[lba]]
    conflicts = []       # (异常位置, 读到的是谁的标记或 None)
    for lba in probes:
        if got[lba] != expected[lba]:
            conflicts.append((lba, parse_marker(got[lba])))

    print(f"\n  {'=' * 60}")
    print(f"  抽样 {len(probes)} 个扇区，一致 {len(matched)} 个，异常 {len(conflicts)} 个")

    real_bytes = None
    upper_bytes = None
    verdict = "PASS"
    note = ""

    if not conflicts:
        print("  结论：抽样范围内未发现地址映射/虚标")
        print("  说明：抽样通过 ≠ 全盘可靠。要下定论仍需顺序写满全盘。")
    else:
        verdict = "FAKE"
        print("  结论：确认虚标（存在超出真实容量的地址）")

        mapping_pairs = [(a, b) for a, b in conflicts if b is not None]
        if matched:
            print(f"  最大可正常读写的 LBA: {max(matched):,}  "
                  f"({fmt_bytes((max(matched) + 1) * SECTOR)})")
        first_bad, _ = conflicts[0]
        upper_bytes = first_bad * SECTOR
        print(f"  首个异常 LBA        : {first_bad:,}  ({fmt_bytes(upper_bytes)})")
        print(f"  容量硬上界          : ≤ {fmt_bytes(upper_bytes)}")

        if mapping_pairs:
            for a, b in mapping_pairs[:6]:
                print(f"    LBA {a:,} 读到的是 LBA {b:,} 写入的标记")
            deltas = [abs(a - b) for a, b in mapping_pairs if a != b]
            g = 0
            for d in deltas:
                g = math.gcd(g, d)
            if g > 0:
                real_bytes = g * SECTOR
                note = "回绕型：由映射位置差的最大公约数反推"
                print(f"\n  真实容量 ≈ {fmt_bytes(real_bytes)}"
                      f"（{g:,} × {SECTOR}B）")
                print(f"  依据：映射位置差 gcd = {g:,} 扇区")
        else:
            # 丢写型：写入被直接丢弃，读回的不是任何标记
            note = "丢写型：按抽样命中比例估算"
            n_lo, n_hi = probes[0], probes[-1]
            p = len(matched) / len(probes)
            sd = math.sqrt(max(p * (1 - p), 1e-12) / len(probes))
            est = int(n_lo + p * (n_hi - n_lo))
            lo_est = int(n_lo + max(0.0, p - sd) * (n_hi - n_lo))
            hi_est = min(int(n_lo + min(1.0, p + sd) * (n_hi - n_lo)), first_bad - 1)
            real_bytes = est * SECTOR
            print("\n  该主控不是回绕型：超出容量的写入被直接丢弃，")
            print("  因此无法用位置差反推，只能按命中比例统计估算。")
            print(f"\n  命中比例：{len(matched)}/{len(probes)} = {p * 100:.2f}%")
            print(f"  真实容量点估计 ≈ {fmt_bytes(real_bytes)}"
                  f"（{est:,} 扇区，即 {real_bytes / 1e9:.1f} GB / {real_bytes / (1 << 30):.1f} GiB）")
            print(f"  一倍标准差区间 ≈ {fmt_bytes(lo_est * SECTOR)} ~ "
                  f"{fmt_bytes(hi_est * SECTOR)}")

    print(f"  耗时：{time.time() - t0:.1f}s")
    print(f"  {'=' * 60}")

    report = {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "物理磁盘": disk_no,
        "设备": info.get("model"),
        "上报容量": info.get("size"),
        "抽样扇区数": len(probes),
        "一致扇区数": len(matched),
        "异常扇区数": len(conflicts),
        "最大成功LBA": max(matched) if matched else None,
        "首个异常LBA": conflicts[0][0] if conflicts else None,
        "异常LBA列表": [a for a, _ in conflicts[:200]],
        "首处映射": conflicts[:20],
        "结论": verdict,
        "估算方法": note,
        "推算真实容量": real_bytes,
        "容量硬上界": upper_bytes,
        "原始扇区已写回": restored if args.backup else None,
    }
    name = f"usb_quickprobe_{letter or disk_no}.json"
    with open(name, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  报告已保存: {os.path.abspath(name)}")

    if verdict != "PASS" and disk_no:
        print(f"\n  要拿到精确边界（约 30 次探测、10 秒）：")
        print(f"    python {os.path.basename(__file__)} --letter {letter or 'E'} --locate --yes")
    return 0 if verdict == "PASS" else 3


def cmd_locate(args):
    """二分查找真实容量边界：探测“在 LBA x 写入能否原样读回”。"""
    disk_no = args.disk or disk_number_of(args.letter)
    info = disk_info(disk_no)
    print(f"\n=== 定位真实容量边界 Disk {disk_no} ===")
    print(f"  设备    : {info.get('model', '未知')}  ({info.get('bus', '?')})")
    print(f"  上报容量: {fmt_bytes(info.get('size', 0))}")

    if str(info.get("bus", "")).upper() != "USB" and not args.force:
        print("[拒绝] 不是 USB 设备。确认无误请加 --force")
        return 2
    if not is_admin():
        print("\n[失败] 需要管理员权限（要裸写 \\\\.\\PHYSICALDRIVEn）。")
        return 2
    if not args.yes:
        print("\n将对该盘做约 30 次单扇区写入（会临时卸载卷）。二分法依赖“超容量写入被丢弃”，")
        print("若该盘是回绕型，会提示改用抽样模式。")
        if input("继续？输入 yes 开始: ").strip().lower() not in ("yes", "y"):
            print("已取消。")
            return 1

    t0 = time.time()
    with RawDisk(disk_no=disk_no) as rd:
        rd.open(write=True)
        rd.lock_volumes()
        bps, total = rd.geometry()
        lo_lba, hi_lba = SKIP_HEAD // SECTOR, total // SECTOR - 1

        def test(lba):
            m = make_marker(lba, os.urandom(16))
            rd.write_sector(lba, m)
            rd.flush()
            return rd.read_sector(lba) == m

        print(f"  起始 {lo_lba:,} 测试 ...", end=" ", flush=True)
        if not test(lo_lba):
            print("失败 —— 盘在最开头就无法读写，可能已损坏")
            return 2
        print("可读写")
        print(f"  末端 {hi_lba:,} 测试 ...", end=" ", flush=True)
        if test(hi_lba):
            print("也可正常读写")
            print("  [中止] 该盘不是丢写型（可能是回绕型或确实足量），"
                  "请用抽样模式：--letter X --count 1024 --yes")
            return 3
        print("异常（符合虚标特征）")

        ok, bad, steps = lo_lba, hi_lba, 0
        while bad - ok > 1:
            mid = (ok + bad) // 2
            if test(mid):
                ok = mid
            else:
                bad = mid
            steps += 1
            sys.stdout.write(f"\r  二分 {steps:2d} 次: 好={ok:,} 坏={bad:,}   ")
            sys.stdout.flush()
        print()

        real_bytes = bad * SECTOR
        print(f"\n  {'=' * 60}")
        print(f"  最后可正常读写的扇区: LBA {ok:,}")
        print(f"  第一个失效的扇区    : LBA {bad:,}")
        print(f"  真实容量            : {real_bytes:,} B")
        print(f"                      = {real_bytes / 1e9:.2f} GB (十进制)")
        print(f"                      = {real_bytes / (1 << 30):.2f} GiB")
        print(f"  约为标称容量的      : {real_bytes / info.get('size', 1) * 100:.2f}%")
        print(f"  耗时                : {time.time() - t0:.1f}s")
        print(f"  {'=' * 60}")

    mb = int(real_bytes * 0.98 // (1 << 20))
    report = {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "物理磁盘": disk_no,
        "设备": info.get("model"),
        "上报容量": info.get("size"),
        "最大有效LBA": ok,
        "首个失效LBA": bad,
        "真实容量字节": real_bytes,
        "真实容量GiB": round(real_bytes / (1 << 30), 2),
        "建议分区MB": mb,
    }
    name = f"usb_locate_{args.letter or disk_no}.json"
    with open(name, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  报告已保存: {os.path.abspath(name)}")
    print(f"\n  按真实容量重分区：")
    print(f"    python usb_fake_capacity.py restore --disk {disk_no} "
          f"--size-mb {mb} --letter {args.letter or 'X'} --dry-run")
    return 0


def main():
    ap = argparse.ArgumentParser(description="U 盘映射型虚标快速探测（Windows，需管理员）")
    ap.add_argument("--letter", help="盘符，如 E")
    ap.add_argument("--disk", type=int, help="物理磁盘号（与 --letter 二选一）")
    ap.add_argument("--count", type=int, default=512, help="抽样扇区数，默认 512")
    ap.add_argument("--seed", type=int, default=None, help="随机种子，便于复现")
    ap.add_argument("--backup", help="探测前备份被写扇区到该文件，结束后写回")
    ap.add_argument("--locate", action="store_true",
                    help="二分查找真实容量精确边界（丢写型虚标专用，约 30 次探测）")
    ap.add_argument("--dry-run", action="store_true", help="只输出计划，不写盘")
    ap.add_argument("--force", action="store_true", help="允许非 USB 设备")
    ap.add_argument("--yes", "-y", action="store_true", help="跳过交互确认")
    args = ap.parse_args()
    if args.seed is None:
        args.seed = random.SystemRandom().randrange(1 << 31)
    try:
        if args.locate:
            return cmd_locate(args)
        return cmd_probe(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
