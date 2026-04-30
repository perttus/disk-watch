#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DURATION_SECONDS = 90
DEFAULT_INTERVAL_SECONDS = 15
FREE_DROP_THRESHOLD_GB = 2
JOURNAL_ATTR_THRESHOLD = 8
SPOTLIGHT_DATA_STORE_DELTA_THRESHOLD = 10
IVF_VECTOR_DELTA_THRESHOLD = 4
DATA_SPOTLIGHT_SIZE_DELTA_THRESHOLD_GB = 1
DATA_SPOTLIGHT_ROOT = "/System/Volumes/Data/.Spotlight-V100"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def human_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:,.2f} {unit}"
        size /= 1024.0
    return f"{value} B"


def run_cmd(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return process.returncode, process.stdout, process.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:  # pragma: no cover - defensive wrapper
        return 1, "", f"ERROR running {' '.join(cmd)}: {exc}"


def disk_free_bytes() -> int:
    return shutil.disk_usage("/").free


def directory_size_bytes(path: str) -> tuple[int | None, str | None]:
    rc, out, err = run_cmd(["/usr/bin/du", "-skx", path], timeout=120)
    if rc != 0:
        failure = err.strip() or f"du rc={rc}"
        return None, failure

    first_line = out.splitlines()[0].strip() if out.splitlines() else ""
    if not first_line:
        return None, "du returned no output"

    size_kib = first_line.split()[0]
    try:
        return int(size_kib) * 1024, None
    except ValueError:
        return None, f"unexpected du output: {first_line}"


def parse_mdutil_status(output: str) -> dict[str, str]:
    status = {}
    current_volume = None
    for line_text in output.splitlines():
        if not line_text.strip():
            continue
        if line_text.endswith(":"):
            current_volume = line_text.strip().rstrip(":")
            continue
        if current_volume:
            status[current_volume] = line_text.strip()
            current_volume = None
    return status


def mdutil_anomalies(status: dict[str, str]) -> list[str]:
    anomalies = []
    for volume, state in status.items():
        lowered = state.lower()
        if "error:" in lowered or "unexpected indexing state" in lowered:
            anomalies.append(f"{volume}: {state}")
    return anomalies


def get_mds_stores_pids() -> list[str]:
    rc, out, _ = run_cmd(["/usr/bin/pgrep", "-x", "mds_stores"], timeout=30)
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def summarize_open_paths(paths: list[str]) -> tuple[list[str], dict[str, int]]:
    unique_paths = []
    seen = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        unique_paths.append(path)

    counts = {
        "spotlight_data_store": 0,
        "spotlight_private_store": 0,
        "spotlight_boot_volume_store": 0,
        "spotlight_preboot_store": 0,
        "journal_attr": 0,
        "tmp_merge": 0,
        "ivf_vectors": 0,
        "temp_files": 0,
    }

    for path in unique_paths:
        if path.startswith("/System/Volumes/Data/.Spotlight-V100/"):
            counts["spotlight_data_store"] += 1
        if path.startswith("/private/var/db/Spotlight-V100/"):
            counts["spotlight_private_store"] += 1
        if path.startswith("/private/var/db/Spotlight-V100/BootVolume/"):
            counts["spotlight_boot_volume_store"] += 1
        if path.startswith("/private/var/db/Spotlight-V100/Preboot/"):
            counts["spotlight_preboot_store"] += 1
        if "/journalAttr." in path:
            counts["journal_attr"] += 1
        if "/tmp.merge." in path:
            counts["tmp_merge"] += 1
        if ".ivf-" in path:
            counts["ivf_vectors"] += 1
        if path.startswith("/private/var/folders/"):
            counts["temp_files"] += 1

    return unique_paths, counts


def collect_open_paths_for_pid(pid: str) -> tuple[list[str], str | None]:
    rc, out, err = run_cmd(["/usr/sbin/lsof", "-nP", "-Fn", "-p", pid], timeout=120)
    if rc != 0:
        return [], err.strip() or f"lsof rc={rc}"

    paths = []
    for line_text in out.splitlines():
        if line_text.startswith("n"):
            path = line_text[1:]
            if path:
                paths.append(path)
    return paths, None


def collect_sample() -> dict:
    aggregate_counts = {
        "spotlight_data_store": 0,
        "spotlight_private_store": 0,
        "spotlight_boot_volume_store": 0,
        "spotlight_preboot_store": 0,
        "journal_attr": 0,
        "tmp_merge": 0,
        "ivf_vectors": 0,
        "temp_files": 0,
    }
    pid_summaries: list[dict[str, object]] = []
    suspicious_reasons: list[str] = []
    data_spotlight_store_bytes, data_spotlight_store_error = directory_size_bytes(
        DATA_SPOTLIGHT_ROOT
    )

    sample = {
        "timestamp": now(),
        "disk_free_bytes": disk_free_bytes(),
        "data_spotlight_store_bytes": data_spotlight_store_bytes,
        "data_spotlight_store_error": data_spotlight_store_error,
        "mdutil_status_raw": "",
        "mdutil_status": {},
        "mdutil_anomalies": [],
        "mds_stores_pids": [],
        "pid_summaries": pid_summaries,
        "aggregate_counts": aggregate_counts,
        "suspicious_reasons": suspicious_reasons,
    }

    sample_mdutil_status: dict[str, str] = {}
    sample_mdutil_anomalies: list[str] = []

    rc, out, err = run_cmd(["/usr/bin/mdutil", "-a", "-s"], timeout=60)
    if rc == 0:
        sample["mdutil_status_raw"] = out.strip()
        sample_mdutil_status = parse_mdutil_status(out)
        sample_mdutil_anomalies = mdutil_anomalies(sample_mdutil_status)
    else:
        mdutil_failure = f"FAILED: {err.strip()}"
        sample["mdutil_status_raw"] = mdutil_failure
        sample_mdutil_anomalies = [mdutil_failure]

    sample["mdutil_status"] = sample_mdutil_status
    sample["mdutil_anomalies"] = sample_mdutil_anomalies

    pids = get_mds_stores_pids()
    sample["mds_stores_pids"] = pids

    aggregate_unique_paths = []
    aggregate_seen = set()

    for pid in pids:
        paths, error = collect_open_paths_for_pid(pid)
        unique_paths, counts = summarize_open_paths(paths)
        pid_summaries.append(
            {
                "pid": pid,
                "error": error,
                "unique_paths": unique_paths,
                "counts": counts,
            }
        )
        for path in unique_paths:
            if path not in aggregate_seen:
                aggregate_seen.add(path)
                aggregate_unique_paths.append(path)

    _, aggregate_counts = summarize_open_paths(aggregate_unique_paths)
    sample["aggregate_counts"] = aggregate_counts

    if not pids:
        suspicious_reasons.extend(sample_mdutil_anomalies)
        return sample

    suspicious_reasons.extend(sample_mdutil_anomalies)

    if aggregate_counts["journal_attr"] >= JOURNAL_ATTR_THRESHOLD:
        suspicious_reasons.append(
            f"journalAttr files open: {aggregate_counts['journal_attr']}"
        )
    if aggregate_counts["tmp_merge"] > 0:
        suspicious_reasons.append(
            f"tmp.merge files open: {aggregate_counts['tmp_merge']}"
        )

    return sample


def classify_run(samples: list[dict]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    suspicious_samples = [sample for sample in samples if sample["suspicious_reasons"]]
    if suspicious_samples:
        reasons.append(f"suspicious samples: {len(suspicious_samples)}/{len(samples)}")

    if samples:
        free_drop = samples[0]["disk_free_bytes"] - samples[-1]["disk_free_bytes"]
        if free_drop >= FREE_DROP_THRESHOLD_GB * 1024**3:
            reasons.append(f"free space dropped by {human_bytes(free_drop)} during sampling")

        spotlight_counts = [sample["aggregate_counts"]["spotlight_data_store"] for sample in samples]
        ivf_counts = [sample["aggregate_counts"]["ivf_vectors"] for sample in samples]
        journal_counts = [sample["aggregate_counts"]["journal_attr"] for sample in samples]
        data_store_sizes = [
            sample["data_spotlight_store_bytes"]
            for sample in samples
            if sample["data_spotlight_store_bytes"] is not None
        ]
        pid_sets = [tuple(sample["mds_stores_pids"]) for sample in samples]

        spotlight_delta = max(spotlight_counts) - min(spotlight_counts)
        if spotlight_delta >= SPOTLIGHT_DATA_STORE_DELTA_THRESHOLD:
            reasons.append(f"Spotlight data-store path count changed by {spotlight_delta}")

        ivf_delta = max(ivf_counts) - min(ivf_counts)
        if ivf_delta >= IVF_VECTOR_DELTA_THRESHOLD:
            reasons.append(f"ivf vector path count changed by {ivf_delta}")

        journal_delta = max(journal_counts) - min(journal_counts)
        if journal_delta > 0 and max(journal_counts) >= JOURNAL_ATTR_THRESHOLD:
            reasons.append(
                f"journalAttr path count changed from {min(journal_counts)} to {max(journal_counts)}"
            )

        if data_store_sizes:
            data_store_delta = max(data_store_sizes) - min(data_store_sizes)
            if data_store_delta >= DATA_SPOTLIGHT_SIZE_DELTA_THRESHOLD_GB * 1024**3:
                reasons.append(
                    f"Data Spotlight store size changed by {human_bytes(data_store_delta)}"
                )

        mdutil_reasons = sorted(
            {
                anomaly
                for sample in samples
                for anomaly in sample["mdutil_anomalies"]
            }
        )
        reasons.extend(mdutil_reasons)

        if len(set(pid_sets)) > 1:
            reasons.append("mds_stores PID set changed during sampling")

    issue_detected = bool(reasons)
    return issue_detected, reasons


def write_report(log_dir: Path, samples: list[dict], issue_detected: bool, reasons: list[str]) -> None:
    report_path = log_dir / "report.txt"
    mdutil_path = log_dir / "mdutil_status.log"
    open_files_path = log_dir / "mds_stores_open_files.log"
    size_log_path = log_dir / "spotlight_store_sizes.log"

    with report_path.open("w", encoding="utf-8") as report:
        report.write(f"started_at={samples[0]['timestamp'] if samples else now()}\n")
        report.write(f"finished_at={samples[-1]['timestamp'] if samples else now()}\n")
        report.write(f"issue_detected={'yes' if issue_detected else 'no'}\n")
        if reasons:
            report.write("reasons:\n")
            for reason in reasons:
                report.write(f"- {reason}\n")
        report.write("\n")
        for index, sample in enumerate(samples, start=1):
            counts = sample["aggregate_counts"]
            report.write(
                f"sample={index} timestamp={sample['timestamp']} free={human_bytes(sample['disk_free_bytes'])} "
                f"data_store_size={human_bytes(sample['data_spotlight_store_bytes'])} "
                f"pids={','.join(sample['mds_stores_pids']) or 'none'} "
                f"spotlight_data_store={counts['spotlight_data_store']} "
                f"journal_attr={counts['journal_attr']} tmp_merge={counts['tmp_merge']} "
                f"ivf_vectors={counts['ivf_vectors']}\n"
            )
            if sample["data_spotlight_store_error"]:
                report.write(
                    f"  data_store_size_error: {sample['data_spotlight_store_error']}\n"
                )
            for reason in sample["suspicious_reasons"]:
                report.write(f"  suspicious: {reason}\n")

    with mdutil_path.open("w", encoding="utf-8") as mdutil_log:
        for sample in samples:
            mdutil_log.write(f"[{sample['timestamp']}]\n")
            mdutil_log.write(sample["mdutil_status_raw"] + "\n\n")

    with open_files_path.open("w", encoding="utf-8") as open_files_log:
        for sample in samples:
            open_files_log.write(f"[{sample['timestamp']}]\n")
            for pid_summary in sample["pid_summaries"]:
                open_files_log.write(f"pid={pid_summary['pid']}\n")
                if pid_summary["error"]:
                    open_files_log.write(f"error={pid_summary['error']}\n")
                    continue
                counts = pid_summary["counts"]
                open_files_log.write(
                    "summary "
                    f"spotlight_data_store={counts['spotlight_data_store']} "
                    f"spotlight_private_store={counts['spotlight_private_store']} "
                    f"spotlight_boot_volume_store={counts['spotlight_boot_volume_store']} "
                    f"spotlight_preboot_store={counts['spotlight_preboot_store']} "
                    f"journal_attr={counts['journal_attr']} tmp_merge={counts['tmp_merge']} "
                    f"ivf_vectors={counts['ivf_vectors']} temp_files={counts['temp_files']}\n"
                )
                for path in pid_summary["unique_paths"]:
                    open_files_log.write(path + "\n")
                open_files_log.write("\n")

    with size_log_path.open("w", encoding="utf-8") as size_log:
        for sample in samples:
            size_log.write(f"[{sample['timestamp']}] data_spotlight_store={human_bytes(sample['data_spotlight_store_bytes'])}")
            if sample["data_spotlight_store_error"]:
                size_log.write(f" error={sample['data_spotlight_store_error']}")
            size_log.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sample Spotlight state after reboot and report whether mds_stores looks suspicious."
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_SECONDS,
        help=f"total sampling duration in seconds (default: {DEFAULT_DURATION_SECONDS})",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between samples (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0 or args.interval <= 0:
        print("duration and interval must be positive integers", file=sys.stderr)
        return 1

    log_dir = Path.cwd() / "logs" / f"boot_spotlight_check_{timestamp_slug()}"
    log_dir.mkdir(parents=True, exist_ok=True)

    if os.geteuid() != 0:
        print("warning: run with sudo for complete mdutil and lsof visibility", file=sys.stderr)

    print(f"Writing logs to: {log_dir}")
    samples = []
    start = time.time()
    deadline = start + args.duration

    while True:
        sample = collect_sample()
        samples.append(sample)
        counts = sample["aggregate_counts"]
        print(
            f"[{sample['timestamp']}] free={human_bytes(sample['disk_free_bytes'])} "
            f"data_store_size={human_bytes(sample['data_spotlight_store_bytes'])} "
            f"pids={','.join(sample['mds_stores_pids']) or 'none'} "
            f"spotlight_data_store={counts['spotlight_data_store']} "
            f"journal_attr={counts['journal_attr']} tmp_merge={counts['tmp_merge']} "
            f"ivf_vectors={counts['ivf_vectors']}"
        )
        for reason in sample["suspicious_reasons"]:
            print(f"  suspicious: {reason}")

        now_ts = time.time()
        if now_ts >= deadline:
            break
        time.sleep(min(args.interval, max(0, deadline - now_ts)))

    issue_detected, reasons = classify_run(samples)
    write_report(log_dir, samples, issue_detected, reasons)

    if issue_detected:
        print("\nLIKELY ISSUE DETECTED")
        for reason in reasons:
            print(f"- {reason}")
        print(f"See: {log_dir / 'report.txt'}")
        return 2

    print("\nNo clear Spotlight issue detected during sampling")
    print(f"See: {log_dir / 'report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())