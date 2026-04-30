# Disk Watch

Small macOS-focused utility for catching short-lived disk space drops that are hard to reproduce manually.

It is also useful when a disk-space problem may have been building quietly for a long time and only becomes obvious after free space reaches a tipping point. In that situation, continuous free-space monitoring helps distinguish a sudden one-off spike from a longer deterioration that only surfaced late.

The script polls free space on `/` every 30 seconds, writes a time series CSV, and when free space drops below a threshold it captures a deeper snapshot of disk activity and large watched directories. This is useful when the disk appears to fill up temporarily and then recover before you can inspect it.

For the broader Spotlight-specific findings and operational takeaways from the incident that motivated this tool, see [SPOTLIGHT_INCIDENT_SUMMARY.md](SPOTLIGHT_INCIDENT_SUMMARY.md).

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
- an optional bounded File Provider daemon and domain dump from `fileproviderctl dump -l`
- Spotlight indexing status from `mdutil -a -s`
- open-file paths for `mds_stores`, summarized and captured from `lsof`
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
- `/private/var/db/Spotlight-V100`, including BootVolume and Preboot stores
- `/private/var/vm`

## Requirements

- macOS
- Python 3.13+
- permission to run system tools such as `du`, `ps`, `lsof`, and `fs_usage`

Run the watcher with `sudo` so `fs_usage` and the other system tooling can collect the data you need.

If you need to inspect protected Spotlight paths such as `/System/Volumes/Data/.Spotlight-V100`, `sudo` may still not be enough on its own. In practice, the terminal app may also need Full Disk Access before `du`, `ncdu`, or similar tools can inspect those paths reliably.

## Running

Run the watcher from the project root with `sudo`:

```bash
sudo python3 disk_watch.py
```

By default the script uses the invoking account from `sudo` via `SUDO_USER` to build the watched `~/Library/...` paths. If you need to target a different macOS account, override it explicitly:

```bash
sudo DISK_WATCH_USER=your.username python3 disk_watch.py
```

The script leaves `fileproviderctl dump -l` disabled by default because it can wake File Provider state during an incident. If you explicitly want that artifact, opt in:

```bash
sudo DISK_WATCH_ENABLE_FILE_PROVIDER_DUMP=1 python3 disk_watch.py
```

Some macOS locations are still protected by TCC or system policy even when the process runs under `sudo`. In those cases the script logs the permission errors and keeps any partial `du` total that macOS still returns.

If you grant Full Disk Access for this purpose, treat it as a broad permission for that terminal app: commands, scripts, and child processes launched from it inherit the same access.

If you want a short post-reboot check instead of the long-running watcher, run:

```bash
sudo python3 boot_spotlight_check.py
```

That helper samples `mds_stores` for 90 seconds, checks `mdutil -a -s`, summarizes open Spotlight store files, and exits with `0` when no clear issue is seen or `2` when the reboot looks suspicious. You can shorten or extend the window with `--duration` and `--interval`.

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
- `file_provider_dump.log`: either a skip marker or, when explicitly enabled, bounded `fileproviderctl dump -l` output showing providers and domains
- `file_provider_summary.log`: either a skip marker or, when explicitly enabled, a concise per-provider and per-domain summary extracted from the full File Provider dump, including disconnected state, indexer flags, and `keepDownloaded` versus `lazy` item counts
- `spotlight_status.log`: `mdutil -a -s` output for current Spotlight indexing status
- `mds_stores_open_files.log`: per-process `lsof` capture for `mds_stores`, with counts for Spotlight data-store, journal, merge, and IVF vector-index files
- `lsof_deleted_open.log`: deleted files still held open by processes
- `unified_log_spotlight.log`: filtered `log show` output for Spotlight, CoreSpotlight, and File Provider repair/indexing activity from the last 15 minutes

Each `boot_spotlight_check.py` run creates a `logs/boot_spotlight_check_<timestamp>/` directory containing:

- `report.txt`: sample-by-sample classification and final verdict
- `mdutil_status.log`: raw `mdutil -a -s` output for each sample
- `mds_stores_open_files.log`: per-sample `lsof` output grouped by `mds_stores` PID

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
- `fileproviderctl dump -l` is disabled by default and can be enabled with `DISK_WATCH_ENABLE_FILE_PROVIDER_DUMP=1`

If you need to watch a different account than the one invoking `sudo`, set `DISK_WATCH_USER` accordingly.

## Typical workflow

1. Start the script and leave it running continuously so it can capture both gradual free-space erosion and sharp incident drops.
2. Inspect `disk_space.csv` to find the time window where free space dropped.
3. Check `lowspace.log` and the `fs_usage` logs for the same window.
4. If the incident hit the minimal-log threshold, look in `main.log` for the incident start, recovery, and deferred-capture markers.
5. Check `file_provider_summary.log` first to see which File Provider domains were mounted, disconnected, or still index-enabled during the incident.
6. Check `unified_log_spotlight.log` for `mds_stores`, `corespotlightd`, `fileproviderd`, `repair_lookupPath`, and `forceToOrphanParent` entries in that window.
7. Compare watched path sizes and look for large changes in caches, containers, Spotlight, or `/private/var/vm`.
8. Review `lsof_deleted_open.log` for space held by deleted files that processes have not released when that capture was not skipped due to severe disk pressure.
9. Pay particular attention to the OneDrive and Google Drive File Provider state, their CloudStorage roots, and their support directories if either provider disappears, pauses, or restarts during a low-space event.

## Notes

- The watcher measures total filesystem free space, not per-volume free space for arbitrary mount points.
- Path sizes are collected with `du -skx`, so each watched path stays on its own filesystem.
- `fs_usage` output is intentionally summarized and sampled so one noisy process does not dominate the log file.
- When the filesystem is out of space, log writes are treated as best-effort so the watcher keeps running instead of crashing on `OSError: [Errno 28] No space left on device`.
- Some watched paths, including Spotlight and protected Library subtrees, may still report permission errors even under `sudo`; the script now keeps partial totals when `du` provides one.
- When the post-restart incident is driven by Spotlight, `mds_stores_open_files.log` should reveal whether the pressure is concentrated in `.Spotlight-V100`, BootVolume, Preboot, `journalAttr.*`, `tmp.merge.*`, or IVF vector-index files.
- As a mitigation, it can be worth excluding very large or high-file-count folders from Spotlight using the system privacy settings when those paths do not need to be searchable.
- That kind of exclusion mainly affects future indexing pressure; it does not necessarily remove already indexed data from an existing Spotlight store until the index is reset or rebuilt.
- Because the script writes logs continuously, keep the log directory itself in mind during long runs.
