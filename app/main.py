import os
import sys
import signal
import datetime
import logging
import json
import threading
from kubernetes import client, config, watch

_BUILTIN_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
        }
        data.update({k: v for k, v in record.__dict__.items() if k not in _BUILTIN_FIELDS})
        data["msg"] = record.getMessage()
        return json.dumps(data, default=str)


class LogfmtFormatter(logging.Formatter):
    def format(self, record):
        fields = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
        }
        fields.update({k: v for k, v in record.__dict__.items() if k not in _BUILTIN_FIELDS})
        fields["msg"] = record.getMessage()
        parts = []
        for k, v in fields.items():
            sv = str(v).replace('\n', '\\n').replace('\r', '')
            if any(c in sv for c in (' ', '"', '=')):
                sv = '"' + sv.replace('"', '\\"') + '"'
            parts.append(f"{k}={sv}")
        return " ".join(parts)


def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "logfmt").lower()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else LogfmtFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    for noisy in ("urllib3", "kubernetes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger(__name__)


def parse_taint(taint_str):
    """Parse 'key:Effect' or 'key=value:Effect' into (key, value, effect)."""
    if ":" not in taint_str:
        raise ValueError(f"missing ':Effect' in taint '{taint_str}'")
    key_part, effect = taint_str.rsplit(":", 1)
    if not effect:
        raise ValueError(f"empty effect in taint '{taint_str}'")
    if "=" in key_part:
        key, value = key_part.split("=", 1)
    else:
        key, value = key_part, None
    if not key:
        raise ValueError(f"empty key in taint '{taint_str}'")
    return key, value, effect


def apply_node_taint(v1, node_name, taint_key, taint_value, taint_effect, dry_run=False):
    ctx = {"event": "node_taint_apply", "node": node_name, "taint_key": taint_key, "taint_effect": taint_effect}
    if taint_value:
        ctx["taint_value"] = taint_value
    if dry_run:
        log.info("dry run: would apply node taint", extra={**ctx, "dry_run": True})
        return
    log.info("applying node taint", extra=ctx)
    taint = {"key": taint_key, "effect": taint_effect}
    if taint_value:
        taint["value"] = taint_value
    body = {"spec": {"taints": [taint]}}
    try:
        v1.patch_node(node_name, body)
    except Exception as e:
        log.error("patch node taint failed", extra={**ctx, "event": "patch_error", "error": str(e)})


def remove_node_taint(v1, node_name, taint_key, dry_run=False):
    ctx = {"event": "node_taint_remove", "node": node_name, "taint_key": taint_key}
    if dry_run:
        log.info("dry run: would remove node taint", extra={**ctx, "dry_run": True})
        return
    log.info("removing node taint", extra=ctx)
    try:
        node = v1.read_node(node_name)
        new_taints = [t for t in (node.spec.taints or []) if t.key != taint_key]
        v1.patch_node(node_name, {"spec": {"taints": new_taints}})
    except Exception as e:
        log.error("remove node taint failed", extra={**ctx, "event": "patch_error", "error": str(e)})


def parse_annotation_selector(selector):
    if not selector:
        return None
    result = {}
    for pair in selector.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    return result or None


def node_disruption_taint_matches(node, taint_keys):
    if not taint_keys or not node.spec.taints:
        return set()
    node_taint_keys = {t.key for t in node.spec.taints}
    return node_taint_keys & taint_keys


def trigger_rollout(apps_v1, namespace, deployment_name, dry_run=False):
    ctx = {"event": "rollout", "namespace": namespace, "deployment": deployment_name}
    if dry_run:
        log.info("dry run: would trigger rollout", extra={**ctx, "dry_run": True})
        return
    log.info("triggering rollout", extra=ctx)
    now = datetime.datetime.now().isoformat()
    body = {
        'spec': {
            'template': {
                'metadata': {
                    'annotations': {
                        'kubectl.kubernetes.io/restartedAt': now
                    }
                }
            }
        }
    }
    try:
        apps_v1.patch_namespaced_deployment(deployment_name, namespace, body)
    except Exception as e:
        log.error("patch deployment failed", extra={**ctx, "event": "patch_error", "error": str(e)})


def process_disrupted_node(v1, apps_v1, node_name, disruption_reason, processed_nodes, lock,
                            pod_label_selector, pod_annotation_selector,
                            allowed_namespaces, dry_run, pod_sel_ctx,
                            node_event_taint=None, node_event_taint_remove=False):
    with lock:
        if node_name in processed_nodes:
            log.debug("node already processed", extra={"event": "skip_node", "node": node_name})
            return False
        processed_nodes.add(node_name)

    log.info("target node disrupted", extra={"event": "node_disrupted", "node": node_name, "disruption_reason": disruption_reason})

    pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}", label_selector=pod_label_selector).items
    log.debug("pods found", extra={"event": "pods_found", "node": node_name, "count": len(pods), **pod_sel_ctx})

    eligible_pods = []
    for pod in pods:
        pod_ctx = {"pod": pod.metadata.name, "namespace": pod.metadata.namespace}

        if not pod.metadata.owner_references:
            log.debug("skip pod: no owner references", extra={"event": "skip_pod", "reason": "no_owner_references", **pod_ctx, **pod_sel_ctx})
            continue

        if allowed_namespaces and pod.metadata.namespace not in allowed_namespaces:
            log.debug("skip pod: namespace not allowed", extra={"event": "skip_pod", "reason": "namespace_not_allowed", **pod_ctx, **pod_sel_ctx})
            continue

        if pod_annotation_selector:
            annotations = pod.metadata.annotations or {}
            mismatched = {k: annotations.get(k) for k, v in pod_annotation_selector.items() if annotations.get(k) != v}
            if mismatched:
                log.debug("skip pod: annotation mismatch", extra={"event": "skip_pod", "reason": "annotation_mismatch", "mismatched": str(mismatched), **pod_ctx, **pod_sel_ctx})
                continue

        eligible_pods.append(pod)

    if node_event_taint and eligible_pods:
        key, value, effect = node_event_taint
        apply_node_taint(v1, node_name, key, value, effect, dry_run=dry_run)

    processed_deployments = set()

    for pod in eligible_pods:
        pod_ctx = {"pod": pod.metadata.name, "namespace": pod.metadata.namespace}

        owner = pod.metadata.owner_references[0]
        if owner.kind != "ReplicaSet":
            log.debug("skip pod: owner is not a replicaset", extra={"event": "skip_pod", "reason": "owner_not_replicaset", "owner_kind": owner.kind, **pod_ctx, **pod_sel_ctx})
            continue

        try:
            rs = apps_v1.read_namespaced_replica_set(owner.name, pod.metadata.namespace)
            if not rs.metadata.owner_references:
                log.debug("skip pod: replicaset has no owner", extra={"event": "skip_pod", "reason": "rs_no_owner", "rs": owner.name, **pod_ctx, **pod_sel_ctx})
                continue

            deploy_name = rs.metadata.owner_references[0].name
            deploy_key = f"{pod.metadata.namespace}/{deploy_name}"
            ns = pod.metadata.namespace

            log.debug("rs resolved", extra={"event": "rs_resolved", "rs": owner.name, "namespace": ns, "deployment": deploy_name, **pod_ctx, **pod_sel_ctx})

            if deploy_key not in processed_deployments:
                deploy = apps_v1.read_namespaced_deployment(deploy_name, ns)
                if deploy.spec.replicas == 1:
                    trigger_rollout(apps_v1, ns, deploy_name, dry_run=dry_run)
                    processed_deployments.add(deploy_key)
                else:
                    log.debug("skip deployment: multi-replica", extra={"event": "skip_deployment", "reason": "multi_replica", "namespace": ns, "deployment": deploy_name, "replicas": deploy.spec.replicas})
            else:
                log.debug("skip deployment: already processed", extra={"event": "skip_deployment", "reason": "already_processed", "namespace": ns, "deployment": deploy_name})
        except Exception as e:
            log.warning("could not process pod", extra={"event": "pod_error", **pod_ctx, "error": str(e)})

    if node_event_taint and node_event_taint_remove and eligible_pods:
        key, value, effect = node_event_taint
        remove_node_taint(v1, node_name, key, dry_run=dry_run)

    return True


def watch_node_events(v1, apps_v1, node_event_reasons, processed_nodes, lock,
                      pod_label_selector, pod_annotation_selector,
                      allowed_namespaces, dry_run, pod_sel_ctx, w,
                      node_event_taint=None, node_event_taint_remove=False):
    for event in w.stream(v1.list_event_for_all_namespaces,
                          field_selector="involvedObject.kind=Node"):
        kube_event = event['object']
        reason = kube_event.reason
        node_name = kube_event.involved_object.name
        if reason in node_event_reasons:
            log.info("node event matched", extra={"event": "node_event_match", "node": node_name, "reason": reason})
            process_disrupted_node(v1, apps_v1, node_name, f"event:{reason}", processed_nodes, lock,
                                   pod_label_selector, pod_annotation_selector,
                                   allowed_namespaces, dry_run, pod_sel_ctx,
                                   node_event_taint=node_event_taint,
                                   node_event_taint_remove=node_event_taint_remove)


def monitor_nodes():
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    node_label_selector = os.environ.get("NODE_LABEL_SELECTOR") or None
    pod_label_selector = os.environ.get("POD_LABEL_SELECTOR") or None
    pod_annotation_selector = parse_annotation_selector(os.environ.get("POD_ANNOTATION_SELECTOR"))
    allowed_namespaces = set(ns.strip() for ns in os.environ["ALLOWED_NAMESPACES"].split(",") if ns.strip()) if os.environ.get("ALLOWED_NAMESPACES") else None
    disruption_taints = set(k.strip() for k in os.environ["NODE_DISRUPTION_TAINTS"].split(",") if k.strip()) if os.environ.get("NODE_DISRUPTION_TAINTS") else set()
    disruption_cordoned = os.environ.get("NODE_DISRUPTION_CORDONED", "").strip() in ("1", "true")
    node_event_reasons = set(r.strip() for r in os.environ["NODE_EVENT_REASONS"].split(",") if r.strip()) if os.environ.get("NODE_EVENT_REASONS") else set()
    node_event_taint_raw = os.environ.get("NODE_EVENT_TAINT")
    try:
        node_event_taint = parse_taint(node_event_taint_raw) if node_event_taint_raw else None
    except ValueError as e:
        log.error("invalid NODE_EVENT_TAINT", extra={"event": "config_error", "error": str(e)})
        sys.exit(1)
    node_event_taint_remove = os.environ.get("NODE_EVENT_TAINT_REMOVE", "0").strip() in ("1", "true")

    if not disruption_taints and not disruption_cordoned and not node_event_reasons:
        log.error("at least one of NODE_DISRUPTION_TAINTS, NODE_DISRUPTION_CORDONED, or NODE_EVENT_REASONS must be set", extra={"event": "config_error"})
        sys.exit(1)

    try:
        config.load_incluster_config()
    except config.ConfigException as e:
        log.warning("in-cluster config unavailable, using kubeconfig", extra={"event": "config_fallback", "error": str(e)})
        try:
            config.load_kube_config(context=os.environ.get("KUBE_CONTEXT") or os.environ.get("KUBE_CTX") or None)
        except Exception as e:
            log.error("failed to load kubeconfig", extra={"event": "config_error", "error": str(e)})
            sys.exit(1)

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    w = watch.Watch()
    w_events = watch.Watch()

    def _shutdown(signum, frame):
        log.info("shutting down", extra={"event": "shutdown"})
        w.stop()
        w_events.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("starting", extra={
        "event": "start",
        "dry_run": dry_run,
        "node_label_selector": node_label_selector,
        "pod_label_selector": pod_label_selector,
        "pod_annotation_selector": os.environ.get("POD_ANNOTATION_SELECTOR") or None,
        "allowed_namespaces": ",".join(sorted(allowed_namespaces)) if allowed_namespaces else None,
        "node_disruption_taints": ",".join(sorted(disruption_taints)) or None,
        "node_disruption_cordoned": disruption_cordoned,
        "node_event_reasons": ",".join(sorted(node_event_reasons)) or None,
        "node_event_taint": node_event_taint_raw or None,
        "node_event_taint_remove": node_event_taint_remove,
    })

    node_sel_ctx = {"node_label_selector": node_label_selector} if node_label_selector else {}
    pod_sel_ctx = {
        **({"pod_label_selector": pod_label_selector} if pod_label_selector else {}),
        **({"pod_annotation_selector": os.environ.get("POD_ANNOTATION_SELECTOR")} if pod_annotation_selector else {}),
    }

    processed_nodes = set()
    lock = threading.Lock()

    if node_event_reasons:
        t = threading.Thread(
            target=watch_node_events,
            args=(v1, apps_v1, node_event_reasons, processed_nodes, lock,
                  pod_label_selector, pod_annotation_selector,
                  allowed_namespaces, dry_run, pod_sel_ctx, w_events),
            kwargs={"node_event_taint": node_event_taint, "node_event_taint_remove": node_event_taint_remove},
            daemon=True,
        )
        t.start()

    if disruption_taints or disruption_cordoned:
        for event in w.stream(v1.list_node, label_selector=node_label_selector):
            node = event['object']
            node_name = node.metadata.name
            event_type = event['type']

            log.debug("node event", extra={"event": "node_event", "node": node_name, "type": event_type, **node_sel_ctx})

            if event_type == "DELETED":
                with lock:
                    if node_name in processed_nodes:
                        processed_nodes.discard(node_name)
                log.debug("node deleted, cleared", extra={"event": "node_cleared", "node": node_name, **node_sel_ctx})

            if event_type == "MODIFIED":
                cordoned = disruption_cordoned and (node.spec.unschedulable or False)
                matched_taints = node_disruption_taint_matches(node, disruption_taints)
                is_disrupted = cordoned or bool(matched_taints)

                disruption_reasons = ([" cordoned"] if cordoned else []) + ([f"taint:{k}" for k in sorted(matched_taints)])
                disruption_reason = ",".join(disruption_reasons) if disruption_reasons else None

                log.debug("node check", extra={"event": "node_check", "node": node_name, "disrupted": is_disrupted, "disruption_reason": disruption_reason, **node_sel_ctx})

                if not is_disrupted:
                    with lock:
                        processed_nodes.discard(node_name)
                    log.debug("node uncordoned, cleared", extra={"event": "node_cleared", "node": node_name, **node_sel_ctx})

                if is_disrupted:
                    process_disrupted_node(v1, apps_v1, node_name, disruption_reason, processed_nodes, lock,
                                           pod_label_selector, pod_annotation_selector,
                                           allowed_namespaces, dry_run, pod_sel_ctx)


if __name__ == "__main__":
    setup_logging()
    try:
        monitor_nodes()
    except Exception as e:
        log.error("unexpected error", extra={"event": "fatal", "error": str(e)})
        sys.exit(1)
