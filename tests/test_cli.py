"""End-to-end behaviour of the command line: output shape and exit codes."""

import json

import pytest

from rollout_stall.cli import EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, build_parser, main

from conftest import fixture_path

FLAGS = (
    "--from-file",
    "--namespace",
    "--all-namespaces",
    "--min-age",
    "--json",
    "--wide",
    "--quiet",
    "--now",
    "--kubectl",
    "--context",
    "--version",
)

NOW = ["--now", "2026-11-23T12:00:00Z"]


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_stalled_dump_exits_one_and_prints_a_table(capsys):
    code, out, _ = run(capsys, "--from-file", fixture_path("deploy-unschedulable.json"), *NOW)
    assert code == EXIT_FINDINGS
    assert out.splitlines()[0].split() == [
        "NAMESPACE",
        "KIND",
        "NAME",
        "DESIRED",
        "UPDATED",
        "READY",
        "AGE",
        "REASON",
        "DETAIL",
    ]
    assert "checkout-api" in out
    assert "Unschedulable" in out
    assert "22m" in out
    assert "1 stalled rollout (Deployment 1)" in out


def test_clean_dump_exits_zero(capsys):
    code, out, _ = run(capsys, "--from-file", fixture_path("healthy.json"), *NOW)
    assert code == EXIT_CLEAN
    assert "No stalled rollouts found (3 workloads checked)." in out


def test_json_is_one_object_per_stalled_workload(capsys):
    code, out, _ = run(
        capsys, "--from-file", fixture_path("ds-stalled.json"), "--json", *NOW
    )
    assert code == EXIT_FINDINGS
    payload = json.loads(out)
    assert isinstance(payload, list) and len(payload) == 1
    finding = payload[0]
    assert finding["kind"] == "DaemonSet"
    assert finding["target"] == "daemonset/log-shipper"
    assert finding["reason"] == "CrashLoopBackOff"
    assert finding["desiredReplicas"] == 4
    assert finding["readyReplicas"] == 3
    assert finding["rolloutAge"].endswith("m")
    assert finding["pods"][0]["restarts"] == 5


def test_json_on_a_clean_dump_is_an_empty_array(capsys):
    code, out, _ = run(capsys, "--from-file", fixture_path("healthy.json"), "--json", *NOW)
    assert code == EXIT_CLEAN
    assert json.loads(out) == []


def test_several_files_are_merged(capsys):
    code, out, _ = run(
        capsys,
        "--from-file",
        fixture_path("deploy-unschedulable.json"),
        "--from-file",
        fixture_path("deploy-imagepull.json"),
        *NOW,
    )
    assert code == EXIT_FINDINGS
    assert "checkout-api" in out and "billing-api" in out
    assert "2 stalled rollouts (Deployment 2)" in out


def test_namespace_filters_a_saved_dump(capsys):
    code, out, _ = run(
        capsys,
        "--from-file",
        fixture_path("deploy-unschedulable.json"),
        "--from-file",
        fixture_path("deploy-imagepull.json"),
        "-n",
        "billing",
        *NOW,
    )
    assert code == EXIT_FINDINGS
    assert "billing-api" in out
    assert "checkout-api" not in out


def test_min_age_can_silence_a_young_rollout(capsys):
    code, out, _ = run(
        capsys,
        "--from-file",
        fixture_path("deploy-unschedulable.json"),
        "--min-age",
        "3600",
        *NOW,
    )
    assert code == EXIT_CLEAN
    assert "No stalled rollouts" in out


def test_wide_keeps_the_whole_scheduler_message(capsys):
    _, narrow, _ = run(capsys, "--from-file", fixture_path("deploy-unschedulable.json"), *NOW)
    _, wide, _ = run(
        capsys, "--from-file", fixture_path("deploy-unschedulable.json"), "--wide", *NOW
    )
    assert "..." in narrow
    assert "Preemption is not helpful for scheduling." in wide


def test_quiet_drops_the_summary(capsys):
    _, out, _ = run(
        capsys, "--from-file", fixture_path("deploy-unschedulable.json"), "--quiet", *NOW
    )
    assert "stalled rollout" not in out


def test_missing_file_is_a_usage_error(capsys):
    code, _, err = run(capsys, "--from-file", "/nope/does-not-exist.json")
    assert code == EXIT_USAGE
    assert "cannot read" in err


def test_invalid_json_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run(capsys, "--from-file", str(bad))
    assert code == EXIT_USAGE
    assert "not valid JSON" in err


def test_bad_now_is_a_usage_error(capsys):
    code, _, err = run(
        capsys, "--from-file", fixture_path("healthy.json"), "--now", "yesterday"
    )
    assert code == EXIT_USAGE
    assert "RFC3339" in err


def test_negative_min_age_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        main(["--min-age", "-5"])
    assert excinfo.value.code == EXIT_USAGE


def test_namespace_and_all_namespaces_are_mutually_exclusive():
    with pytest.raises(SystemExit) as excinfo:
        main(["-A", "-n", "payments"])
    assert excinfo.value.code == EXIT_USAGE


def test_help_documents_every_flag():
    help_text = build_parser().format_help()
    for flag in FLAGS:
        assert flag in help_text, "%s is undocumented" % flag
    assert "exit codes:" in help_text
