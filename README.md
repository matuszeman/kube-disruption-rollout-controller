# kube-disruption-rollout-controller

A Kubernetes controller that performs **make-before-break rolling restarts** for single-replica deployments when their node is disrupted.

## How It Works

When a node is detected as disrupted (cordoned, tainted, or via a matching Kubernetes Event), the controller:

1. Lists all pods running on that node
2. Walks the pod → ReplicaSet → Deployment ownership chain
3. Patches `kubectl.kubernetes.io/restartedAt` on the deployment to trigger a rolling restart

Only deployments with **exactly 1 replica** are restarted — multi-replica deployments survive node disruption naturally via Kubernetes scheduling.

Node changes and Kubernetes Events are consumed via the Kubernetes **watch API** (long-polling), so reactions are immediate rather than periodic.

## Deployment Requirements

For make-before-break to work end-to-end, each target deployment must be configured with a rolling update strategy, a readiness probe, and a PodDisruptionBudget.

### Rolling Update Strategy

The deployment must use `RollingUpdate` with `maxUnavailable: 0` and `maxSurge: 1`. This ensures Kubernetes schedules and waits for the new pod to be ready *before* terminating the old one.

Without this, Kubernetes may terminate the old pod first (the default `maxUnavailable: 1` allows it), defeating the make-before-break guarantee.

```yaml
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
```

### PodDisruptionBudget

A PDB with `minAvailable: 1` acts as a safety net: if the rolling update hasn't completed before node drain or eviction begins, the PDB blocks eviction until the replacement pod is running on a healthy node.

Without a PDB, there is a race between the controller's rollout and the node drain — the pod could be evicted before the new one is ready.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: my-app-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: my-app
```

### Readiness Probe

A readiness probe is critical for `maxUnavailable: 0` to be meaningful. Kubernetes considers a pod "ready" only when its readiness probe passes — without one, the new pod is immediately marked ready regardless of whether the application is actually serving traffic, and the old pod is terminated too early.

```yaml
spec:
  containers:
    - name: my-app
      readinessProbe:
        httpGet:
          path: /healthz
          port: 8080
        initialDelaySeconds: 5
        periodSeconds: 5
```

## Configuration

### Detecting Disruption

At least one disruption detection method must be configured.

**Option A — Cordoned/unschedulable nodes**

Triggers on any node that becomes unschedulable (e.g. `kubectl cordon`, drain, or any tool that sets `spec.unschedulable=true`).

```
NODE_DISRUPTION_CORDONED=1
```

**Option B — Disruption taints (Karpenter)**

Triggers when a node has one or more of the specified taint keys. Karpenter adds a taint before terminating a node, making this the preferred method for Karpenter-managed clusters.

```
NODE_DISRUPTION_TAINTS=karpenter.sh/disrupted
```

Multiple taint keys can be specified as a comma-separated list:

```
NODE_DISRUPTION_TAINTS=karpenter.sh/disruption,node.kubernetes.io/unschedulable
```

**Option C — Kubernetes Events (Karpenter `DisruptionBlocked`)**

Triggers when a Kubernetes `Event` targeting a node matches one of the configured reasons. Useful for Karpenter clusters where the controller emits a `DisruptionBlocked` event when it cannot drain a node because a single-replica pod without a PDB is blocking disruption. Reacting to this event moves the pod proactively, unblocking Karpenter's disruption flow.

```
NODE_EVENT_REASONS=DisruptionBlocked
```

Multiple reasons can be specified as a comma-separated list:

```
NODE_EVENT_REASONS=DisruptionBlocked,DisruptionDenied
```

**Option C — Node Event Taint**

When using `NODE_EVENT_REASONS`, optionally apply a taint to the matched node at the time the rollout is triggered. This prevents new pods from being scheduled on the node during the rollout, and can also integrate with `NODE_DISRUPTION_TAINTS` if the same taint key is configured there.

```
NODE_EVENT_TAINT=karpenter.sh/disrupted:NoSchedule
```

To remove the taint after the rollout is triggered:

```
NODE_EVENT_TAINT_REMOVE=1
```

Both settings are ignored unless `NODE_EVENT_REASONS` is set.

**Option D — Combined**

All options can be set simultaneously — any matching condition triggers a rollout.

```
NODE_DISRUPTION_CORDONED=1
NODE_DISRUPTION_TAINTS=karpenter.sh/disrupted
NODE_EVENT_REASONS=DisruptionBlocked
```

### Karpenter

For Karpenter-managed node pools, configure the controller to watch for the disruption taint and optionally scope it to spot nodes:

```
NODE_DISRUPTION_TAINTS=karpenter.sh/disrupted
NODE_LABEL_SELECTOR=karpenter.sh/capacity-type=spot
```

Karpenter (v1) marks nodes for disruption by adding a taint with key `karpenter.sh/disrupted:NoSchedule` before the node is terminated.

### Node Scoping

Restrict monitoring to a subset of nodes using a label selector:

```
NODE_LABEL_SELECTOR=karpenter.sh/capacity-type=spot
```

Standard Kubernetes label selector syntax — multiple labels are comma-separated (`key1=val1,key2=val2`).

### Pod Filtering

Restrict which pods are eligible for rollout:

```bash
# Only restart pods with these labels
POD_LABEL_SELECTOR=app=myapp,env=production

# Only restart pods with these annotations
POD_ANNOTATION_SELECTOR=disruption-rollout=true
```

Both filters can be combined — a pod must satisfy both to be restarted.

### Namespace Scoping

Restrict rollouts to specific namespaces:

```
ALLOWED_NAMESPACES=default,production
```

## Local Development

**Prerequisites:** Docker, a kubeconfig with access to a cluster.

1. Copy the example env file and configure your cluster access:

   ```bash
   cp .env.example .env
   # Edit .env: set KUBECONFIG_OVERRIDE or ensure KUBECONFIG is set in your shell
   # Set KUBE_CONTEXT to the cluster context you want to use
   ```

2. Start with Docker Compose:

   ```bash
   docker compose up app
   ```

   The app source is mounted as a volume — changes to `app/` take effect on restart.

3. **Direct Python** (alternative):

   ```bash
   pip install -r app/requirements.txt
   # Export env vars, then:
   python app/main.py
   ```

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `NODE_DISRUPTION_CORDONED` | one of these three | — | Set to `1` to treat unschedulable (cordoned) nodes as disrupted |
| `NODE_DISRUPTION_TAINTS` | one of these three | — | Comma-separated taint keys that signal node disruption |
| `NODE_EVENT_REASONS` | one of these three | — | Comma-separated Kubernetes Event reasons to react to (e.g. `DisruptionBlocked`) |
| `NODE_EVENT_TAINT` | No | — | Taint to apply to the node on event match. Format: `key:Effect` or `key=value:Effect` (e.g. `karpenter.sh/disrupted:NoSchedule`) |
| `NODE_EVENT_TAINT_REMOVE` | No | `0` | Set to `1` to remove the taint after the rollout is triggered |
| `NODE_LABEL_SELECTOR` | No | — | Kubernetes label selector to scope which nodes are watched |
| `POD_LABEL_SELECTOR` | No | — | Label selector to filter which pods trigger rollouts |
| `POD_ANNOTATION_SELECTOR` | No | — | Annotation selector (`key=value,...`) to filter pods |
| `ALLOWED_NAMESPACES` | No | — | Comma-separated list of namespaces; restricts rollouts to these only |
| `DRY_RUN` | No | `0` | Set to `1` to log what would happen without patching deployments |
| `LOG_LEVEL` | No | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | No | `logfmt` | Log format (`logfmt` or `json`) |
| `KUBECONFIG` | No | — | Path to kubeconfig file (for local development) |
| `KUBE_CONTEXT` | No | — | Kubeconfig context to use (for local development) |
