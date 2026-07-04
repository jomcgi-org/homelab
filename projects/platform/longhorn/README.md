# Longhorn

Distributed block storage system for Kubernetes persistent volumes.

## Overview

Longhorn provides cloud-native distributed block storage with built-in replication, snapshots, and backups. It transforms locally-attached storage on Kubernetes nodes into a highly available distributed storage system.

```mermaid
flowchart TB
    subgraph "node-4 (single replica, homelab default)"
        N1[Replica]
    end

    subgraph Workload
        POD[Pod] --> PVC[PVC]
    end

    PVC --> N1
```

## Key Features

Longhorn as software supports the following; which of these are actually turned
on in this cluster is covered in Replica Configuration below (short version:
replication and S3 backup are not).

- **Distributed replication** - Data can be replicated across nodes for high availability
- **Automatic recovery** - Self-healing from node failures, rebuilds replicas on healthy nodes
- **Backup/restore** - S3-compatible backup targets for disaster recovery
- **Snapshots** - Point-in-time volume snapshots with instant restore
- **Volume resize** - Expand PVCs without downtime
- **ReadWriteMany** - RWX volumes via NFS for multi-pod access

## Replica Configuration

**homelab production default: 1 replica** (`values-prod.yaml` `defaultReplicaCount: 1`).
Because the cluster runs single-replica by default, most volumes have no
node-loss tolerance: there is no second copy to fail over to. No S3
`backupTarget` is configured either (see the Configuration table below), so
durability currently rests entirely on the single replica plus whatever
out-of-band export a given service does for its own data.

The one exception is GPU workload storage: `storageclass-gpu.yaml` defines a
real `longhorn-gpu` StorageClass, pinned to node-4 (`nodeSelector:
"kubernetes.io/hostname:node-4"`, `diskSelector: "nvme,gpu"`) with
`numberOfReplicas: "1"` and `dataLocality: "strict-local"`, used for
performance rather than availability since GPU workloads only ever run on
node-4 anyway.

For the full range of replication trade-offs Longhorn supports (1 vs. 2 vs. 3
replicas, rebuild behavior, node-loss tolerance), see the [upstream Longhorn
volumes-and-nodes docs](https://longhorn.io/docs/latest/volumes-and-nodes/).

## Storage Classes

The default `longhorn` StorageClass is created by the upstream chart with
`numberOfReplicas` set from `defaultSettings.defaultReplicaCount` (`1` in this
cluster, see above), not the Longhorn upstream default of `3`:

```bash
kubectl get storageclass longhorn -o yaml
```

The only custom StorageClass in this repo is `longhorn-gpu`
(`storageclass-gpu.yaml`), described above. For creating additional custom
StorageClasses (disk/node selectors, data locality, reclaim policy), see the
[upstream StorageClass parameters
reference](https://longhorn.io/docs/latest/references/storage-class-parameters/).

## Volume Operations

### Expand Volume

1. Edit PVC:

   ```bash
   kubectl patch pvc postgres-data -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
   ```

2. Verify expansion:
   ```bash
   kubectl get pvc postgres-data
   ```

No pod restart required (online expansion).

### Create Snapshot

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: postgres-snapshot-20260203
spec:
  volumeSnapshotClassName: longhorn
  source:
    persistentVolumeClaimName: postgres-data
```

### Clone Volume

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-clone
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  dataSource:
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
    name: postgres-snapshot-20260203
  resources:
    requests:
      storage: 20Gi
```

## Monitoring

### Check Volume Health

```bash
kubectl -n longhorn-system get volumes
```

**Volume states:**

- `attached` - Mounted to a pod
- `detached` - Not in use
- `degraded` - Missing replicas (rebuilding)
- `faulted` - Critical error, data may be lost

### Replica Status

```bash
kubectl -n longhorn-system get replicas
```

**Replica states:**

- `running` - Healthy
- `rebuilding` - Recovering from failure
- `error` - Failed, needs attention

### Check Backup Status

```bash
kubectl -n longhorn-system get backupvolumes
kubectl -n longhorn-system get backups
```

## Troubleshooting

### Volume Stuck in "Attaching"

**Symptom:** Pod pending, volume shows "Attaching" state

**Solution:**

```bash
# Check Longhorn manager logs
kubectl -n longhorn-system logs deploy/longhorn-manager

# Force detach and reattach
kubectl -n longhorn-system annotate volume/<volume-name> \
  longhorn.io/force-detach=true
```

### Replica Rebuild Stuck

**Symptom:** Volume degraded for extended period

**Solution:**

```bash
# Check instance-manager logs
kubectl -n longhorn-system logs deploy/instance-manager-<node>

# Delete stuck replica
kubectl -n longhorn-system delete replica <replica-name>
```

### Out of Space

**Symptom:** Cannot create new volumes, "insufficient storage" error

**Solution:**

1. Check node storage:

   ```bash
   kubectl -n longhorn-system get nodes -o wide
   ```

2. Increase node disk size or add new nodes

3. Clean up old backups/snapshots:
   ```bash
   kubectl -n longhorn-system delete backups --all
   ```

## Configuration

| Value                                               | Description                 | Default  |
| --------------------------------------------------- | --------------------------- | -------- |
| `defaultSettings.backupTarget`                      | S3 bucket URL for backups   | `""`     |
| `defaultSettings.defaultReplicaCount`               | Default replica count       | `1`      |
| `defaultSettings.storageMinimalAvailablePercentage` | Min free space %            | `25`     |
| `defaultSettings.upgradeChecker`                    | Check for updates           | `true`   |
| `persistence.defaultClass`                          | Set as default StorageClass | `true`   |
| `persistence.reclaimPolicy`                         | PV reclaim policy           | `Delete` |

Full configuration: See [longhorn chart values](https://github.com/longhorn/charts/tree/master/charts/longhorn)

## Access UI

The authoritative route is the path-based private ingress:
https://private.jomcgi.dev/app/longhorn (see `templates/httproute.yaml`). A
former `longhorn.jomcgi.dev` tunnel route was retired in PR #2534.

For local access without going through the ingress:

```bash
kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80
```

Navigate to http://localhost:8080

**UI Features:**

- Volume management
- Backup/restore
- Node/disk management
- Recurring job configuration
- Event logs

## Related Documentation

- [Longhorn Official Docs](https://longhorn.io/docs/)
- [Backup and Restore](https://longhorn.io/docs/latest/snapshots-and-backups/backup-and-restore/)
- [Volume Snapshots](https://longhorn.io/docs/latest/snapshots-and-backups/csi-snapshot-support/)
