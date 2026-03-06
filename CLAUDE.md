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
