"""Input handling: JSON shapes, kubectl invocation, failure paths."""

import json
import os
import stat

import pytest

from rollout_stall.collect import (
    CollectError,
    filter_namespace,
    flatten,
    load_files,
    load_kubectl,
)

from conftest import fixture_path


def test_flatten_handles_a_typed_list():
    doc = {"kind": "DeploymentList", "items": [{"kind": "Deployment"}, {"kind": "Deployment"}]}
    assert len(flatten(doc)) == 2


def test_flatten_handles_a_single_object():
    assert flatten({"kind": "Pod", "metadata": {}}) == [{"kind": "Pod", "metadata": {}}]


def test_flatten_handles_a_bare_array_and_nested_lists():
    doc = [
        {"kind": "List", "items": [{"kind": "Pod"}]},
        {"kind": "Deployment"},
    ]
    assert [o["kind"] for o in flatten(doc)] == ["Pod", "Deployment"]


def test_flatten_ignores_junk():
    assert flatten({}) == []
    assert flatten(None) == []
    assert flatten("string") == []
    assert flatten({"items": []}) == []


def test_load_files_merges_dumps():
    items = load_files(
        [fixture_path("deploy-unschedulable.json"), fixture_path("deploy-imagepull.json")]
    )
    kinds = {o["kind"] for o in items}
    assert kinds == {"Deployment", "Pod"}
    assert len(items) == 9


def test_load_files_reports_a_missing_path():
    with pytest.raises(CollectError) as excinfo:
        load_files(["/nope/missing.json"])
    assert "cannot read" in str(excinfo.value)


def test_load_files_reports_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("<html>nope</html>", encoding="utf-8")
    with pytest.raises(CollectError):
        load_files([str(bad)])


def test_filter_namespace_defaults_missing_namespace_to_default():
    items = [
        {"kind": "Pod", "metadata": {"name": "a", "namespace": "payments"}},
        {"kind": "Pod", "metadata": {"name": "b"}},
    ]
    assert [o["metadata"]["name"] for o in filter_namespace(items, "payments")] == ["a"]
    assert [o["metadata"]["name"] for o in filter_namespace(items, "default")] == ["b"]
    assert len(filter_namespace(items, None)) == 2


def fake_kubectl(tmp_path, stdout="", stderr="", code=0):
    """Write a kubectl stand-in that records its argv and replays canned output."""
    argv_log = tmp_path / "argv.txt"
    script = tmp_path / "kubectl"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" > "{log}"\n'
        "cat <<'STDOUT'\n{stdout}\nSTDOUT\n"
        ">&2 printf '%s' '{stderr}'\n"
        "exit {code}\n".format(log=argv_log, stdout=stdout, stderr=stderr, code=code),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script, argv_log


def test_load_kubectl_parses_output_and_passes_scope(tmp_path, monkeypatch):
    with open(fixture_path("deploy-unschedulable.json"), "r", encoding="utf-8") as handle:
        payload = json.dumps(json.load(handle))
    script, argv_log = fake_kubectl(tmp_path, stdout=payload)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    items = load_kubectl(all_namespaces=True, context="prod", kubectl=str(script))

    assert len(items) == 5
    recorded = argv_log.read_text(encoding="utf-8")
    assert "get deployments,statefulsets,daemonsets,pods -o json" in recorded
    assert "--context prod" in recorded
    assert "--all-namespaces" in recorded


def test_load_kubectl_passes_a_single_namespace(tmp_path, monkeypatch):
    script, argv_log = fake_kubectl(tmp_path, stdout='{"kind":"List","items":[]}')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    assert load_kubectl(namespace="payments", kubectl=str(script)) == []
    recorded = argv_log.read_text(encoding="utf-8")
    assert "--namespace payments" in recorded
    assert "--all-namespaces" not in recorded


def test_load_kubectl_surfaces_a_failure(tmp_path, monkeypatch):
    script, _ = fake_kubectl(
        tmp_path, stderr="error: You must be logged in to the server", code=1
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(CollectError) as excinfo:
        load_kubectl(kubectl=str(script))
    assert "must be logged in" in str(excinfo.value)


def test_load_kubectl_surfaces_non_json_output(tmp_path, monkeypatch):
    script, _ = fake_kubectl(tmp_path, stdout="No resources found")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(CollectError) as excinfo:
        load_kubectl(kubectl=str(script))
    assert "invalid JSON" in str(excinfo.value)


def test_missing_kubectl_tells_you_about_from_file():
    with pytest.raises(CollectError) as excinfo:
        load_kubectl(kubectl="kubectl-that-does-not-exist")
    assert "--from-file" in str(excinfo.value)
