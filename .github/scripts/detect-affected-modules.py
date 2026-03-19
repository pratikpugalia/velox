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

Uses the CMake file API (codemodel v2) to get exact source file to target
mappings and dependency edges. This is more accurate than text parsing
because CMake resolves all variables, globs, and conditional logic.

The workflow runs cmake configure with a file API query, then this script
reads the JSON response to compute build impact.
"""

import json
import os
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Read CMake file API response
# ---------------------------------------------------------------------------


def find_reply_dir(build_dir: str) -> Path:
    """Find the CMake file API reply directory."""
    reply_dir = Path(build_dir) / ".cmake" / "api" / "v1" / "reply"
    if not reply_dir.is_dir():
        raise FileNotFoundError(
            f"CMake file API reply not found at {reply_dir}. "
            "Run cmake configure with a codemodel-v2 query first."
        )
    return reply_dir


def load_codemodel(reply_dir: Path) -> dict:
    """Load the codemodel v2 index file."""
    for f in sorted(reply_dir.iterdir()):
        if f.name.startswith("codemodel-v2-"):
            return json.loads(f.read_text())
    raise FileNotFoundError("No codemodel-v2 response found in reply directory.")


def parse_file_api(build_dir: str, source_dir: str) -> dict:
    """Parse CMake file API response and extract:

    Returns a dict with:
      - targets: {target_name: {"type": str, "sources": [relative paths]}}
      - deps: {target_name: set(dep_names)} — forward dependency edges
      - file_to_targets: {relative_path: [target_names]} — exact file mapping
    """
    reply_dir = find_reply_dir(build_dir)
    codemodel = load_codemodel(reply_dir)

    targets = {}
    deps = defaultdict(set)
    file_to_targets = defaultdict(list)

    # The codemodel contains configurations, each with a list of targets.
    for config in codemodel.get("configurations", []):
        for target_ref in config.get("targets", []):
            target_file = reply_dir / target_ref["jsonFile"]
            target_data = json.loads(target_file.read_text())

            name = target_data["name"]
            target_type = target_data.get("type", "UNKNOWN")

            # Skip imported/interface targets.
            if target_type in ("INTERFACE_LIBRARY", "UTILITY"):
                continue

            # Collect source files.
            source_files = []
            for source_group in target_data.get("compileGroups", []):
                for source_idx in source_group.get("sourceIndexes", []):
                    source_entry = target_data["sources"][source_idx]
                    source_path = source_entry.get("path", "")
                    if source_path:
                        source_files.append(source_path)

            # Also check top-level sources list for files not in compile groups.
            for source_entry in target_data.get("sources", []):
                source_path = source_entry.get("path", "")
                if source_path and source_path not in source_files:
                    # Make path relative to source dir.
                    source_files.append(source_path)

            # Normalize paths to be relative to source directory.
            normalized_sources = []
            for src in source_files:
                if os.path.isabs(src):
                    try:
                        src = os.path.relpath(src, source_dir)
                    except ValueError:
                        continue
                # Skip generated files (in build dir).
                if src.startswith("_build") or src.startswith(".."):
                    continue
                normalized_sources.append(src)

            is_test = target_type == "EXECUTABLE" or _is_non_prod_target(
                name, target_data
            )

            targets[name] = {
                "type": target_type,
                "sources": normalized_sources,
                "is_test": is_test,
            }

            # Build file-to-target mapping.
            for src in normalized_sources:
                file_to_targets[src].append(name)

            # Collect dependencies.
            for dep_entry in target_data.get("dependencies", []):
                dep_id = dep_entry.get("id", "")
                # The id format is "target_name::hash@build_dir".
                dep_name = dep_id.split("::")[0] if "::" in dep_id else dep_id
                if dep_name and (
                    dep_name.startswith("velox") or dep_name.startswith("Velox")
                ):
                    deps[name].add(dep_name)

    return {
        "targets": targets,
        "deps": dict(deps),
        "file_to_targets": dict(file_to_targets),
    }


def _is_non_prod_target(name: str, target_data: dict) -> bool:
    """Check if a target is non-production (test/benchmark/fuzzer/example)."""
    lower = name.lower()
    markers = {"_test", "_fuzzer", "_benchmark", "gtest", "gmock"}
    if any(m in lower for m in markers):
        return True

    # Check source directory for test/benchmark/example paths.
    source_dir = target_data.get("paths", {}).get("source", "")
    non_prod_parts = {"tests", "test", "fuzzer", "benchmarks", "examples"}
    if source_dir:
        parts = Path(source_dir).parts
        if any(p in non_prod_parts for p in parts):
            return True

    return False


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


def map_files_to_targets(
    changed_files: list[str],
    file_to_targets: dict[str, list[str]],
) -> set[str]:
    """Map changed files to their exact CMake targets."""
    directly_changed = set()
    for f in changed_files:
        if f in file_to_targets:
            directly_changed.update(file_to_targets[f])
    return directly_changed


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
    file_to_targets = cmake_data["file_to_targets"]

    reverse_deps = build_reverse_deps(forward_deps)

    # Map changed files to directly changed targets.
    directly_changed = map_files_to_targets(changed_files, file_to_targets)

    # Separate library targets from test targets.
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
    build_dir = os.environ.get("BUILD_DIR", "_build/impact")
    source_dir = os.environ.get("SOURCE_DIR", ".")
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

    # Parse CMake file API response.
    cmake_data = parse_file_api(build_dir, source_dir)

    print(f"Loaded {len(cmake_data['targets'])} targets from CMake file API")
    print(
        f"File-to-target mappings: {len(cmake_data['file_to_targets'])} source files"
    )

    # Compute metrics.
    metrics = compute_metrics(changed_files, cmake_data)

    # Print summary.
    print("\n=== Build Impact ===")
    print(f"Directly changed libs: {len(metrics['directly_changed_libs'])}")
    print(f"Affected libs: {len(metrics['affected_libs'])}/{metrics['total_libs']}")
    print(f"Affected tests: {len(metrics['affected_tests'])}/{metrics['total_tests']}")
    print(f"Skippable libs: {len(metrics['skippable_libs'])}")
    print(f"Skippable tests: {len(metrics['skippable_tests'])}")

    print("\n=== Directly Changed ===")
    for t in metrics["directly_changed_libs"]:
        print(f"  {t}")

    print("\n=== All Affected Libs ===")
    for t in metrics["affected_libs"]:
        print(f"  {t}")

    print("\n=== Affected Tests ===")
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
