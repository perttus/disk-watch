# Disk Watch

Small macOS-focused utility for catching short-lived disk space drops that are hard to reproduce manually.

The script polls free space on `/` every 30 seconds, writes a time series CSV, and when free space drops below a threshold it captures a deeper snapshot of disk activity and large watched directories. This is useful when the disk appears to fill up temporarily and then recover before you can inspect it.

## What it captures

On every cycle:

- total, used, and free disk space for `/`
- a CSV row in `disk_space.csv`
- a summary entry in `main.log`

When free space drops below the low-space threshold:

- `fs_usage` sample for `diskio`
- `fs_usage` sample for `filesys`
- a process snapshot from `ps`
- a filtered File Provider plugin snapshot from `pluginkit`
- a bounded File Provider daemon and domain dump from `fileproviderctl dump -l`
- deleted-but-still-open files from `lsof +L1`
- a filtered unified log snapshot for Spotlight, CoreSpotlight, and File Provider activity from the recent incident window
- `du -skx` size snapshots for selected paths that commonly grow unexpectedly on macOS

The low-space capture has a 10-minute cooldown, but the script now overrides that cooldown if free space keeps dropping sharply or reaches a critical level during the same incident.

If free space is already critically low, the script switches to a smaller capture mode so it does not try to write the largest artifacts while the disk is nearly full. In that mode it still records the most useful lightweight signals immediately, then queues the heaviest artifacts for a deferred capture after free space has recovered to a safer level.

## Watched paths

The current script tracks these locations:

- `~/Library/Caches`
- `~/Library/Application Support`
- `~/Library/Containers`
- `~/Library/Group Containers`
- `~/Library/Metadata`
- `~/Library/Group Containers/UBF8T346G9.OneDriveSyncClientSuite`
- OneDrive state directories under that group container, including `.noindex`, staging, hydration, and File Provider areas
- OneDrive CloudStorage roots such as `~/Library/CloudStorage/OneDrive-Personal`
- `~/Library/Group Containers/EQHXZ8M8AV.group.com.google.drivefs`
- Google Drive File Provider storage and state under that group container
- `~/Library/Application Support/Google/DriveFS`, including log and cache areas
- Google Drive CloudStorage roots such as `~/Library/CloudStorage/GoogleDrive-*`
- the parent of macOS temporary `var/folders`
- LaunchServices cache under `var/folders`
- `.Spotlight-V100`
- `/private/var/vm`

## Requirements

- macOS
- Python 3.13+
- permission to run system tools such as `du`, `ps`, `lsof`, and `fs_usage`

Run the watcher with `sudo` so `fs_usage` and the other system tooling can collect the data you need.

## Running

Run the watcher from the project root with `sudo`:

```bash
sudo python3 disk_watch.py
```

By default the script uses the invoking account from `sudo` via `SUDO_USER` to build the watched `~/Library/...` paths. If you need to target a different macOS account, override it explicitly:

```bash
sudo DISK_WATCH_USER=your.username python3 disk_watch.py
```

Some macOS locations are still protected by TCC or system policy even when the process runs under `sudo`. In those cases the script logs the permission errors and keeps any partial `du` total that macOS still returns.

It creates a timestamped log directory under `logs/`, for example:

```text
logs/disk_watch_20260424_103944/
```

## Output files

Each run creates a new directory containing:

- `main.log`: cycle summaries, command failures, and cooldown decisions
- `lowspace.log`: enriched summary written when a low-space capture happens
- `disk_space.csv`: timestamped free-space history
- `fs_usage_diskio.log`: disk I/O capture with a per-process summary plus a sampled subset of raw lines
- `fs_usage_filesys.log`: filesystem capture with a per-process summary plus a sampled subset of raw lines
- `process_snapshot.log`: top process snapshot captured during a low-space event
- `file_provider_plugins.log`: File Provider-related extension registrations seen by `pluginkit`
- `file_provider_dump.log`: bounded `fileproviderctl dump -l` output showing providers and domains
- `lsof_deleted_open.log`: deleted files still held open by processes
- `unified_log_spotlight.log`: filtered `log show` output for Spotlight, CoreSpotlight, and File Provider repair/indexing activity from the last 15 minutes

## Current defaults

The script currently uses these defaults in `disk_watch.py`:

- poll interval: 30 seconds
- low-space threshold: 200 GB free
- low-space capture cooldown: 10 minutes
- emergency cooldown override: free space below 20 GB or down by at least 20 GB since the last capture
- minimal-log capture mode below 10 GB free to avoid large writes during severe disk pressure
- deferred heavy capture after recovery above 40 GB free
- top process rows recorded: 30
- unified log capture window: last 15 minutes with a Spotlight/File Provider-focused predicate
- target user defaults to the invoking `sudo` user and can be overridden with `DISK_WATCH_USER`

If you need to watch a different account than the one invoking `sudo`, set `DISK_WATCH_USER` accordingly.

## Typical workflow

1. Start the script and leave it running while the intermittent disk growth reproduces.
2. Inspect `disk_space.csv` to find the time window where free space dropped.
3. Check `lowspace.log` and the `fs_usage` logs for the same window.
4. If the incident hit the minimal-log threshold, look in `main.log` for the incident start, recovery, and deferred-capture markers.
5. Check `unified_log_spotlight.log` for `mds_stores`, `corespotlightd`, `fileproviderd`, `repair_lookupPath`, and `forceToOrphanParent` entries in that window.
6. Compare watched path sizes and look for large changes in caches, containers, Spotlight, or `/private/var/vm`.
7. Review `lsof_deleted_open.log` for space held by deleted files that processes have not released when that capture was not skipped due to severe disk pressure.
8. Pay particular attention to the OneDrive and Google Drive File Provider state, their CloudStorage roots, and their support directories if either provider disappears, pauses, or restarts during a low-space event.

## Notes

- The watcher measures total filesystem free space, not per-volume free space for arbitrary mount points.
- Path sizes are collected with `du -skx`, so each watched path stays on its own filesystem.
- `fs_usage` output is intentionally summarized and sampled so one noisy process does not dominate the log file.
- When the filesystem is out of space, log writes are treated as best-effort so the watcher keeps running instead of crashing on `OSError: [Errno 28] No space left on device`.
- Some watched paths, including Spotlight and protected Library subtrees, may still report permission errors even under `sudo`; the script now keeps partial totals when `du` provides one.
- Because the script writes logs continuously, keep the log directory itself in mind during long runs.
