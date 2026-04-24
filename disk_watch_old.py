#!/usr/bin/env python3
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import time

CHECK_PATH = "/"
LOW_SPACE_GIB = 350
INTERVAL_SEC = 60

run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_log = Path.home() / f"disk_free_log_{run_ts}.csv"
detail_log = Path.home() / f"disk_growth_detail_{run_ts}.log"
proc_log = Path.home() / f"process_snapshot_{run_ts}.log"

WATCH_PATHS = [
    #str(Path.home()),
    str(Path.home() / "Library"),
    str(Path.home() / "Library/Caches"),
    str(Path.home() / "Library/Containers"),
    str(Path.home() / "Library/Group Containers"),
    "/System/Volumes/Data/private/var",
    "/System/Volumes/Data/Library",
    #"/System/Volumes/Data/Users",
    "/System/Volumes/Data/Applications",
]

csv_log.write_text("timestamp,free_gib,used_gib,total_gib\n")

def gib(n: int) -> float:
    return n / 1024**3

def run_cmd(cmd, timeout=120):
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        return f"[command failed] {' '.join(cmd)}\n{proc.stderr.strip()}"
    except Exception as e:
        return f"[command exception] {' '.join(cmd)}\n{e}"

def run_du(path: str, now: str) -> str:
    print(f"[{now}] scanning: du -xhd 1 {path}", flush=True)
    started = time.time()
    try:
        proc = subprocess.run(
            ["du", "-xhd", "1", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.time() - started

        if proc.returncode == 0:
            lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]

            def size_kib(line: str) -> int:
                try:
                    return int(line.split()[0])
                except Exception:
                    return -1

            lines.sort(key=size_kib, reverse=True)
            top = "\n".join(lines[:20])
            print(f"[{now}] finished: {path} in {elapsed:.1f}s, {len(lines)} rows", flush=True)
            print(top, flush=True)
            return top

        print(f"[{now}] du failed: {path} in {elapsed:.1f}s: {proc.stderr.strip()}", flush=True)
        return f"[du failed for {path}] {proc.stderr.strip()}"

    except Exception as e:
        elapsed = time.time() - started
        print(f"[{now}] du exception: {path} in {elapsed:.1f}s: {e}", flush=True)
        return f"[du exception for {path}] {e}"

def top_processes(now: str) -> str:
    print(f"[{now}] collecting top process snapshot", flush=True)

    by_mem = run_cmd([
        "ps", "-axo",
        "pid,ppid,%cpu,%mem,rss,vsz,etime,state,comm",
        "-r"
    ], timeout=30)

    by_cpu = run_cmd([
        "ps", "-axo",
        "pid,ppid,%cpu,%mem,rss,vsz,etime,state,comm",
        "-r"
    ], timeout=30)

    # same base command; we sort in Python-like way by using ps output order as returned
    # on macOS, `ps -r` sorts by CPU. For memory, we use full output and keep a second command below.
    by_mem = run_cmd([
        "ps", "-axo",
        "pid,ppid,%cpu,%mem,rss,vsz,etime,state,comm"
    ], timeout=30)

    lines = [x for x in by_mem.splitlines() if x.strip()]
    header = lines[0] if lines else ""
    rows = lines[1:] if len(lines) > 1 else []

    def rss_kb(line: str) -> int:
        parts = line.split(None, 8)
        if len(parts) < 9:
            return -1
        try:
            return int(parts[4])
        except Exception:
            return -1

    mem_top = sorted(rows, key=rss_kb, reverse=True)[:20]
    cpu_top = by_cpu.splitlines()[:21] if by_cpu else []

    out = []
    out.append("=" * 80)
    out.append(f"{now} PROCESS SNAPSHOT")
    out.append("=" * 80)

    out.append("\nTop 20 by RSS (memory):")
    if header:
        out.append(header)
    out.extend(mem_top)

    out.append("\nTop 20 by CPU:")
    out.extend(cpu_top)

    return "\n".join(out)

print(f"CSV log:    {csv_log}", flush=True)
print(f"Detail log: {detail_log}", flush=True)
print(f"Proc log:   {proc_log}", flush=True)

while True:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total, used, free = shutil.disk_usage(CHECK_PATH)

    total_gib = gib(total)
    used_gib = gib(used)
    free_gib = gib(free)

    line = (
        f"[{now}] free: {free_gib:8.2f} GiB | "
        f"used: {used_gib:8.2f} GiB | "
        f"total: {total_gib:8.2f} GiB"
    )
    print(line, flush=True)

    with csv_log.open("a") as f:
        f.write(f"{now},{free_gib:.2f},{used_gib:.2f},{total_gib:.2f}\n")
        f.flush()

    proc_snapshot = top_processes(now)
    with proc_log.open("a") as f:
        f.write(proc_snapshot + "\n\n")
        f.flush()

    if free_gib < LOW_SPACE_GIB:
        print(f"[{now}] LOW SPACE trigger: collecting processes and directory sizes", flush=True)

        with proc_log.open("a") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{now} LOW SPACE EXTRA PROCESS SNAPSHOT\n")
            f.write("=" * 80 + "\n")
            f.write(run_cmd(["ps", "-axo", "pid,ppid,user,%cpu,%mem,rss,vsz,etime,state,command"], timeout=60))
            f.write("\n\n")
            f.flush()

        with detail_log.open("a") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{now} LOW SPACE TRIGGER: free={free_gib:.2f} GiB\n")
            f.write("=" * 80 + "\n")
            for path in WATCH_PATHS:
                f.write(f"\n--- {path} ---\n")
                f.write(run_du(path, now))
                f.write("\n")
            f.flush()

        print(f"[{now}] LOW SPACE scan complete", flush=True)

    time.sleep(INTERVAL_SEC)
