# Cassandra 5-Node Cluster on AWS — Test Runbook

Mirrors the airgapped target exactly: 5 EC2 instances, 1 Cassandra node
each, 2 racks, RF=3 `NetworkTopologyStrategy`. Same `docker-compose.node.yml`
as `dis/`, only the per-node `.env` differs (private IPs instead of LAN
IPs). The point of testing here first is that a config that works on this
topology should work unmodified on the airgapped one — don't add anything
AWS-specific (Elastic IPs, ALB, RDS-style managed anything) that the
airgapped cluster can't have.

| Node        | AZ (rack)     | Seed? |
|-------------|---------------|-------|
| cassandra-1 | AZ-a (rack1)  | yes |
| cassandra-2 | AZ-a (rack1)  | |
| cassandra-3 | AZ-a (rack1)  | |
| cassandra-4 | AZ-b (rack2)  | yes |
| cassandra-5 | AZ-b (rack2)  | |

--------------------------------------------------------------------
## 1. Launch the instances
--------------------------------------------------------------------
- 5x EC2, same VPC, 2 subnets (one per AZ, matching the rack split above).
- Instance type: `t3.medium` (2 vCPU/4GB) is enough for a 512M-heap smoke
  test; go bigger only if you're also load-testing, not just validating
  topology/recovery.
- AMI: Amazon Linux 2023 (or Ubuntu — anything with Docker installable).
- Root volume: 20GB+ gp3 (this is a test, not sized for real data volume).
- **No public IP needed** if you have a bastion/SSM Session Manager —
  keeps this closer to the airgapped reality where nothing is internet-facing.

Install Docker on each (Amazon Linux 2023):
```
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user   # re-login for group to take effect
```

--------------------------------------------------------------------
## 2. Security group
--------------------------------------------------------------------
One SG shared by all 5 instances, self-referencing (source = the SG
itself, not a CIDR) so it doesn't need editing as instances come and go:
```
aws ec2 create-security-group --group-name cassandra-cluster --description "Cassandra test cluster" --vpc-id <VPC_ID>
aws ec2 authorize-security-group-ingress --group-id <SG_ID> --protocol tcp --port 7000 --source-group <SG_ID>
aws ec2 authorize-security-group-ingress --group-id <SG_ID> --protocol tcp --port 9042 --source-group <SG_ID>
# + a rule allowing SSH/SSM from wherever you administer from
```

--------------------------------------------------------------------
## 3. Get the code onto each instance
--------------------------------------------------------------------
```
scp -r casssndra/ ec2-user@<instance>:~/
```
Or `git clone` if the repo is reachable from the instance.

--------------------------------------------------------------------
## 4. Fill in the per-node .env files
--------------------------------------------------------------------
For each of the 5 instances, get its private IP (`hostname -I` on the box,
or from the EC2 console) and fill in the matching
`dis/envs/aws-nodeN.env` placeholders:
- `BIND_IP` -> that instance's own private IP
- `SEED_IPS` -> both seeds' private IPs (same value on all 5 files)

--------------------------------------------------------------------
## 5. Bring up the cluster
--------------------------------------------------------------------
Same commands as the airgapped runbook, seeds first:
```
# on the cassandra-1 instance (AZ-a seed)
cd casssndra/dis
docker compose --env-file envs/aws-node1.env -f docker-compose.node.yml up -d
docker exec cassandra-1 nodetool status

# on the cassandra-4 instance (AZ-b seed)
docker compose --env-file envs/aws-node4.env -f docker-compose.node.yml up -d
docker exec cassandra-4 nodetool status      # wait for 2x UN, no UJ

# on cassandra-2, cassandra-3, cassandra-5 (any order)
docker compose --env-file envs/aws-nodeN.env -f docker-compose.node.yml up -d

# verify from any instance
docker exec cassandra-1 nodetool status      # target: 5x UN, 3 in rack1, 2 in rack2
```

--------------------------------------------------------------------
## 6. Load schema + data
--------------------------------------------------------------------
```
docker cp cassandra/schema.sql cassandra-1:/schema.sql
docker exec -it cassandra-1 cqlsh -f /schema.sql
CASSANDRA_HOST=<cassandra-1-private-ip> python cassandra/seed_data.py 200
```

--------------------------------------------------------------------
## 7. What to actually validate before trusting this on the airgapped cluster
--------------------------------------------------------------------
1. **Ring forms correctly**: `nodetool status` shows 5x `UN`, `Rack` column
   shows 3x rack1 / 2x rack2, `Owns` roughly balanced.
2. **Single-node loss**: `docker stop cassandra-2`. App reads/writes at
   `LOCAL_QUORUM` should keep working (2 of 3 replicas still up for every
   range). `docker start cassandra-2` and confirm it rejoins as `UN`
   without manual intervention.
3. **Rack-loss drill** (this is the one that actually matters for the
   2-rack design): stop all of rack2 (`cassandra-4`, `cassandra-5`).
   Expect some `LOCAL_QUORUM` operations to fail (ranges whose majority
   replica landed in rack2) and some to keep working. This tells you the
   real, not theoretical, blast radius of losing one of your two
   airgapped PCs — write it down.
4. **Node replacement**: `docker compose ... down -v` one non-seed node to
   simulate a dead disk, then follow the replace procedure in
   `../RECOVERY.md` and confirm it streams back to `UN` without a manual
   restore.
5. **Repair + backup**: run `scripts/repair.sh` and `scripts/backup.sh`
   against a live node, confirm both complete without error and the
   backup tarball is non-empty.
6. **App connectivity**: point `backend/.env` `CASSANDRA_HOSTS` at 2-3 of
   the private IPs (not just one — so the driver has a live contact point
   even if node 1 happens to be down when the app starts) and
   `CASSANDRA_LOCAL_DC=datacenter-1`, run `python serve.py`, hit `/health/`.

Only once all 6 pass should this same config get replicated onto the
airgapped `dis/envs/pcNNN.env` files.

--------------------------------------------------------------------
## 8. Tear down
--------------------------------------------------------------------
```
docker compose --env-file envs/aws-nodeN.env -f docker-compose.node.yml down -v   # per instance
```
Then terminate the 5 EC2 instances and delete the security group.
