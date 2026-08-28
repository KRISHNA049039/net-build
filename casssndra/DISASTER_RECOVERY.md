# Disaster recovery

[`RECOVERY.md`](RECOVERY.md) covers fault tolerance — surviving a node
or rack going down while the rest of the cluster keeps running. This doc
covers the scenario that isn't: **the whole cluster is gone** — all 5
nodes, a site disaster, corrupted data cluster-wide. RF=3 doesn't help
you here; only a checkpoint stored somewhere else does.

---

## The model: checkpoints + watermarks

A **checkpoint** is a coordinated, cluster-wide backup round: every node
snapshots under the *same tag* at roughly the same time, so the set of 5
tarballs together represents one coherent point in time you could
rebuild the whole cluster from.

A **watermark** is the answer to "what's the most recent point we could
actually recover to, right now, if disaster struck this second." Not
every checkpoint attempt is one — a node that failed to snapshot, a
tarball that got corrupted in transfer, a node that's still mid-upload —
none of those count. The watermark is specifically the most recent
checkpoint where **all 5 nodes' tarballs are present and their checksums
verify** — [`scripts/consolidate-checkpoint.sh`](scripts/consolidate-checkpoint.sh)
computes exactly this and prints it as `WATERMARK: latest complete
checkpoint = <tag>`. Anything written to the cluster after that tag is,
by definition, not covered by any complete backup yet — that gap is your
*current actual RPO exposure*, not the target you wrote in a doc once.

```
per-node checkpoint.sh          -> tarball + watermark.json, per node
        |
        v (CHECKPOINT_REMOTE push)
backup host: consolidate-checkpoint.sh
        |  groups by tag, verifies checksums,
        |  marks tag complete only if all 5 nodes verify
        v
      manifest.json  <-  the watermark ledger
        |
        v
  verify-restore.sh   ->  proves the latest complete checkpoint's
                           SSTables actually pass Cassandra's own
                           integrity check (sstableverify), not just
                           "the file exists"
```

---

## RPO / RTO

- **RPO target: 24h.** Checkpoints run nightly (schedule below). Actual
  current RPO is whatever `consolidate-checkpoint.sh`'s watermark output
  says — check it, don't assume the target is being met.
- **RTO: not a fixed number — measure it.** The rebuild runbook below is
  the procedure; the first time you actually run it end-to-end (do this
  as a drill, not for the first time during a real disaster) is when you
  get a real RTO number. Until then treat any RTO estimate here as a
  guess, not a commitment.

Both are configurable: checkpoint more often for a tighter RPO (cron
schedule below), and the biggest RTO lever is how much of the rebuild
runbook you've actually rehearsed vs. improvising live.

---

## Automated pieces and their schedule

| Script | Runs where | Schedule | Does |
|---|---|---|---|
| `scripts/checkpoint.sh` | each of the 5 Cassandra hosts | nightly, **same tag across all 5** (pass it explicitly, e.g. the date) | snapshot, export, hash, write watermark, push to backup host |
| `scripts/consolidate-checkpoint.sh` | the backup host | right after checkpoints land (e.g. 30 min after the nightly run) | verify checksums, mark complete, update the watermark |
| `scripts/verify-restore.sh` | the backup host (or any Docker machine with access to the checkpoints) | weekly | `sstableverify` against the latest complete checkpoint |
| `scripts/repair.sh` | each node | weekly, staggered (see `RECOVERY.md`) | anti-entropy — unrelated to checkpoints, still required |

Example cron, coordinated so the same tag lands everywhere (adjust per
host in the real crontab/Task Scheduler — this is the logical schedule):
```
0 0 * * *   TAG=$(date -u +\%Y\%m\%dT000000Z); scripts/checkpoint.sh cassandra-N /backups "$TAG"
30 0 * * *  scripts/consolidate-checkpoint.sh /backups              # on the backup host
0 2 * * 0   scripts/verify-restore.sh /backups                      # weekly, backup host
```
On the airgapped Windows hosts, same commands via Task Scheduler (Git
Bash, which these scripts already assume — see `RECOVERY.md`'s note on
`repair.sh`).

**What "off-site" honestly means here**: `CHECKPOINT_REMOTE` automates
getting a checkpoint off the node it was taken on, onto a designated
backup host. If that backup host is on the *same* airgapped LAN/site as
the 5 Cassandra nodes, this protects against losing individual
nodes/disks, **not** against losing the site — a fire or physical
destruction of the location takes the backup host with it too. True
off-site means that backup host's storage is physically somewhere else,
or its checkpoints get rotated onto removable media that leaves the
site. That last step isn't automatable by a script — it's a process
your team has to actually run. Don't skip it because the automation up
to that point makes it feel handled.

---

## Whole-cluster rebuild runbook

Use when: all 5 nodes are lost/corrupted and you're rebuilding from the
latest complete checkpoint. This restores each node's data *including*
`system`/`system_auth`/`system_schema` (`checkpoint.sh` snapshots every
keyspace, not just the app ones) onto hosts reusing the **same identity**
(broadcast address, rack, node name) as the node being restored — this
is the standard Cassandra snapshot-restore model, not a "join as a brand
new node" flow.

**Before you start**: this procedure is written from Cassandra's
documented snapshot-restore approach but has not been run end-to-end in
this environment — rehearse it (e.g. against the single-machine test rig
in `../AIRGAP_TESTING.md`) before you need it for real, and fix this doc
based on what you find.

1. **Provision 5 hosts** with the exact same `BIND_IP`/`NODE_NAME`/`CASSANDRA_RACK`
   as before (`dis/envs/pcNNN.env` — don't change these, the restored
   `system` keyspace data assumes the same node identities).
2. **Get the latest complete checkpoint's tarballs onto the matching
   hosts** — `cassandra-1`'s tarball onto the host that will run
   `cassandra-1`, etc. Check `manifest.json`'s watermark first.
3. **Pre-seed each host's data volume before first boot** (do NOT start
   the container on an empty volume first):
   ```bash
   docker volume create cassandra-data-cassandra-1     # matches docker-compose.node.yml's naming
   # extract the tarball and reassemble snapshot dirs into the live
   # layout, same transform verify-restore.sh does:
   mkdir -p /tmp/restore && tar xzf cassandra-1-<tag>.tar.gz -C /tmp/restore
   # for each <ks>/<table-uuid>/snapshots/<tag>/ dir found, its contents
   # go to <ks>/<table-uuid>/ (one level up, out of snapshots/<tag>/)
   docker run --rm -v /tmp/restore/reassembled:/source:ro \
     -v cassandra-data-cassandra-1:/target alpine \
     sh -c "cp -r /source/. /target/"
   ```
4. **First boot with `auto_bootstrap` disabled** — the data is already
   local, so Cassandra shouldn't try to re-stream it as if this were a
   brand-new node joining:
   ```
   # in that host's envs/pcNNN.env:
   JVM_EXTRA_OPTS=-Dcassandra.auto_bootstrap=false
   ```
5. **Bring up all 5 in the normal seed-first order** (`dis/RUNBOOK.md`
   §1), same as any other cluster start.
6. **Verify**: `nodetool status` → 5x `UN`. Then confirm auth actually
   works (`cqlsh -u cassandra -p <the password from the restored
   system_auth>` — it's whatever it was *at checkpoint time*, which
   matters if you'd rotated it since) and that app data round-trips.
7. **Run repair on every node immediately** — the checkpoint is a
   point-in-time snapshot; anything written between the checkpoint and
   the disaster is gone (that's the RPO gap), but repair reconciles any
   remaining inconsistency between the 5 nodes' restored data.
8. **Remove `JVM_EXTRA_OPTS`** from every host's `.env` once the ring is
   confirmed stable — it was only needed for this one restore boot.

---

## Related docs

- [`RECOVERY.md`](RECOVERY.md) — node/rack-level fault tolerance (the
  thing that usually means you never need this doc).
- [`AUTH.md`](AUTH.md) — why `system_auth`'s own RF matters even though
  checkpoints capture it regardless.
- [`../AIRGAP_TESTING.md`](../AIRGAP_TESTING.md) — where to rehearse the
  rebuild runbook, and `jq`'s now on the list of tools to vendor for the
  airgapped side (`consolidate-checkpoint.sh`/`verify-restore.sh` need it).
