# kube-disruption-rollout-controller

A Kubernetes controller that performs **make-before-break rolling restarts** for single-replica deployments when their node is disrupted.

## How It Works

When a node is detected as disrupted (cordoned or tainted), the controller:

1. Lists all pods running on that node
2. Walks the pod → ReplicaSet → Deployment ownership chain
3. Patches `kubectl.kubernetes.io/restartedAt` on the deployment to trigger a rolling restart

Only deployments with **exactly 1 replica** are restarted — multi-replica deployments survive node disruption naturally via Kubernetes scheduling.

Node events are consumed via the Kubernetes **watch API** (long-polling), so reactions are immediate rather than periodic.

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
NODE_DISRUPTION_TAINTS=karpenter.sh/disruption
```

Multiple taint keys can be specified as a comma-separated list:

```
NODE_DISRUPTION_TAINTS=karpenter.sh/disruption,node.kubernetes.io/unschedulable
```

**Option C — Combined**

Both can be set simultaneously — any matching condition triggers a rollout.

```
NODE_DISRUPTION_CORDONED=1
NODE_DISRUPTION_TAINTS=karpenter.sh/disruption
```

### Karpenter

For Karpenter-managed node pools, configure the controller to watch for the disruption taint and optionally scope it to spot nodes:

```
NODE_DISRUPTION_TAINTS=karpenter.sh/disruption
NODE_LABEL_SELECTOR=karpenter.sh/capacity-type=spot
```

Karpenter (v1) marks nodes for disruption by adding a taint with key `karpenter.sh/disruption` and value `disrupting` before the node is terminated.

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
| `NODE_DISRUPTION_CORDONED` | one of these two | — | Set to `1` to treat unschedulable (cordoned) nodes as disrupted |
| `NODE_DISRUPTION_TAINTS` | one of these two | — | Comma-separated taint keys that signal node disruption |
| `NODE_LABEL_SELECTOR` | No | — | Kubernetes label selector to scope which nodes are watched |
| `POD_LABEL_SELECTOR` | No | — | Label selector to filter which pods trigger rollouts |
| `POD_ANNOTATION_SELECTOR` | No | — | Annotation selector (`key=value,...`) to filter pods |
| `ALLOWED_NAMESPACES` | No | — | Comma-separated list of namespaces; restricts rollouts to these only |
| `DRY_RUN` | No | `0` | Set to `1` to log what would happen without patching deployments |
| `LOG_LEVEL` | No | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | No | `logfmt` | Log format (`logfmt` or `json`) |
| `KUBECONFIG` | No | — | Path to kubeconfig file (for local development) |
| `KUBE_CONTEXT` | No | — | Kubeconfig context to use (for local development) |
