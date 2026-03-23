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

"""Parse CMake File API codemodel output and generate a dependency graph JSON.

This script reads the CMake File API reply directory (codemodel-v2) and
produces a JSON file containing:
  - file_to_targets: mapping of source file paths to their owning targets
  - target_deps: mapping of each target to its direct dependencies

The total target count can be derived from len(target_deps).

Usage:
    python generate-dependency-graph.py \\
        --build-dir _build/release \\
        --source-dir . \\
        --output dependency-graph.json
"""

import argparse
import json
import os
import sys
from pathlib import Path


def find_codemodel_reply(reply_dir: Path) -> dict:
    """Find and parse the codemodel reply index file."""
    for f in sorted(reply_dir.iterdir()):
        if f.name.startswith("codemodel-v2-") and f.suffix == ".json":
            with open(f) as fp:
                return json.load(fp)
    print("ERROR: No codemodel-v2 reply found in", reply_dir, file=sys.stderr)
    sys.exit(1)


def parse_target_file(reply_dir: Path, target_json_file: str) -> dict:
    """Parse a single target JSON file from the file API reply."""
    target_path = reply_dir / target_json_file
    with open(target_path) as fp:
        return json.load(fp)


def build_dependency_graph(
    build_dir: str, source_dir: str
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Build the dependency graph from CMake File API output.

    Returns:
        file_to_targets: maps relative source file paths to list of target names
        target_deps: maps each target name to its direct dependency target names
    """
    reply_dir = Path(build_dir) / ".cmake" / "api" / "v1" / "reply"
    if not reply_dir.exists():
        print("ERROR: File API reply directory not found:", reply_dir, file=sys.stderr)
        print(
            "Ensure cmake was configured with the file API query file.",
            file=sys.stderr,
        )
        sys.exit(1)

    source_dir = os.path.realpath(source_dir)
    build_dir_real = os.path.realpath(build_dir)

    codemodel = find_codemodel_reply(reply_dir)

    file_to_targets: dict[str, list[str]] = {}
    target_deps: dict[str, list[str]] = {}

    configurations = codemodel.get("configurations", [])
    if not configurations:
        print("ERROR: No configurations found in codemodel reply.", file=sys.stderr)
        sys.exit(1)

    config = configurations[0]
    targets = config.get("targets", [])

    # Build an ID-to-name lookup to avoid re-parsing target files for deps.
    id_to_name: dict[str, str] = {}
    target_data_cache: dict[str, dict] = {}
    for target_ref in targets:
        target_json_file = target_ref["jsonFile"]
        target_data = parse_target_file(reply_dir, target_json_file)
        target_id = target_ref.get("id", "")
        target_name = target_data["name"]
        id_to_name[target_id] = target_name
        target_data_cache[target_id] = target_data

    for target_ref in targets:
        target_id = target_ref.get("id", "")
        target_data = target_data_cache[target_id]
        target_name = id_to_name[target_id]

        # Extract source files.
        for source in target_data.get("sources", []):
            source_path = source.get("path", "")
            if not source_path:
                continue

            # Resolve to absolute then make relative to source dir.
            if not os.path.isabs(source_path):
                abs_path = os.path.normpath(os.path.join(source_dir, source_path))
            else:
                abs_path = os.path.normpath(source_path)

            # Skip generated files (those in the build directory).
            if abs_path.startswith(build_dir_real):
                continue

            # Make path relative to source directory.
            try:
                rel_path = os.path.relpath(abs_path, source_dir)
            except ValueError:
                continue

            # Skip paths outside the source tree.
            if rel_path.startswith(".."):
                continue

            if rel_path not in file_to_targets:
                file_to_targets[rel_path] = []
            if target_name not in file_to_targets[rel_path]:
                file_to_targets[rel_path].append(target_name)

        # Extract dependencies.
        dep_names = []
        for dep in target_data.get("dependencies", []):
            dep_id = dep.get("id", "")
            if dep_id in id_to_name:
                dep_names.append(id_to_name[dep_id])
        target_deps[target_name] = dep_names

    return file_to_targets, target_deps


def main():
    parser = argparse.ArgumentParser(
        description="Generate dependency graph from CMake File API output."
    )
    parser.add_argument(
        "--build-dir",
        required=True,
        help="Path to the CMake build directory.",
    )
    parser.add_argument(
        "--source-dir",
        default=".",
        help="Path to the source directory (default: current directory).",
    )
    parser.add_argument(
        "--output",
        default="dependency-graph.json",
        help="Output JSON file path (default: dependency-graph.json).",
    )
    args = parser.parse_args()

    file_to_targets, target_deps = build_dependency_graph(
        args.build_dir, args.source_dir
    )

    graph = {
        "file_to_targets": file_to_targets,
        "target_deps": target_deps,
    }

    with open(args.output, "w") as fp:
        json.dump(graph, fp, indent=2, sort_keys=True)

    print(f"Dependency graph written to {args.output}")
    print(f"  Files mapped: {len(file_to_targets)}")
    print(f"  Targets: {len(target_deps)}")


if __name__ == "__main__":
    main()
