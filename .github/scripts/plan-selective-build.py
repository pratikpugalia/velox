#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Plan a selective build by computing affected CMake targets from changed files.

Two modes of invocation:

1. Decide which detection path to run (no graph required):

       python plan-selective-build.py \\
           --changed-files changed_files.txt \\
           --decide-mode-only

   Prints `mode=<noop|full|slow|fast>` to $GITHUB_OUTPUT (or stdout).

2. Compute affected targets and write the PR comment:

       python plan-selective-build.py \\
           --graph dependency-graph.json \\
           --changed-files changed_files.txt \\
           --build-type debug \\
           --output comment.md

Early exits when computing:
  - velox/experimental/ or velox/external/ changes → full build recommended
    (CUDA-only and vendored code outside the dependency graph).

File resolution:
  For each changed file under velox/, resolves to affected CMake targets:
  1. File API exact match (source file directly mapped to a target)
  2. Header scan match (header → source files via g++ -MM → targets)
  For headers, both results are unioned: exact match provides the owning
  target, header scan provides all targets whose sources include the header.
  Files that match neither are reported as unresolved.

Then computes the transitive reverse dependency closure and identifies the
minimal set of selective build targets.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict, deque


# Paths whose changes require a full build because they are outside the
# dependency graph (CUDA-only and vendored code).
FULL_BUILD_PREFIXES = ("velox/experimental/", "velox/external/")

# CMake files whose changes invalidate the cached graph from main: targets,
# sources, or dependencies may have shifted, so the graph must be
# regenerated from the PR branch before computing impact. Matches:
#   - any CMakeLists.txt under velox/ (including velox/CMakeLists.txt)
#   - any CMake/*.cmake helper module
# Does NOT match the repo-root CMakeLists.txt — changes to the top-level
# project entry-point are rare and usually structural enough that the
# noop → full fallback path covers them adequately.
SLOW_PATH_PATTERN = re.compile(r"(velox/(.+/)?CMakeLists\.txt|CMake/.+\.cmake)$")


def decide_mode(changed_files: list[str]) -> str:
    """Decide which detection path to run, based on changed files.

    Returns one of:
      - 'full'  — files outside the dependency graph changed; a full build
                  is required regardless of the cached graph.
      - 'slow'  — a CMakeLists.txt under velox/ or a CMake/*.cmake changed;
                  the cached graph from main may be stale, regenerate from
                  the PR branch.
      - 'noop'  — no velox/* files changed; nothing to analyze.
      - 'fast'  — default path; use the cached graph from main.
    """
    if any(f.startswith(FULL_BUILD_PREFIXES) for f in changed_files):
        return "full"
    if any(SLOW_PATH_PATTERN.match(f) for f in changed_files):
        return "slow"
    if not any(f.startswith("velox/") for f in changed_files):
        return "noop"
    return "fast"


def emit_github_output(key: str, value: str) -> None:
    """Append a key=value line to $GITHUB_OUTPUT, falling back to stdout."""
    line = f"{key}={value}"
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as fp:
            fp.write(line + "\n")
    print(line)


def load_graph(graph_path: str) -> dict:
    """Load the dependency graph JSON."""
    with open(graph_path) as fp:
        return json.load(fp)


def resolve_file_to_targets(
    file_path: str,
    file_to_targets: dict[str, list[str]],
    header_to_sources: dict[str, list[str]],
) -> tuple[set[str], str]:
    """Resolve a changed file to its affected targets.

    Returns:
        A tuple of (set of target names, resolution method string).
    """
    # Step 1: File API exact match (source or header directly in a target).
    # Step 2: Header scan match (header → source files → targets).
    # For headers, union both: exact gives the owning target, scan gives includers.
    exact_targets = set(file_to_targets.get(file_path, []))
    scan_targets: set[str] = set()

    if file_path.endswith((".h", ".hpp", ".cuh")):
        for source in header_to_sources.get(file_path, []):
            scan_targets.update(file_to_targets.get(source, []))

    combined = exact_targets | scan_targets
    if combined:
        if exact_targets and scan_targets:
            method = "exact+header-scan"
        elif exact_targets:
            method = "exact"
        else:
            method = "header-scan"
        return combined, method

    return set(), "unresolved"


def compute_reverse_deps(target_deps: dict[str, list[str]]) -> dict[str, set[str]]:
    """Invert the dependency graph to get reverse dependencies."""
    reverse: dict[str, set[str]] = defaultdict(set)
    for target, deps in target_deps.items():
        for dep in deps:
            reverse[dep].add(target)
    return reverse


def compute_transitive_closure(
    start_targets: set[str], reverse_deps: dict[str, set[str]]
) -> set[str]:
    """BFS from start_targets through reverse_deps to find all affected targets."""
    visited = set()
    queue = deque(start_targets)
    while queue:
        target = queue.popleft()
        if target in visited:
            continue
        visited.add(target)
        for dependent in reverse_deps.get(target, set()):
            if dependent not in visited:
                queue.append(dependent)
    return visited


def compute_selective_build_targets(
    affected: set[str], target_deps: dict[str, list[str]]
) -> set[str]:
    """Find the minimal set of root targets that cover all affected targets.

    These are affected targets that are not a dependency of any other
    affected target.
    """
    depended_upon = set()
    for target in affected:
        for dep in target_deps.get(target, []):
            if dep in affected:
                depended_upon.add(dep)

    return affected - depended_upon


def generate_comment(
    changed_targets: dict[str, dict],
    all_affected: set[str],
    selective_targets: set[str],
    total_targets: int,
    build_type: str,
    graph_source: str,
    unresolved_files: list[str],
) -> str:
    """Generate the PR comment markdown."""
    total_affected = len(all_affected)

    lines = []
    lines.append("## Selective Build Plan\n")

    # Group changed files by target.
    target_files: dict[str, list[str]] = defaultdict(list)
    for file_path, info in changed_targets.items():
        for target in info["targets"]:
            target_files[target].append(os.path.basename(file_path))

    # Selective build targets.
    selective_sorted = sorted(selective_targets)
    lines.append(
        f"### Selective Build Targets "
        f"(building these covers all {total_affected} affected)"
    )
    targets_str = " ".join(selective_sorted)
    lines.append("```")
    lines.append(f"cmake --build _build/{build_type} --target {targets_str}")
    lines.append("```")
    lines.append("")

    lines.append(f"**Total affected:** {total_affected}/{total_targets} targets")
    lines.append("")

    # Unresolved files warning.
    if unresolved_files:
        lines.append(
            f"> **Warning:** {len(unresolved_files)} file(s) could not be "
            f"mapped to any target. A full build may be needed."
        )
        lines.append(">")
        for f in unresolved_files[:10]:
            lines.append(f"> - `{f}`")
        if len(unresolved_files) > 10:
            lines.append(f"> - ... and {len(unresolved_files) - 10} more")
        lines.append("")

    # Collapsible affected targets breakdown.
    transitive_only = all_affected - set(target_files.keys())
    lines.append("<details>")
    lines.append(f"<summary>Affected targets ({total_affected})</summary>")
    lines.append("")
    lines.append(f"#### Directly changed ({len(target_files)})")
    lines.append("")
    lines.append("| Target | Changed Files |")
    lines.append("|--------|--------------|")
    for target in sorted(target_files.keys()):
        files_list = sorted(set(target_files[target]))
        files = ", ".join(files_list[:5])
        if len(files_list) > 5:
            files += f", ... (+{len(files_list) - 5} more)"
        lines.append(f"| `{target}` | {files} |")
    lines.append("")
    if transitive_only:
        lines.append(f"#### Transitively affected ({len(transitive_only)})")
        lines.append("")
        for target in sorted(transitive_only):
            lines.append(f"- `{target}`")
        lines.append("")
    lines.append("</details>")
    lines.append("")

    # Footer.
    lines.append("---")
    lines.append(f"*{graph_source}*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Plan a selective build from changed files."
    )
    parser.add_argument(
        "--graph",
        help=(
            "Path to the dependency graph JSON file. Required unless "
            "--decide-mode-only is given."
        ),
    )
    parser.add_argument(
        "--changed-files",
        required=True,
        help="Path to a file listing changed files (one per line).",
    )
    parser.add_argument(
        "--build-type",
        default="release",
        help="Build type for the cmake command (default: release).",
    )
    parser.add_argument(
        "--graph-source",
        default="",
        help="Description of graph source for the comment footer.",
    )
    parser.add_argument(
        "--output",
        default="comment.md",
        help="Output file for the PR comment markdown (default: comment.md).",
    )
    parser.add_argument(
        "--decide-mode-only",
        action="store_true",
        help=(
            "Print 'mode=<noop|full|slow|fast>' to $GITHUB_OUTPUT (or "
            "stdout) and exit. Does not require --graph."
        ),
    )
    args = parser.parse_args()

    with open(args.changed_files) as fp:
        changed_files = [
            line.strip() for line in fp if line.strip() and not line.startswith("#")
        ]

    if args.decide_mode_only:
        emit_github_output("mode", decide_mode(changed_files))
        return

    if not args.graph:
        print(
            "ERROR: --graph is required unless --decide-mode-only is given.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Load inputs.
    graph = load_graph(args.graph)
    file_to_targets = graph["file_to_targets"]
    header_to_sources = graph.get("header_to_sources", {})
    target_deps = graph["target_deps"]
    total_targets = len(target_deps)

    # Check for experimental/ or external/ changes — these are outside
    # the dependency graph (CUDA-only, vendored code) and need a full build.
    full_build_prefixes = ("velox/experimental/", "velox/external/")
    full_build_files = [f for f in changed_files if f.startswith(full_build_prefixes)]
    if full_build_files:
        comment = (
            "## Selective Build Plan\n\n"
            "**Full build recommended.** Files outside the dependency graph changed:\n\n"
        )
        for f in full_build_files[:10]:
            comment += f"- `{f}`\n"
        if len(full_build_files) > 10:
            comment += f"- ... and {len(full_build_files) - 10} more\n"
        comment += (
            "\nThese directories are not fully covered by the dependency graph. "
            "A full build is the safest option.\n\n"
            "```\n"
            f"cmake --build _build/{args.build_type}\n"
            "```\n\n"
            "---\n"
            f"*{args.graph_source or 'Selective build plan'}*"
        )
        with open(args.output, "w") as fp:
            fp.write(comment)
        print("experimental/external files changed — full build recommended.")
        return

    # Only keep files under velox/ that can affect build targets.
    source_files = []
    skipped_files = []
    for f in changed_files:
        if f.startswith("velox/"):
            source_files.append(f)
        else:
            skipped_files.append(f)
    if skipped_files:
        print(f"  Skipped {len(skipped_files)} non-source files")
    changed_files = source_files

    # Build lookup structures.
    reverse_deps = compute_reverse_deps(target_deps)

    # Resolve each changed file.
    directly_affected: set[str] = set()
    changed_targets: dict[str, dict] = {}
    unresolved_files: list[str] = []

    for file_path in changed_files:
        targets, method = resolve_file_to_targets(
            file_path,
            file_to_targets,
            header_to_sources,
        )
        if targets:
            directly_affected.update(targets)
            changed_targets[file_path] = {
                "targets": sorted(targets),
                "method": method,
            }
        else:
            unresolved_files.append(file_path)

    if not directly_affected:
        comment = (
            "## Selective Build Plan\n\n"
            "No build targets affected by this change.\n\n"
            "---\n"
            f"*{args.graph_source or 'Selective build plan'}*"
        )
        with open(args.output, "w") as fp:
            fp.write(comment)
        print("No targets affected.")
        return

    # Compute transitive closure.
    all_affected = compute_transitive_closure(directly_affected, reverse_deps)

    # Compute selective build targets.
    selective_targets = compute_selective_build_targets(all_affected, target_deps)

    # Generate comment.
    graph_source = args.graph_source or "Selective build plan"
    comment = generate_comment(
        changed_targets,
        all_affected,
        selective_targets,
        total_targets,
        args.build_type,
        graph_source,
        unresolved_files,
    )

    with open(args.output, "w") as fp:
        fp.write(comment)

    print(f"Comment written to {args.output}")
    print(f"  Directly affected targets: {len(directly_affected)}")
    print(f"  Total affected (transitive): {len(all_affected)}")
    print(f"  Selective build targets: {len(selective_targets)}")
    if unresolved_files:
        print(f"  Unresolved files: {len(unresolved_files)}")

    # Also output the selective build targets as a simple list for CI use.
    targets_file = os.path.splitext(args.output)[0] + "-targets.txt"
    with open(targets_file, "w") as fp:
        fp.write("\n".join(sorted(selective_targets)))
    print(f"  Selective targets list: {targets_file}")


if __name__ == "__main__":
    main()
