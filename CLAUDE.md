# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Operator Does

A Kubernetes controller that performs **"make-before-break" rolling restarts** for single-replica deployments running when node is cordoned.

When a matching node is detected, it walks the pod → ReplicaSet → Deployment ownership chain and patches the deployment's `kubectl.kubernetes.io/restartedAt` annotation — **but only for deployments with exactly 1 replica** (higher-replica deployments handle disruption via normal rolling updates).

## Run locally with kubeconfig (docker compose)

```bash
docker compose up app
```

## Dependencies

Defined in `requirements.txt`

## Configuration

When changing app configuration, update docker compose.yaml, .env.example, and README.md accordingly.

## Dry run

When dry run is enabled `DRY_RUN=1`, the app does not perform any actions but only logs what it would perform.

## Helm Chart

Located in `charts/app/`. Uses [`idp-app`](https://github.com/matuszeman/charts/tree/main/charts/idp-app) as a subchart (aliased as `app`).

RBAC templates are in `charts/app/templates/`:
- `clusterrole.yaml` — ClusterRole with required permissions (cluster-scoped because nodes are cluster-scoped and pods/deployments span all namespaces)
- `clusterrolebinding.yaml` — binds the ClusterRole to the ServiceAccount managed by `idp-app`

The ServiceAccount is controlled by `idp-app`:
- `app.serviceAccount.create: true` to have `idp-app` create it (default: `false`)
- `app.serviceAccount.name` to override the name (default: `idp-app.fullname` = release name)

## Kubernetes RBAC Required

The service account running this operator needs:
- `get`, `list`, `watch` on `nodes` (core)
- `list` on `pods` across all namespaces (core)
- `get` on `replicasets` (apps)
- `get`, `patch` on `deployments` (apps)

## Design Decisions

- **Single-replica only**: Multi-replica deployments survive node disruption naturally via Kubernetes scheduling. This operator only acts on `replicas == 1` to avoid unnecessary restarts.
- **Deduplication per event**: `processed_deployments` set prevents triggering multiple rollouts for the same deployment if multiple pods land on the same disrupted node.
- **Watch stream**: Uses the Kubernetes watch API (long-polling) rather than polling — events are processed as they arrive.
