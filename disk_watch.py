#!/usr/bin/env python3
from collections import defaultdict
import csv
import errno
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

INTERVAL = 30
LOW_GB_THRESHOLD = 200
LOW_REPEAT_MINUTES = 10
CRITICAL_FREE_GB = 20
EMERGENCY_DROP_GB = 20
MIN_FREE_GB_FOR_HEAVY_LOGS = 10
RECOVERY_FREE_GB_FOR_DEFERRED_LOGS = 40

TARGET_USER_ENV_VAR = "DISK_WATCH_USER"


def resolve_target_user() -> str:
    configured_user = os.environ.get(TARGET_USER_ENV_VAR)
    if configured_user:
        return configured_user

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user

    return getpass.getuser()


USER = resolve_target_user()
USER_HOME = Path(f"/Users/{USER}")
USER_VAR_FOLDERS = Path(tempfile.gettempdir()).resolve().parent
ONEDRIVE_GROUP_CONTAINER = USER_HOME / "Library" / "Group Containers" / "UBF8T346G9.OneDriveSyncClientSuite"
ONEDRIVE_CLOUD_STORAGE = USER_HOME / "Library" / "CloudStorage"
GOOGLE_DRIVE_GROUP_CONTAINER = USER_HOME / "Library" / "Group Containers" / "EQHXZ8M8AV.group.com.google.drivefs"
GOOGLE_DRIVE_APP_SUPPORT = USER_HOME / "Library" / "Application Support" / "Google" / "DriveFS"
GOOGLE_DRIVE_CLOUD_STORAGE_PATHS = sorted(
    path for path in ONEDRIVE_CLOUD_STORAGE.glob("GoogleDrive-*") if path.is_dir()
)
WATCH_PATHS = [
    ("user_library_caches", str(USER_HOME / "Library" / "Caches")),
    ("user_library_app_support", str(USER_HOME / "Library" / "Application Support")),
    ("user_library_containers", str(USER_HOME / "Library" / "Containers")),
    ("user_library_group_containers", str(USER_HOME / "Library" / "Group Containers")),
    ("user_library_metadata", str(USER_HOME / "Library" / "Metadata")),
    ("onedrive_group_container", str(ONEDRIVE_GROUP_CONTAINER)),
    ("onedrive_personal_state", str(ONEDRIVE_GROUP_CONTAINER / "OneDrive.noindex")),
    ("onedrive_business_state", str(ONEDRIVE_GROUP_CONTAINER / "OneDrive - Nitor Group.noindex")),
    ("onedrive_nitor_state", str(ONEDRIVE_GROUP_CONTAINER / "Nitor Group.noindex")),
    ("onedrive_file_provider_storage", str(ONEDRIVE_GROUP_CONTAINER / "File Provider Storage")),
    ("onedrive_file_provider_logs", str(ONEDRIVE_GROUP_CONTAINER / "FileProviderLogs")),
    ("onedrive_personal_cloudstorage", str(ONEDRIVE_CLOUD_STORAGE / "OneDrive-Personal")),
    ("onedrive_nitor_cloudstorage", str(ONEDRIVE_CLOUD_STORAGE / "OneDrive-NitorGroup")),
    ("onedrive_sharedlibraries_cloudstorage", str(ONEDRIVE_CLOUD_STORAGE / "OneDrive-SharedLibraries-NitorGroup")),
    ("google_drive_group_container", str(GOOGLE_DRIVE_GROUP_CONTAINER)),
    ("google_drive_file_provider_storage", str(GOOGLE_DRIVE_GROUP_CONTAINER / "File Provider Storage")),
    ("google_drive_group_library", str(GOOGLE_DRIVE_GROUP_CONTAINER / "Library")),
    ("google_drive_fp_state", str(GOOGLE_DRIVE_GROUP_CONTAINER / "fp")),
    ("google_drive_app_support", str(GOOGLE_DRIVE_APP_SUPPORT)),
    ("google_drive_logs", str(GOOGLE_DRIVE_APP_SUPPORT / "Logs")),
    ("google_drive_cef_cache", str(GOOGLE_DRIVE_APP_SUPPORT / "cef_cache")),
    *[
        (f"google_drive_cloudstorage_{index}", str(path))
        for index, path in enumerate(GOOGLE_DRIVE_CLOUD_STORAGE_PATHS, start=1)
    ],
    ("user_var_folders", str(USER_VAR_FOLDERS)),
    ("user_launchservices_cache", str(USER_VAR_FOLDERS / "0" / "com.apple.LaunchServices.dv")),
    ("spotlight_store", "/System/Volumes/Data/.Spotlight-V100"),
    ("swap_and_sleep", "/private/var/vm"),
]

TOP_PROCESSES = 30
FS_USAGE_MAX_SAMPLE_LINES = 400
FS_USAGE_MAX_SAMPLE_LINES_PER_PROCESS = 80
FS_USAGE_SUMMARY_LIMIT = 20
UNIFIED_LOG_WINDOW_MINUTES = 15
UNIFIED_LOG_PREDICATE = (
    '(process == "corespotlightd" OR '
    'process == "mds_stores" OR '
    'process == "mds" OR '
    'process == "fileproviderd" OR '
    'eventMessage CONTAINS[c] "FileProvider" OR '
    'eventMessage CONTAINS[c] "spotlightindex" OR '
    'eventMessage CONTAINS[c] "repair_lookupPath" OR '
    'eventMessage CONTAINS[c] "forceToOrphanParent")'
)

HOST = socket.gethostname()
START_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_DIR = Path.cwd() / "logs" / f"disk_watch_{START_STAMP}"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAIN_LOG = LOG_DIR / "main.log"
LOWSPACE_LOG = LOG_DIR / "lowspace.log"
FS_DISKIO_LOG = LOG_DIR / "fs_usage_diskio.log"
FS_FILESYS_LOG = LOG_DIR / "fs_usage_filesys.log"
PROC_LOG = LOG_DIR / "process_snapshot.log"
LSOF_LOG = LOG_DIR / "lsof_deleted_open.log"
UNIFIED_LOG = LOG_DIR / "unified_log_spotlight.log"
FILE_PROVIDER_PLUGINS_LOG = LOG_DIR / "file_provider_plugins.log"
FILE_PROVIDER_DUMP_LOG = LOG_DIR / "file_provider_dump.log"
FILE_PROVIDER_SUMMARY_LOG = LOG_DIR / "file_provider_summary.log"
DISK_CSV = LOG_DIR / "disk_space.csv"

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

last_low_capture = 0.0
last_low_capture_free = None
reported_log_write_failures = set()
current_incident_started_at = None
current_incident_lowest_free = None
current_incident_needs_deferred_capture = False
pending_deferred_captures = []

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_timestamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def line(char="-", width=100):
    return char * width

def human_bytes(n):
    if n is None:
        return "n/a"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    x = float(n)
    for unit in units:
        if x < 1024 or unit == units[-1]:
            return f"{x:,.2f} {unit}"
        x /= 1024.0

def report_log_write_failure(path, exc):
    key = (str(path), exc.errno)
    if key in reported_log_write_failures:
        return

    reported_log_write_failures.add(key)
    print(
        f"[{now()}] log write skipped for {path}: {exc.strerror}",
        file=sys.stderr,
        flush=True,
    )

def append(path, text):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            report_log_write_failure(path, exc)
            return False
        raise

def init_disk_csv():
    try:
        with open(DISK_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "host",
                "disk_total_bytes",
                "disk_used_bytes",
                "disk_free_bytes",
                "low_space",
            ])
        return True
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            report_log_write_failure(DISK_CSV, exc)
            return False
        raise

def write_interval_csv(timestamp, total, used, free, low):
    try:
        with open(DISK_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, HOST, total, used, free, int(low)])
        return True
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            report_log_write_failure(DISK_CSV, exc)
            return False
        raise

def run_cmd(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return 1, "", f"ERROR running {' '.join(cmd)}: {e}"

def disk_free():
    usage = shutil.disk_usage("/")
    return usage.total, usage.used, usage.free

def parse_du_size(out):
    for line_text in reversed(out.splitlines()):
        parts = line_text.split()
        if not parts:
            continue
        try:
            return int(parts[0]) * 1024
        except ValueError:
            continue
    return None

def du_one(path):
    append(MAIN_LOG, f"[{now()}] running: du -skx {path}\n")
    rc, out, err = run_cmd(["/usr/bin/du", "-skx", path], timeout=300)
    size = parse_du_size(out)
    if size is not None:
        if rc != 0 and err.strip():
            append(MAIN_LOG, f"[{now()}] du partial for {path}: rc={rc} err={err.strip()}\n")
        return size
    append(MAIN_LOG, f"[{now()}] du failed for {path}: rc={rc} err={err.strip()}\n")
    return None

def watch_paths_snapshot():
    results = []
    for label, path in WATCH_PATHS:
        if os.path.exists(path):
            size = du_one(path)
            results.append((label, path, size))
        else:
            append(MAIN_LOG, f"[{now()}] skip missing path: {path}\n")
            results.append((label, path, None))
    return results

def print_status(total, used, free, low=False, watched=None):
    print(line("="))
    print(f"[{now()}] host={HOST}")
    print(f"Target user: {USER}")
    print(line("-"))
    print(f"Disk total : {human_bytes(total)}")
    print(f"Disk used  : {human_bytes(used)}")
    print(f"Disk free  : {human_bytes(free)}")
    print(f"Threshold  : {LOW_GB_THRESHOLD} GB")
    print(f"State      : {'LOW SPACE' if low else 'OK'}")
    if watched is not None:
        print(line("-"))
        print("Tracked paths:")
        for label, path, size in watched:
            print(f"  {label:<26} {human_bytes(size):>12}  {path}")
    print(line("-"))
    print(f"Logs dir   : {LOG_DIR}")
    print(line("="), flush=True)

def write_summary(logfile, total, used, free, label, watched=None):
    block = []
    block.append("\n" + line("=") + "\n")
    block.append(f"[{now()}] {label} host={HOST}\n")
    block.append(f"disk_total={human_bytes(total)} disk_used={human_bytes(used)} disk_free={human_bytes(free)}\n")
    if watched is not None:
        for watch_label, path, size in watched:
            block.append(f"watch_path label={watch_label} path={path} size={human_bytes(size)}\n")
    block.append(line("=") + "\n")
    append(logfile, "".join(block))

def normalize_fs_usage_process(process_token):
    name, dot, suffix = process_token.rpartition(".")
    if dot and suffix.isdigit():
        return name
    return process_token

def parse_fs_usage_line(line_text):
    parts = line_text.split()
    if len(parts) < 2:
        return None

    size_bytes = 0
    for part in parts:
        if part.startswith("B=0x"):
            try:
                size_bytes = int(part[4:], 16)
            except ValueError:
                size_bytes = 0
            break

    return {
        "operation": parts[1],
        "process": normalize_fs_usage_process(parts[-1]),
        "size_bytes": size_bytes,
    }

def decode_subprocess_output(output):
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output or ""

def fs_usage_sample(kind, seconds, outfile):
    append(outfile, f"\n[{now()}] START fs_usage kind={kind} duration={seconds}s\n")
    try:
        captured = ""
        with open(outfile, "a", encoding="utf-8") as f:
            p = subprocess.Popen(
                ["/usr/bin/fs_usage", "-w", "-f", kind],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            time.sleep(seconds)
            p.terminate()
            try:
                captured, _ = p.communicate(timeout=5)
                captured = decode_subprocess_output(captured)
            except subprocess.TimeoutExpired as exc:
                # Under severe disk pressure fs_usage may not exit promptly;
                # keep any partial output instead of dropping the whole sample.
                captured = decode_subprocess_output(exc.output)
                p.kill()
                try:
                    tail_output, _ = p.communicate(timeout=5)
                    if tail_output:
                        captured += decode_subprocess_output(tail_output)
                except subprocess.TimeoutExpired as kill_exc:
                    captured += decode_subprocess_output(kill_exc.output)
                    append(
                        outfile,
                        f"[{now()}] fs_usage kill timeout kind={kind}; using partial output\n",
                    )

            process_stats = defaultdict(lambda: {
                "lines": 0,
                "bytes": 0,
                "kept": 0,
                "suppressed": 0,
                "ops": defaultdict(int),
            })
            sample_lines = []
            total_lines = 0

            for raw_line in captured.splitlines():
                parsed = parse_fs_usage_line(raw_line)
                if parsed is None:
                    continue

                total_lines += 1
                stats = process_stats[parsed["process"]]
                stats["lines"] += 1
                stats["bytes"] += parsed["size_bytes"]
                stats["ops"][parsed["operation"]] += 1

                if (
                    len(sample_lines) < FS_USAGE_MAX_SAMPLE_LINES
                    and stats["kept"] < FS_USAGE_MAX_SAMPLE_LINES_PER_PROCESS
                ):
                    sample_lines.append(raw_line)
                    stats["kept"] += 1
                else:
                    stats["suppressed"] += 1

            f.write(f"[{now()}] parsed_lines={total_lines} unique_processes={len(process_stats)}\n")

            if process_stats:
                f.write("Summary by process (sorted by total bytes):\n")
                ranked_processes = sorted(
                    process_stats.items(),
                    key=lambda item: (item[1]["bytes"], item[1]["lines"]),
                    reverse=True,
                )
                for process_name, stats in ranked_processes[:FS_USAGE_SUMMARY_LIMIT]:
                    ops_summary = ", ".join(
                        f"{op}={count}"
                        for op, count in sorted(
                            stats["ops"].items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )
                    )
                    f.write(
                        "  "
                        f"process={process_name} "
                        f"lines={stats['lines']} "
                        f"bytes={human_bytes(stats['bytes'])} "
                        f"suppressed={stats['suppressed']} "
                        f"ops=[{ops_summary}]\n"
                    )

            suppressed_total = sum(stats["suppressed"] for stats in process_stats.values())
            if sample_lines:
                f.write("Sampled raw lines:\n")
                for raw_line in sample_lines:
                    f.write(raw_line + "\n")
            if suppressed_total:
                f.write(
                    f"[{now()}] sampled_lines={len(sample_lines)} suppressed_lines={suppressed_total} "
                    f"sample_limit={FS_USAGE_MAX_SAMPLE_LINES} "
                    f"per_process_limit={FS_USAGE_MAX_SAMPLE_LINES_PER_PROCESS}\n"
                )
        append(outfile, f"[{now()}] END fs_usage kind={kind}\n")
    except Exception as e:
        append(outfile, f"[{now()}] ERROR fs_usage kind={kind}: {e}\n")

def top_processes_snapshot():
    append(PROC_LOG, f"\n[{now()}] process snapshot\n")
    rc, out, err = run_cmd(
        ["/bin/ps", "-axo", "pid,ppid,user,%cpu,%mem,state,etime,command"],
        timeout=120
    )
    if rc == 0:
        lines = out.splitlines()
        kept = lines[: TOP_PROCESSES + 1]
        append(PROC_LOG, "\n".join(kept) + "\n")
    else:
        append(PROC_LOG, f"FAILED rc={rc} err={err.strip()}\n")

def strip_ansi(text):
    return ANSI_ESCAPE_RE.sub("", text)

def clean_file_provider_text(text):
    return " ".join(strip_ansi(text).strip().split())

def file_provider_detail(line_text):
    if ":" not in line_text:
        return None
    return clean_file_provider_text(line_text.split(":", 1)[1])

def summarize_file_provider_dump(output):
    providers = []
    current_provider = None
    current_domain = None
    lines = output.splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        line_text = clean_file_provider_text(raw_line)

        if (
            raw_line.startswith("=")
            and index + 2 < len(lines)
            and lines[index + 2].startswith("=")
        ):
            if current_domain is not None and current_provider is not None:
                current_provider["domains"].append(current_domain)
                current_domain = None
            if current_provider is not None:
                providers.append(current_provider)
            current_provider = {
                "provider_id": lines[index + 1].strip(),
                "display_name": None,
                "domains": [],
            }
            index += 3
            continue

        if current_provider is None:
            index += 1
            continue

        if line_text.startswith("+ display name:"):
            current_provider["display_name"] = file_provider_detail(line_text)
        elif line_text.startswith("domain:"):
            if current_domain is not None:
                current_provider["domains"].append(current_domain)
            current_domain = {
                "name": file_provider_detail(line_text),
                "status": "connected",
                "enabled": None,
                "indexing": None,
                "needs_auth": None,
                "needs_indexing": None,
                "pending_indexable": None,
                "total_indexable": None,
                "extension_error": None,
                "keep_downloaded": 0,
                "lazy": 0,
            }
        elif current_domain is not None:
            if "temporarily disconnected:" in line_text:
                current_domain["status"] = clean_file_provider_text(
                    line_text.split("temporarily disconnected:", 1)[1].rstrip(")")
                )
            elif line_text.startswith("enabled:"):
                current_domain["enabled"] = file_provider_detail(line_text)
            elif line_text.startswith("indexing:"):
                current_domain["indexing"] = file_provider_detail(line_text)
            elif line_text.startswith("needs-auth:"):
                current_domain["needs_auth"] = file_provider_detail(line_text)
            elif line_text.startswith("needs-indexing:"):
                current_domain["needs_indexing"] = file_provider_detail(line_text)
            elif line_text.startswith("pending-indexable-count:"):
                current_domain["pending_indexable"] = file_provider_detail(line_text)
            elif line_text.startswith("total-indexable-count:"):
                current_domain["total_indexable"] = file_provider_detail(line_text)
            elif line_text.startswith("can't dump the extension:"):
                current_domain["extension_error"] = file_provider_detail(line_text)

            if "cp:keepDownloaded" in raw_line:
                current_domain["keep_downloaded"] += 1
            if "cp:lazy" in raw_line:
                current_domain["lazy"] += 1

        index += 1

    if current_domain is not None and current_provider is not None:
        current_provider["domains"].append(current_domain)
    if current_provider is not None:
        providers.append(current_provider)

    if not providers:
        return "[no File Provider domains summarized]\n"

    summary_lines = []
    for provider in providers:
        display_name = provider["display_name"] or provider["provider_id"]
        summary_lines.append(
            f"provider={provider['provider_id']} display_name={display_name} domains={len(provider['domains'])}"
        )
        if not provider["domains"]:
            summary_lines.append("  [no domains found]")
            continue

        for domain in provider["domains"]:
            summary_lines.append(f"  domain={domain['name']}")
            summary_lines.append(f"    status={domain['status']}")
            summary_lines.append(
                "    "
                f"indexer_enabled={domain['enabled'] or 'n/a'} "
                f"indexing={domain['indexing'] or 'n/a'} "
                f"needs_auth={domain['needs_auth'] or 'n/a'} "
                f"needs_indexing={domain['needs_indexing'] or 'n/a'}"
            )
            summary_lines.append(
                "    "
                f"total_indexable={domain['total_indexable'] or 'n/a'} "
                f"pending_indexable={domain['pending_indexable'] or 'n/a'}"
            )
            summary_lines.append(
                "    "
                f"keep_downloaded_items={domain['keep_downloaded']} "
                f"lazy_items={domain['lazy']}"
            )
            if domain["extension_error"]:
                summary_lines.append(f"    extension_error={domain['extension_error']}")

    return "\n".join(summary_lines) + "\n"

def file_provider_plugins_snapshot():
    append(FILE_PROVIDER_PLUGINS_LOG, f"\n[{now()}] running: pluginkit -m -A -D\n")
    rc, out, err = run_cmd(["/usr/bin/pluginkit", "-m", "-A", "-D"], timeout=120)
    if rc != 0:
        append(FILE_PROVIDER_PLUGINS_LOG, f"FAILED rc={rc} err={err.strip()}\n")
        return

    relevant_lines = []
    for line_text in out.splitlines():
        lowered = line_text.lower()
        if "fileprovider" in lowered or "fpext" in lowered:
            relevant_lines.append(line_text)

    if relevant_lines:
        append(FILE_PROVIDER_PLUGINS_LOG, "\n".join(relevant_lines) + "\n")
    else:
        append(FILE_PROVIDER_PLUGINS_LOG, "[no File Provider related plugins found]\n")

def file_provider_dump_snapshot():
    append(FILE_PROVIDER_DUMP_LOG, f"\n[{now()}] running: fileproviderctl dump -l\n")
    append(FILE_PROVIDER_SUMMARY_LOG, f"\n[{now()}] running: fileproviderctl dump -l\n")
    rc, out, err = run_cmd(["/usr/bin/fileproviderctl", "dump", "-l"], timeout=180)
    if rc == 0:
        append(FILE_PROVIDER_DUMP_LOG, out if out else "[no File Provider dump output]\n")
        append(FILE_PROVIDER_SUMMARY_LOG, summarize_file_provider_dump(out))
    else:
        append(FILE_PROVIDER_DUMP_LOG, f"FAILED rc={rc} err={err.strip()}\n")
        append(FILE_PROVIDER_SUMMARY_LOG, f"FAILED rc={rc} err={err.strip()}\n")

def lsof_deleted_open():
    append(LSOF_LOG, f"\n[{now()}] running: lsof +L1\n")
    rc, out, err = run_cmd(["/usr/sbin/lsof", "-nP", "+L1"], timeout=120)
    if rc == 0:
        append(LSOF_LOG, out if out else "[no deleted-open files found]\n")
    else:
        append(LSOF_LOG, f"FAILED rc={rc} err={err.strip()}\n")

def unified_log_snapshot(window_minutes=UNIFIED_LOG_WINDOW_MINUTES):
    append(
        UNIFIED_LOG,
        f"\n[{now()}] running: log show --last {window_minutes}m --style compact --predicate {UNIFIED_LOG_PREDICATE}\n",
    )
    rc, out, err = run_cmd(
        [
            "/usr/bin/log",
            "show",
            "--last",
            f"{window_minutes}m",
            "--style",
            "compact",
            "--predicate",
            UNIFIED_LOG_PREDICATE,
        ],
        timeout=180,
    )
    if rc == 0:
        append(UNIFIED_LOG, out if out else "[no matching unified log entries found]\n")
    else:
        append(UNIFIED_LOG, f"FAILED rc={rc} err={err.strip()}\n")

def unified_log_snapshot_range(start_time, end_time):
    start_text = format_timestamp(start_time)
    end_text = format_timestamp(end_time)
    append(
        UNIFIED_LOG,
        f"\n[{now()}] running: log show --start {start_text} --end {end_text} --style compact --predicate {UNIFIED_LOG_PREDICATE}\n",
    )
    rc, out, err = run_cmd(
        [
            "/usr/bin/log",
            "show",
            "--start",
            start_text,
            "--end",
            end_text,
            "--style",
            "compact",
            "--predicate",
            UNIFIED_LOG_PREDICATE,
        ],
        timeout=180,
    )
    if rc == 0:
        append(UNIFIED_LOG, out if out else "[no matching unified log entries found]\n")
    else:
        append(UNIFIED_LOG, f"FAILED rc={rc} err={err.strip()}\n")

def deferred_heavy_capture(capture):
    start_time = capture["start_time"]
    recovered_time = capture["recovered_time"]
    lowest_free = capture["lowest_free"]
    append(
        MAIN_LOG,
        f"[{now()}] deferred heavy capture started for incident start={format_timestamp(start_time)} "
        f"recovered={format_timestamp(recovered_time)} lowest_free={human_bytes(lowest_free)}\n",
    )
    file_provider_dump_snapshot()
    unified_log_snapshot_range(start_time, recovered_time)
    append(MAIN_LOG, f"[{now()}] deferred heavy capture finished\n")

def low_space_capture(force_reason=None):
    global current_incident_needs_deferred_capture, last_low_capture, last_low_capture_free
    now_ts = time.time()
    in_cooldown = now_ts - last_low_capture < LOW_REPEAT_MINUTES * 60
    if in_cooldown and force_reason is None:
        append(MAIN_LOG, f"[{now()}] low-space capture skipped due to cooldown\n")
        return

    last_low_capture = now_ts
    total, used, free = disk_free()
    last_low_capture_free = free
    minimal_capture = free <= MIN_FREE_GB_FOR_HEAVY_LOGS * 1024**3

    if force_reason is None:
        append(MAIN_LOG, f"[{now()}] LOW SPACE capture started\n")
    else:
        append(MAIN_LOG, f"[{now()}] LOW SPACE capture started (override: {force_reason})\n")

    if minimal_capture:
        current_incident_needs_deferred_capture = True
        notice = (
            f"[{now()}] LOW SPACE capture using minimal logging at free={human_bytes(free)}; "
            "deferring large log artifacts until recovery\n"
        )
        append(MAIN_LOG, notice)
        print(notice.strip(), flush=True)

    fs_usage_sample("diskio", 10, FS_DISKIO_LOG)
    top_processes_snapshot()
    file_provider_plugins_snapshot()
    if minimal_capture:
        append(
            MAIN_LOG,
            f"[{now()}] skipped filesys, file_provider_dump, lsof, and unified_log due to low free space\n",
        )
    else:
        fs_usage_sample("filesys", 10, FS_FILESYS_LOG)
        file_provider_dump_snapshot()
        lsof_deleted_open()
        unified_log_snapshot()

    total, used, free = disk_free()
    watched = watch_paths_snapshot()

    write_summary(LOWSPACE_LOG, total, used, free, "LOW_SPACE_CAPTURE", watched=watched)
    append(MAIN_LOG, f"[{now()}] LOW SPACE capture finished\n")

def main():
    global current_incident_lowest_free, current_incident_needs_deferred_capture, current_incident_started_at
    critical_free_bytes = CRITICAL_FREE_GB * 1024**3
    emergency_drop_bytes = EMERGENCY_DROP_GB * 1024**3
    deferred_capture_free_bytes = RECOVERY_FREE_GB_FOR_DEFERRED_LOGS * 1024**3

    print(f"Logging to: {LOG_DIR}", flush=True)
    init_disk_csv()
    append(MAIN_LOG, f"[{now()}] started disk watcher on host={HOST} target_user={USER}\n")

    while True:
        cycle_start = time.time()

        total, used, free = disk_free()
        low = free < LOW_GB_THRESHOLD * 1024**3
        timestamp = now()

        write_summary(MAIN_LOG, total, used, free, "STATUS")
        write_interval_csv(timestamp, total, used, free, low)
        print_status(total, used, free, low=low)

        if low:
            if current_incident_started_at is None:
                current_incident_started_at = datetime.now()
                current_incident_lowest_free = free
                current_incident_needs_deferred_capture = False
                append(
                    MAIN_LOG,
                    f"[{now()}] incident started at free={human_bytes(free)}\n",
                )
            else:
                current_incident_lowest_free = min(current_incident_lowest_free, free)

            force_reasons = []
            if free <= critical_free_bytes:
                force_reasons.append(f"free space critical at {human_bytes(free)}")
            if last_low_capture_free is not None:
                drop_since_last_capture = last_low_capture_free - free
                if drop_since_last_capture >= emergency_drop_bytes:
                    force_reasons.append(
                        f"free space dropped by {human_bytes(drop_since_last_capture)} since last capture"
                    )

            force_reason = "; ".join(force_reasons) if force_reasons else None
            low_space_capture(force_reason=force_reason)
        else:
            if current_incident_started_at is not None:
                recovered_time = datetime.now()
                append(
                    MAIN_LOG,
                    f"[{now()}] incident recovered at free={human_bytes(free)}\n",
                )
                if current_incident_needs_deferred_capture:
                    pending_deferred_captures.append(
                        {
                            "start_time": current_incident_started_at,
                            "recovered_time": recovered_time,
                            "lowest_free": current_incident_lowest_free,
                        }
                    )
                    append(
                        MAIN_LOG,
                        f"[{now()}] queued deferred heavy capture for incident start={format_timestamp(current_incident_started_at)} "
                        f"recovered={format_timestamp(recovered_time)} lowest_free={human_bytes(current_incident_lowest_free)}\n",
                    )

                current_incident_started_at = None
                current_incident_lowest_free = None
                current_incident_needs_deferred_capture = False

            if pending_deferred_captures and free >= deferred_capture_free_bytes:
                append(
                    MAIN_LOG,
                    f"[{now()}] running {len(pending_deferred_captures)} deferred heavy capture(s) at free={human_bytes(free)}\n",
                )
                for capture in pending_deferred_captures:
                    deferred_heavy_capture(capture)
                pending_deferred_captures.clear()

        elapsed = time.time() - cycle_start
        sleep_for = max(1, INTERVAL - elapsed)
        append(MAIN_LOG, f"[{now()}] cycle_elapsed={elapsed:.1f}s sleep={sleep_for:.1f}s\n")
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
