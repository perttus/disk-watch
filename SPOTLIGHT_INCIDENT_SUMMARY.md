# Spotlight Store Incident Summary

## Summary

This repository was used to investigate repeated low-disk-space incidents on macOS that turned out to be caused by Spotlight's own Data-volume index store under `/System/Volumes/Data/.Spotlight-V100`.

The key lessons were:

- cloud sync providers were not required for the problem to happen
- `mds_stores` was doing real compaction and merge work, not just idling
- the actual disk usage was concentrated in one giant Spotlight index file
- the problem may have been building for a long time before it became obvious at low free-space levels
- once free space is severely constrained, overall system instability can follow more easily under heavy disk I/O or swap pressure
- giving Full Disk Access to the terminal was necessary to inspect the store reliably
- the pathological store later collapsed back to a normal size on its own

## Symptoms

The failure pattern looked like this:

- free space suddenly collapsed, sometimes to under `1 GB`
- `mds_stores` stayed active during the event
- Spotlight state sometimes reported `kMDConfigSearchLevelTransitioning`
- open-file snapshots showed many `journalAttr.*` and `tmp.merge.*` files under the active `Store-V2` directory
- free space later returned in one large jump without explicit cleanup

One plausible interpretation is that the underlying store growth had been happening earlier and only became visible once remaining free space fell below a practical tipping point. In other words, the visible outage may be the last stage of a longer deterioration rather than a purely sudden one-shot event.

That distinction matters because the disk-space collapse itself can be enough to destabilize the machine. Once free space gets tight, heavy disk activity, memory pressure, or swap growth can amplify the failure and turn a storage problem into broader system instability.

## What Confirmed The Cause

### 1. The issue reproduced without cloud providers

The same behavior occurred even after cloud sync clients were removed or disconnected. That ruled them out as required active causes.

### 2. `mds_stores` was sampled on Spotlight compaction paths

Process samples repeatedly showed `mds_stores` inside Spotlight merge and compaction code such as:

- `si_mergeIndex`
- `OuterMerge`
- `InnerMerge`
- `mergeIndexData`

That is a strong signal that Spotlight was actively rebuilding or compacting its own store.

### 3. Open files showed active merge artifacts

During bad runs, `mds_stores` had the following kinds of files open under the Data Spotlight store:

- many `journalAttr.*` files
- `tmp.merge.*` files
- many `live.*` index files
- the store root and active `Store-V2` directory

That pattern is consistent with an in-progress merge or generation rollover.

### 4. Full Disk Access was necessary to inspect the real size

One important operational detail was that `sudo` alone was not enough to inspect `.Spotlight-V100` reliably in every case.

Without Full Disk Access for the terminal app, commands such as:

- `du`
- `ncdu`

could still fail with `Operation not permitted`, even when run with `sudo`.

After granting Full Disk Access to the terminal, inspection of `/System/Volumes/Data/.Spotlight-V100` became reliable enough to see the actual oversized file.

### 5. The oversized store was dominated by one file

Once the store could be inspected properly, it turned out not to be a case of many small files slowly accumulating.

Instead, the Data Spotlight store had grown to about `262G`, and almost all of that was concentrated in one file inside the active `Store-V2` directory:

- `live.4.indexPositions` at about `256.0 GiB`

That is the most direct evidence that the low-space event was driven by Spotlight's own index data.

### 6. The free-space recovery matched the size of the giant file disappearing

When the system recovered, free space jumped by roughly the same amount as the size of the pathological `live.4.indexPositions` file.

That strongly suggests Spotlight completed or abandoned a generation during compaction, and the huge file was finally released.

## Recovered Baseline

After the bad generation disappeared, the system returned to a healthy baseline.

The important post-recovery characteristics were:

- free space stable again
- no `tmp.merge.*`
- no `journalAttr.*` spike
- no `kMDConfigSearchLevelTransitioning`
- Data Spotlight store size back down to about `5.9 GiB`

That is a useful reference point for deciding whether a future run is healthy or not.

## Did Full Disk Access Help Recovery?

The clear part is this: Full Disk Access for the terminal was necessary to see the oversized Spotlight store and identify the giant file reliably.

The unclear part is whether granting Full Disk Access helped the system recover.

It may have had no effect on recovery at all, and the timing may simply have overlapped with Spotlight finally completing or abandoning the bad compaction generation.

So the safe interpretation is:

- Full Disk Access definitely helped diagnosis
- it might have helped recovery indirectly, but that is not proven

## Security Note On Full Disk Access

Leaving Full Disk Access enabled for a terminal app is usually acceptable for controlled troubleshooting, but it is still a broad permission.

The important operational detail is that anything run from that terminal inherits the same access, including shell scripts, child processes, and copied commands.

So the practical guidance is:

- keep it enabled only on a terminal app you trust
- avoid running untrusted scripts or ad hoc commands there
- prefer using it for diagnostics rather than as a permanent blanket default everywhere
- remove it later if you no longer need visibility into protected Spotlight paths

## Healthy vs Suspicious Signals

Not every period of Spotlight activity is a problem. A useful quick triage is to separate normal churn from signals that look more like a pathological store generation.

More likely healthy:

- free space is broadly stable
- no persistent `tmp.merge.*` files are visible
- no `journalAttr.*` spike is visible
- Spotlight is not stuck in `kMDConfigSearchLevelTransitioning`
- the Data Spotlight store stays near its known-good baseline size

More likely suspicious:

- free space keeps trending down or drops in repeated bursts
- `mds_stores` remains active while free space worsens
- `tmp.merge.*` and many `journalAttr.*` files stay present under the active `Store-V2` directory
- Spotlight reports `kMDConfigSearchLevelTransitioning` during the bad window
- one or more `live.*.indexPositions` files become disproportionately large
- the Data Spotlight store stays far above its recovered baseline size

## Practical Takeaways

If someone else hits a similar macOS disk-space collapse, the most useful checks are:

1. Look at `mds_stores` activity rather than assuming a cloud client is at fault.
2. Check whether `.Spotlight-V100` is the thing consuming space.
3. Give the terminal app Full Disk Access before trusting `du` or `ncdu` results on protected Spotlight paths.
4. Look for `tmp.merge.*`, `journalAttr.*`, and large `live.*` files inside the active `Store-V2` directory.
5. Pay special attention to oversized `live.*.indexPositions` files.
6. Compare the current store size against a healthy baseline after recovery.
7. Monitor free space continuously, because a long-running indexing problem may stay mostly hidden until available headroom gets low enough that the next growth burst becomes user-visible.
8. Consider excluding especially noisy high-file-count folders from Spotlight in System Settings privacy controls if they do not need to be searchable.
9. Do not assume a privacy exclusion immediately removes already indexed data from an existing store; if the store is already pathological, a reset or rebuild may still be needed before space is reclaimed.
10. Treat severe free-space loss as a stability risk in its own right, especially when the machine is also under heavy disk I/O or swap pressure.

## Local Tools In This Repo

This repo also contains two local helper scripts that were useful during the investigation:

- [boot_spotlight_check.py](boot_spotlight_check.py)
- [disk_watch.py](disk_watch.py)

They are repository-specific helpers, but the core findings above are intended to be useful even without them.
