"""Get workloads and pods from a live cluster or from saved JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

#: Everything the detector needs in one round trip.
RESOURCES = "deployments,statefulsets,daemonsets,pods"

DEFAULT_TIMEOUT = 60


class CollectError(Exception):
    """Input could not be read. Always a usage-level failure (exit 2)."""


def flatten(doc: Any) -> List[Dict[str, Any]]:
    """Pull Kubernetes objects out of any shape kubectl emits.

    `kubectl get` returns a single object, a typed List, or a `kind: List` of
    mixed kinds depending on the arguments, and `-o json` on several resources
    nests them. All three collapse to a flat list of objects here.
    """
    out: List[Dict[str, Any]] = []
    if isinstance(doc, list):
        for entry in doc:
            out.extend(flatten(entry))
        return out
    if not isinstance(doc, dict):
        return out
    if isinstance(doc.get("items"), list):
        for entry in doc["items"]:
            out.extend(flatten(entry))
        return out
    if doc.get("kind"):
        out.append(doc)
    return out


def load_files(paths: Sequence[str]) -> List[Dict[str, Any]]:
    """Read one or more JSON dumps. ``-`` reads stdin."""
    items: List[Dict[str, Any]] = []
    for path in paths:
        try:
            if path == "-":
                doc = json.load(sys.stdin)
            else:
                with open(path, "r", encoding="utf-8") as handle:
                    doc = json.load(handle)
        except OSError as exc:
            raise CollectError("cannot read %s: %s" % (path, exc))
        except ValueError as exc:
            raise CollectError("%s is not valid JSON: %s" % (path, exc))
        items.extend(flatten(doc))
    return items


def load_kubectl(
    namespace: Optional[str] = None,
    all_namespaces: bool = False,
    context: Optional[str] = None,
    kubectl: str = "kubectl",
    timeout: int = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Shell out to kubectl once and parse the result."""
    if shutil.which(kubectl) is None:
        raise CollectError(
            "%s not found in PATH; pass --from-file to run offline" % kubectl
        )
    cmd = [kubectl, "get", RESOURCES, "-o", "json"]
    if context:
        cmd += ["--context", context]
    if all_namespaces:
        cmd.append("--all-namespaces")
    elif namespace:
        cmd += ["--namespace", namespace]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CollectError("%s timed out after %ds" % (kubectl, timeout))
    except OSError as exc:
        raise CollectError("cannot run %s: %s" % (kubectl, exc))
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().replace("\n", " ")
        raise CollectError("%s failed: %s" % (" ".join(cmd), stderr[:400]))
    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        raise CollectError("%s returned invalid JSON: %s" % (kubectl, exc))
    return flatten(doc)


def filter_namespace(
    items: Sequence[Dict[str, Any]], namespace: Optional[str]
) -> List[Dict[str, Any]]:
    """Keep only objects in one namespace. Used for saved dumps."""
    if not namespace:
        return list(items)
    return [
        item
        for item in items
        if (((item.get("metadata") or {}).get("namespace")) or "default") == namespace
    ]
