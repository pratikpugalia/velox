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
"""Detect which Velox CMake targets are affected by changes in a PR.

Parses CMakeLists.txt files to extract the target dependency graph,
maps changed files to targets, computes reverse transitive closure,
and outputs build impact metrics.

No static module mappings — the dependency graph is derived entirely
from CMake at runtime.
"""

import json
import os
import re
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Parse CMakeLists.txt files
# ---------------------------------------------------------------------------

RE_ADD_LIB = re.compile(
    r"(?:velox_add_library|add_library)\s*\(\s*(\w+)", re.MULTILINE
)

RE_ADD_EXE = re.compile(
    r"add_executable\s*\(\s*(\w+)", re.MULTILINE
)

RE_LINK_LIBS = re.compile(
    r"(?:velox_link_libraries|target_link_libraries)\s*\(([^)]+)\)",
    re.MULTILINE | re.DOTALL,
)

LINK_KEYWORDS = {"PUBLIC", "PRIVATE", "INTERFACE"}

TEST_TARGET_MARKERS = {"_test", "_fuzzer", "_benchmark", "gtest", "gmock"}

# Directories whose targets are not production libraries.
NON_PROD_DIR_PARTS = {"tests", "test", "fuzzer", "benchmarks", "examples"}


def is_test_target(name: str) -> bool:
    """Return True if the target name looks like a test/fuzzer/benchmark."""
    lower = name.lower()
    return any(marker in lower for marker in TEST_TARGET_MARKERS)


def is_non_prod_dir(dirpath: str) -> bool:
    """Return True if this directory is a test/benchmark/example/fuzzer dir."""
    parts = Path(dirpath).parts
    return any(p in NON_PROD_DIR_PARTS for p in parts)


def find_cmake_files(root: str) -> list[str]:
    """Find all CMakeLists.txt under the velox/ source tree."""
    results = []
    for dirpath, _, filenames in os.walk(os.path.join(root, "velox")):
        if "CMakeLists.txt" in filenames:
            results.append(os.path.join(dirpath, "CMakeLists.txt"))
    return results


def parse_cmake(cmake_files: list[str]) -> dict:
    """Parse CMakeLists.txt files and extract:

    Returns a dict with:
      - targets: {target_name: {"dir": rel_dir, "is_test": bool, "is_exe": bool}}
      - deps: {target_name: set(dep_names)} — forward dependency edges
      - dir_targets: {rel_dir: [target_names]} — targets defined per directory
    """
    targets = {}
    deps = defaultdict(set)
    dir_targets = defaultdict(list)

    for cmake_file in cmake_files:
        content = Path(cmake_file).read_text(errors="replace")
        rel_dir = os.path.relpath(os.path.dirname(cmake_file), ".")

        # Collect library targets.
        for m in RE_ADD_LIB.finditer(content):
            name = m.group(1)
            if name.startswith("velox") or name.startswith("Velox"):
                non_prod = is_test_target(name) or is_non_prod_dir(rel_dir)
                targets[name] = {
                    "dir": rel_dir,
                    "is_test": non_prod,
                    "is_exe": False,
                }
                dir_targets[rel_dir].append(name)

        # Collect executable targets (tests, benchmarks, fuzzers).
        for m in RE_ADD_EXE.finditer(content):
            name = m.group(1)
            if name.startswith("velox") or name.startswith("Velox"):
                targets[name] = {
                    "dir": rel_dir,
                    "is_test": True,
                    "is_exe": True,
                }
                dir_targets[rel_dir].append(name)

        # Collect dependency edges.
        for m in RE_LINK_LIBS.finditer(content):
            args = m.group(1).split()
            if not args:
                continue
            target = args[0]
            for dep in args[1:]:
                if dep in LINK_KEYWORDS or dep.startswith("$<"):
                    continue
                if dep.startswith("velox") or dep.startswith("Velox"):
                    deps[target].add(dep)

    return {"targets": targets, "deps": dict(deps), "dir_targets": dict(dir_targets)}


# ---------------------------------------------------------------------------
# 2. Build reverse dependency graph
# ---------------------------------------------------------------------------


def build_reverse_deps(forward_deps: dict[str, set[str]]) -> dict[str, set[str]]:
    """Flip forward edges to get: {dep: set(targets that depend on dep)}."""
    reverse = defaultdict(set)
    for target, dep_set in forward_deps.items():
        for dep in dep_set:
            reverse[dep].add(target)
    return dict(reverse)


def transitive_closure(seeds: set[str], reverse_deps: dict[str, set[str]]) -> set[str]:
    """Walk reverse edges from seeds to find all transitively affected targets."""
    affected = set(seeds)
    queue = list(seeds)
    while queue:
        node = queue.pop()
        for dependent in reverse_deps.get(node, set()):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return affected


# ---------------------------------------------------------------------------
# 3. Map changed files to targets
# ---------------------------------------------------------------------------


def file_to_targets(
    filepath: str,
    dir_targets: dict[str, list[str]],
) -> list[str]:
    """Map a changed file to the CMake targets defined in its directory.

    Walks up the directory tree to find the nearest CMakeLists.txt directory
    that defines targets.
    """
    dirpath = os.path.dirname(filepath)
    while dirpath:
        if dirpath in dir_targets:
            return dir_targets[dirpath]
        parent = os.path.dirname(dirpath)
        if parent == dirpath:
            break
        dirpath = parent
    return []


# ---------------------------------------------------------------------------
# 4. Compute metrics
# ---------------------------------------------------------------------------


def compute_metrics(
    changed_files: list[str],
    cmake_data: dict,
) -> dict:
    """Compute build impact metrics from changed files."""
    targets = cmake_data["targets"]
    forward_deps = cmake_data["deps"]
    dir_targets = cmake_data["dir_targets"]

    reverse_deps = build_reverse_deps(forward_deps)

    # Map changed files to directly changed targets.
    directly_changed = set()
    file_target_map = {}
    for f in changed_files:
        matched = file_to_targets(f, dir_targets)
        if matched:
            file_target_map[f] = matched
            directly_changed.update(matched)

    # Separate library targets from test targets in directly changed.
    directly_changed_libs = {
        t for t in directly_changed if t in targets and not targets[t]["is_test"]
    }
    directly_changed_tests = {
        t for t in directly_changed if t in targets and targets[t]["is_test"]
    }

    # Compute transitive closure from changed library targets.
    all_affected = transitive_closure(directly_changed_libs, reverse_deps)

    # Separate affected into libs and tests.
    affected_libs = {
        t for t in all_affected if t in targets and not targets[t]["is_test"]
    }
    affected_tests = {
        t for t in all_affected if t in targets and targets[t]["is_test"]
    }
    # Also include directly changed tests.
    affected_tests |= directly_changed_tests

    # Total counts.
    all_libs = {n for n, info in targets.items() if not info["is_test"]}
    all_tests = {n for n, info in targets.items() if info["is_test"]}

    return {
        "directly_changed_libs": sorted(directly_changed_libs),
        "directly_changed_tests": sorted(directly_changed_tests),
        "affected_libs": sorted(affected_libs),
        "affected_tests": sorted(affected_tests),
        "total_libs": len(all_libs),
        "total_tests": len(all_tests),
        "skippable_libs": sorted(all_libs - affected_libs),
        "skippable_tests": sorted(all_tests - affected_tests),
    }


# ---------------------------------------------------------------------------
# 5. Format PR comment
# ---------------------------------------------------------------------------


def format_comment(metrics: dict, changed_files: list[str]) -> str:
    """Format metrics as a GitHub PR comment with collapsible details."""
    n_affected_libs = len(metrics["affected_libs"])
    n_total_libs = metrics["total_libs"]
    n_affected_tests = len(metrics["affected_tests"])
    n_total_tests = metrics["total_tests"]
    n_directly_changed = len(metrics["directly_changed_libs"])
    n_skippable_libs = len(metrics["skippable_libs"])
    n_skippable_tests = len(metrics["skippable_tests"])

    pct_libs = (
        f"{n_affected_libs / n_total_libs * 100:.1f}%" if n_total_libs else "N/A"
    )
    pct_tests = (
        f"{n_affected_tests / n_total_tests * 100:.1f}%" if n_total_tests else "N/A"
    )

    lines = [
        "## Build Impact Analysis",
        "",
        (
            f"**Changed files:** {len(changed_files)} | "
            f"**Directly changed targets:** {n_directly_changed} | "
            f"**Affected libs:** {n_affected_libs}/{n_total_libs} ({pct_libs}) | "
            f"**Affected tests:** {n_affected_tests}/{n_total_tests} ({pct_tests})"
        ),
        "",
        (
            f"**Potentially skippable:** "
            f"{n_skippable_libs} libs, {n_skippable_tests} tests"
        ),
        "",
    ]

    # Details section.
    lines.append("<details>")
    lines.append("<summary>Target details</summary>")
    lines.append("")

    if metrics["directly_changed_libs"]:
        lines.append("### Directly Changed Libraries")
        for t in metrics["directly_changed_libs"]:
            lines.append(f"- `{t}`")
        lines.append("")

    if metrics["directly_changed_tests"]:
        lines.append("### Directly Changed Tests")
        for t in metrics["directly_changed_tests"]:
            lines.append(f"- `{t}`")
        lines.append("")

    transitively_affected = sorted(
        set(metrics["affected_libs"]) - set(metrics["directly_changed_libs"])
    )
    if transitively_affected:
        lines.append("### Transitively Affected Libraries")
        for t in transitively_affected:
            lines.append(f"- `{t}`")
        lines.append("")

    transitively_affected_tests = sorted(
        set(metrics["affected_tests"]) - set(metrics["directly_changed_tests"])
    )
    if transitively_affected_tests:
        lines.append("### Affected Test Targets")
        for t in transitively_affected_tests:
            lines.append(f"- `{t}`")
        lines.append("")

    lines.append("</details>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    root = os.environ.get("REPO_ROOT", ".")
    changed_files_str = os.environ.get("CHANGED_FILES_LIST", "")
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    comment_file = os.environ.get("COMMENT_OUTPUT", "")

    changed_files = [f for f in changed_files_str.strip().splitlines() if f.strip()]
    if not changed_files:
        print("No changed files detected.")
        if comment_file:
            Path(comment_file).write_text(
                "## Build Impact Analysis\n\nNo changed files detected.\n"
            )
        return

    # Parse CMake.
    cmake_files = find_cmake_files(root)
    cmake_data = parse_cmake(cmake_files)

    print(f"Parsed {len(cmake_data['targets'])} targets from "
          f"{len(cmake_files)} CMakeLists.txt files")

    # Compute metrics.
    metrics = compute_metrics(changed_files, cmake_data)

    # Print summary.
    print(f"\n=== Build Impact ===")
    print(f"Directly changed libs: {len(metrics['directly_changed_libs'])}")
    print(f"Affected libs: {len(metrics['affected_libs'])}/{metrics['total_libs']}")
    print(f"Affected tests: {len(metrics['affected_tests'])}/{metrics['total_tests']}")
    print(f"Skippable libs: {len(metrics['skippable_libs'])}")
    print(f"Skippable tests: {len(metrics['skippable_tests'])}")

    print(f"\n=== Directly Changed ===")
    for t in metrics["directly_changed_libs"]:
        print(f"  {t}")

    print(f"\n=== All Affected Libs ===")
    for t in metrics["affected_libs"]:
        print(f"  {t}")

    print(f"\n=== Affected Tests ===")
    for t in metrics["affected_tests"]:
        print(f"  {t}")

    # Write comment.
    comment = format_comment(metrics, changed_files)
    if comment_file:
        Path(comment_file).write_text(comment)
        print(f"\nComment written to {comment_file}")

    # Write JSON metrics for downstream consumption.
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"affected_libs={json.dumps(metrics['affected_libs'])}\n")
            f.write(f"affected_tests={json.dumps(metrics['affected_tests'])}\n")
            f.write(f"total_libs={metrics['total_libs']}\n")
            f.write(f"total_tests={metrics['total_tests']}\n")


if __name__ == "__main__":
    main()
