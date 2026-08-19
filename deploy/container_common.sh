#!/usr/bin/env bash

die() {
    echo "ERROR: $*" >&2
    exit 1
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed or is not in PATH"
}

resolve_project_dir() {
    local project_dir="$1"
    [[ -d "$project_dir" ]] || die "project directory does not exist: $project_dir"
    (cd "$project_dir" && pwd -P)
}

require_project_files() {
    local project_dir="$1"
    shift
    local relative_path
    for relative_path in "$@"; do
        [[ -f "$project_dir/$relative_path" ]] || \
            die "not an npu-monitor project directory; missing: $project_dir/$relative_path"
    done
}

require_image() {
    docker image inspect "$1" >/dev/null 2>&1 || die "image does not exist locally: $1"
}

require_new_container_name() {
    if docker container inspect "$1" >/dev/null 2>&1; then
        die "container already exists: $1 (the script will not delete or replace it)"
    fi
}

require_host_paths() {
    local missing_paths=()
    local host_path
    for host_path in "$@"; do
        [[ -e "$host_path" ]] || missing_paths+=("$host_path")
    done
    if (( ${#missing_paths[@]} > 0 )); then
        echo "ERROR: required host paths are missing:" >&2
        printf '  %s\n' "${missing_paths[@]}" >&2
        exit 1
    fi
}

print_created() {
    local component="$1"
    local name="$2"
    local container_id="$3"
    local image_ref="$4"
    local project_dir="$5"
    echo "$component container created successfully."
    echo "  name:       $name"
    echo "  id:         $container_id"
    echo "  image:      $image_ref"
    echo "  project:    $project_dir -> /work/monitor"
    echo "  init:       /work/monitor/deploy/mini_init.py"
}
