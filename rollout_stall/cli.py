"""Command line interface.

Exit codes are the contract: 0 clean, 1 at least one stalled rollout, 2 usage or
input error. That split is what makes it usable as a deploy gate.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import __version__
from .analyze import analyze, count_workloads, parse_time
from .collect import CollectError, filter_namespace, load_files, load_kubectl
from .render import render_json, render_table, summarize

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

DEFAULT_MIN_AGE = 60.0

EPILOG = """examples:
  # live cluster, every namespace
  k8s-rollout-stall-detector --all-namespaces

  # one namespace, flag anything stuck for more than 5 minutes
  k8s-rollout-stall-detector -n payments --min-age 300

  # offline, from saved dumps
  kubectl get deploy,sts,ds,pods -A -o json > dump.json
  k8s-rollout-stall-detector --from-file dump.json --json

exit codes:
  0  no stalled rollouts
  1  at least one stalled rollout
  2  usage or input error
"""


def _non_negative(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number of seconds" % value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("--min-age cannot be negative")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k8s-rollout-stall-detector",
        description=(
            "Find Deployments, StatefulSets and DaemonSets that are mid-rollout "
            "and not finishing, and print the reason each one is stuck."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from-file",
        metavar="PATH",
        action="append",
        help=(
            "read objects from a JSON dump instead of the cluster; repeat for "
            "several files, use - for stdin"
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "-n",
        "--namespace",
        metavar="NS",
        help="limit to one namespace (default: the kubectl current namespace)",
    )
    scope.add_argument(
        "-A",
        "--all-namespaces",
        action="store_true",
        help="look at every namespace",
    )
    parser.add_argument(
        "--min-age",
        type=_non_negative,
        default=DEFAULT_MIN_AGE,
        metavar="SECONDS",
        help=(
            "ignore rollouts that started less than SECONDS ago, so a healthy "
            "deploy in flight is not reported (default: %(default)s)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument(
        "--wide", action="store_true", help="do not truncate the DETAIL column"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print findings only, no summary line",
    )
    parser.add_argument(
        "--now",
        metavar="RFC3339",
        help=(
            "treat this timestamp as now when computing ages; use it with a dump "
            "saved earlier, e.g. 2026-11-23T12:00:00Z"
        ),
    )
    parser.add_argument(
        "--kubectl", default="kubectl", metavar="BIN", help="kubectl binary to call"
    )
    parser.add_argument(
        "--context", metavar="NAME", help="kubectl context to read from"
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    now = None
    if args.now:
        now = parse_time(args.now)
        if now is None:
            print(
                "error: --now must be an RFC3339 timestamp, e.g. 2026-11-23T12:00:00Z",
                file=sys.stderr,
            )
            return EXIT_USAGE

    try:
        if args.from_file:
            items: List[dict] = load_files(args.from_file)
            items = filter_namespace(items, args.namespace)
        else:
            items = load_kubectl(
                namespace=args.namespace,
                all_namespaces=args.all_namespaces,
                context=args.context,
                kubectl=args.kubectl,
            )
    except CollectError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE

    findings = analyze(items, now=now, min_age=args.min_age)

    if args.json:
        print(render_json(findings))
    elif findings:
        print(render_table(findings, wide=args.wide))
        if not args.quiet:
            print()
            print(summarize(findings))
    elif not args.quiet:
        print(
            "No stalled rollouts found (%d workload%s checked)."
            % (count_workloads(items), "" if count_workloads(items) == 1 else "s")
        )

    return EXIT_FINDINGS if findings else EXIT_CLEAN


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
