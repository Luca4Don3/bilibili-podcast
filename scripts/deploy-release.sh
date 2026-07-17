#!/usr/bin/env bash
# Prepare and atomically select an immutable Bilibili Podcast release.
# Dry-run is the default. This script never reloads or restarts services.

set -euo pipefail

usage() {
    echo "ERROR: usage: scripts/deploy-release.sh [--apply] prepare --root PATH --commit SHA --artifact FILE --artifact-sha256 SHA --wheelhouse DIR --wheel-manifest FILE [--python PATH]" >&2
    echo "       scripts/deploy-release.sh [--apply] activate --root PATH --commit SHA --config-root PATH [--python PATH]" >&2
    exit 2
}

APPLY=false
if [ "${1:-}" = "--apply" ]; then
    APPLY=true
    shift
fi

ACTION="${1:-}"
[ -n "$ACTION" ] && shift || true
ROOT=""
COMMIT=""
ARTIFACT=""
ARTIFACT_SHA256=""
WHEELHOUSE=""
WHEEL_MANIFEST=""
CONFIG_ROOT=""
PYTHON_BIN="python3"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --root) [ "$#" -ge 2 ] || usage; ROOT="$2"; shift 2 ;;
        --commit) [ "$#" -ge 2 ] || usage; COMMIT="$2"; shift 2 ;;
        --artifact) [ "$#" -ge 2 ] || usage; ARTIFACT="$2"; shift 2 ;;
        --artifact-sha256) [ "$#" -ge 2 ] || usage; ARTIFACT_SHA256="$2"; shift 2 ;;
        --wheelhouse) [ "$#" -ge 2 ] || usage; WHEELHOUSE="$2"; shift 2 ;;
        --wheel-manifest) [ "$#" -ge 2 ] || usage; WHEEL_MANIFEST="$2"; shift 2 ;;
        --config-root) [ "$#" -ge 2 ] || usage; CONFIG_ROOT="$2"; shift 2 ;;
        --python) [ "$#" -ge 2 ] || usage; PYTHON_BIN="$2"; shift 2 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$ACTION" in
    prepare|activate) ;;
    *) usage ;;
esac
case "$ROOT" in
    /*) ;;
    *) echo "ERROR: --root must be an absolute path" >&2; exit 2 ;;
esac
if ! printf '%s' "$COMMIT" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "ERROR: --commit must be a full lowercase Git SHA" >&2
    exit 2
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "ERROR: configured Python is unavailable" >&2
    exit 2
}

sha256_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

verify_file() {
    path="$1"
    expected="$2"
    label="$3"
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "ERROR: $label is missing, not regular, or a symlink" >&2
        exit 3
    fi
    if ! printf '%s' "$expected" | grep -Eq '^[0-9a-f]{64}$'; then
        echo "ERROR: invalid $label SHA-256" >&2
        exit 2
    fi
    actual="$(sha256_file "$path")"
    if [ "$actual" != "$expected" ]; then
        echo "ERROR: $label SHA-256 mismatch" >&2
        exit 3
    fi
}

verify_marker() {
    path="$1"
    expected="$2"
    label="$3"
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "ERROR: $label marker is missing, not regular, or a symlink" >&2
        exit 3
    fi
    if [ "$(cat "$path")" != "$expected" ]; then
        echo "ERROR: $label marker mismatch" >&2
        exit 3
    fi
}

marker_value() {
    path="$1"
    label="$2"
    if [ ! -f "$path" ] || [ -L "$path" ]; then
        echo "ERROR: $label marker is missing, not regular, or a symlink" >&2
        exit 3
    fi
    cat "$path"
}

validate_release_artifact() {
    "$PYTHON_BIN" -c '
import pathlib, sys, tarfile
archive = sys.argv[1]
required = {"pyproject.toml", "requirements.lock"}
forbidden_roots = {".git", ".temp", ".venv", "build"}
seen = set()
files = set()
with tarfile.open(archive, "r:gz") as handle:
    for member in handle.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit("unsafe release archive path")
        normalized = str(path)
        if path.parts and (
            path.parts[0] in forbidden_roots
            or any(part.endswith(".egg-info") for part in path.parts)
        ):
            raise SystemExit("release archive contains a generated or local-only path")
        if normalized in seen:
            raise SystemExit("duplicate release archive entry")
        seen.add(normalized)
        if not (member.isfile() or member.isdir()):
            raise SystemExit("unsupported release archive entry")
        if member.isfile():
            files.add(normalized)
if not required.issubset(files):
    raise SystemExit("release archive is missing project metadata")
' "$ARTIFACT"
}

validate_wheelhouse() {
    [ -d "$WHEELHOUSE" ] && [ ! -L "$WHEELHOUSE" ] || {
        echo "ERROR: wheelhouse is missing, not a directory, or a symlink" >&2
        exit 3
    }
    verify_file "$WHEEL_MANIFEST" "$(sha256_file "$WHEEL_MANIFEST")" "wheel manifest"
    "$PYTHON_BIN" -c '
import hashlib, pathlib, re, sys, zipfile
root = pathlib.Path(sys.argv[1])
manifest = pathlib.Path(sys.argv[2])
expected = {}
for raw in manifest.read_text(encoding="ascii").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+\.whl)", raw)
    if match is None or match.group(2) in expected:
        raise SystemExit("invalid wheel manifest")
    expected[match.group(2)] = match.group(1)
actual = {path.name for path in root.iterdir() if path.is_file()}
if not expected or actual != set(expected):
    raise SystemExit("wheelhouse and manifest inventory differ")
for name, digest in expected.items():
    path = root / name
    if path.is_symlink() or not zipfile.is_zipfile(path):
        raise SystemExit("wheelhouse contains an unsafe or invalid wheel")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit("wheel manifest SHA-256 mismatch")
    with zipfile.ZipFile(path) as archive:
        if not any(item.endswith(".dist-info/WHEEL") for item in archive.namelist()):
            raise SystemExit("wheelhouse contains invalid wheel metadata")
' "$WHEELHOUSE" "$WHEEL_MANIFEST"
}

validate_python_metadata() {
    interpreter="$1"
    metadata_source="${2:-$ARTIFACT}"
    "$interpreter" -c '
import pathlib, platform, sys, tarfile, tomllib
try:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import Version
except ImportError:
    from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
    from pip._vendor.packaging.version import Version
source = pathlib.Path(sys.argv[1])
if source.is_dir():
    metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
else:
    with tarfile.open(source, "r:gz") as handle:
        member = handle.getmember("pyproject.toml")
        metadata = tomllib.loads(handle.extractfile(member).read().decode("utf-8"))
required = str(metadata.get("project", {}).get("requires-python", ""))
try:
    specifier = SpecifierSet(required)
except InvalidSpecifier:
    raise SystemExit("invalid Requires-Python in release metadata")
if required and Version(platform.python_version()) not in specifier:
    raise SystemExit("configured Python does not satisfy release Requires-Python")
' "$metadata_source"
    if [ -f "$metadata_source" ]; then
        [ -s "$metadata_source" ] || { echo "ERROR: release artifact is empty" >&2; exit 3; }
    fi
}

validate_lock_file() {
    archive="$1"
    "$PYTHON_BIN" -c '
import re, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as handle:
    lines = handle.extractfile("requirements.lock").read().decode("utf-8").splitlines()
requirements = []
for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith(("-", ".")) or " @ " in line or "://" in line:
        raise SystemExit("requirements.lock contains a non-offline requirement")
    requirement = line.split(";", 1)[0].strip()
    if "==" not in requirement:
        raise SystemExit("requirements.lock contains an unpinned requirement")
    requirements.append(requirement)
if len(requirements) != len(set(requirements)):
    raise SystemExit("requirements.lock contains duplicate requirements")
' "$archive"
}

RELEASES="$ROOT/releases"
VENVS="$ROOT/venvs"
RELEASE="$RELEASES/$COMMIT"
VENV="$VENVS/$COMMIT"
DEPLOY_LOCK="$ROOT/.deploy.lock"

acquire_deploy_lock() {
    if ! mkdir "$DEPLOY_LOCK" 2>/dev/null; then
        echo "ERROR: another release deployment holds $DEPLOY_LOCK" >&2
        exit 3
    fi
}

release_deploy_lock() {
    rmdir "$DEPLOY_LOCK" 2>/dev/null || true
}

verify_required_executables() {
    for executable in \
        python3 \
        bilibili-podcast \
        bilibili-podcast-admin \
        bilibili-podcast-config \
        bilibili-podcast-web \
        bilibili-podcast-publish \
        bilibili-podcast-crontab
    do
        if [ ! -x "$VENV/bin/$executable" ]; then
            echo "ERROR: prepared virtualenv is missing required executable: $executable" >&2
            exit 3
        fi
    done
}

if [ "$ACTION" = "prepare" ]; then
    [ -n "$ARTIFACT" ] && [ -n "$ARTIFACT_SHA256" ] || usage
    [ -n "$WHEELHOUSE" ] && [ -n "$WHEEL_MANIFEST" ] || usage
    [ -z "$CONFIG_ROOT" ] || usage
    verify_file "$ARTIFACT" "$ARTIFACT_SHA256" "release artifact"
    validate_release_artifact
    validate_wheelhouse
    validate_python_metadata "$PYTHON_BIN"
    validate_lock_file "$ARTIFACT"
    "$PYTHON_BIN" -m pip --version >/dev/null 2>&1 || {
        echo "ERROR: configured Python does not provide pip" >&2
        exit 3
    }
    "$PYTHON_BIN" -m pip install --dry-run --ignore-installed \
        --no-index --find-links "$WHEELHOUSE" -r <(
            "$PYTHON_BIN" -c '
import sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as handle:
    sys.stdout.buffer.write(handle.extractfile("requirements.lock").read())
' "$ARTIFACT"
        ) >/dev/null
    echo "Release preparation"
    echo "  mode: $([ "$APPLY" = true ] && echo apply || echo dry-run)"
    echo "  commit: $COMMIT"
    echo "  service actions: none"
    if [ "$APPLY" != true ]; then
        echo "Dry-run complete; checksums verified and no files were written."
        exit 0
    fi

    umask 022
    mkdir -p "$RELEASES" "$VENVS"
    mkdir -p -m 700 "$ROOT/.temp"
    release_stage="$ROOT/.temp/release-$COMMIT-$$"
    venv_stage="$ROOT/.temp/venv-$COMMIT-$$"
    cleanup() {
        status=$?
        trap - EXIT
        [ ! -e "$release_stage" ] || rm -rf -- "$release_stage"
        [ ! -e "$venv_stage" ] || rm -rf -- "$venv_stage"
        release_deploy_lock
        exit "$status"
    }
    acquire_deploy_lock
    trap cleanup EXIT
    if [ -e "$release_stage" ] || [ -e "$venv_stage" ]; then
        echo "ERROR: release staging path already exists" >&2
        exit 3
    fi

    if [ -e "$RELEASE" ]; then
        [ -d "$RELEASE" ] && [ ! -L "$RELEASE" ] || {
            echo "ERROR: existing release is not a regular directory" >&2
            exit 3
        }
        verify_marker "$RELEASE/.release-commit" "$COMMIT" "release commit"
        verify_marker "$RELEASE/.release-artifact-sha256" "$ARTIFACT_SHA256" "release artifact"
        wheel_manifest_sha256="$(marker_value "$RELEASE/.wheel-manifest-sha256" "wheel manifest")"
        verify_file "$WHEEL_MANIFEST" "$wheel_manifest_sha256" "wheel manifest"
        requirements_sha256="$(marker_value "$RELEASE/.requirements-lock-sha256" "requirements lock")"
        verify_file "$RELEASE/requirements.lock" "$requirements_sha256" "requirements lock"
    else
        mkdir -m 755 "$release_stage"
        "$PYTHON_BIN" -c '
import pathlib, sys, tarfile
archive, target = sys.argv[1:]
root = pathlib.Path(target).resolve()
with tarfile.open(archive, "r:gz") as handle:
    for member in handle.getmembers():
        destination = (root / member.name).resolve()
        if root not in (destination, *destination.parents):
            raise SystemExit("unsafe release archive path")
        if not (member.isfile() or member.isdir()):
            raise SystemExit("unsupported release archive entry")
    handle.extractall(root, filter="data")
' "$ARTIFACT" "$release_stage"
        [ -f "$release_stage/pyproject.toml" ] && [ -f "$release_stage/requirements.lock" ] || {
            echo "ERROR: release archive is missing project metadata" >&2
            exit 3
        }
        printf '%s\n' "$COMMIT" > "$release_stage/.release-commit"
        printf '%s\n' "$ARTIFACT_SHA256" > "$release_stage/.release-artifact-sha256"
        requirements_sha256="$(sha256_file "$release_stage/requirements.lock")"
        wheel_manifest_sha256="$(sha256_file "$WHEEL_MANIFEST")"
        printf '%s\n' "$requirements_sha256" > "$release_stage/.requirements-lock-sha256"
        printf '%s\n' "$wheel_manifest_sha256" > "$release_stage/.wheel-manifest-sha256"
        mv "$release_stage" "$RELEASE"
    fi

    if [ -e "$VENV" ]; then
        [ -d "$VENV" ] && [ ! -L "$VENV" ] || {
            echo "ERROR: existing virtualenv is not a regular directory" >&2
            exit 3
        }
        verify_marker "$VENV/.release-commit" "$COMMIT" "virtualenv commit"
        verify_marker "$VENV/.wheel-manifest-sha256" "$wheel_manifest_sha256" "wheel manifest"
        verify_marker "$VENV/.release-artifact-sha256" "$ARTIFACT_SHA256" "virtualenv release artifact"
        verify_marker "$VENV/.requirements-lock-sha256" "$requirements_sha256" "virtualenv requirements lock"
    else
        "$PYTHON_BIN" -m venv "$venv_stage"
        "$venv_stage/bin/pip" install --no-index --find-links "$WHEELHOUSE" \
            -r "$RELEASE/requirements.lock"
        "$venv_stage/bin/pip" install --no-index --find-links "$WHEELHOUSE" \
            --no-deps --no-build-isolation "$RELEASE"
        "$venv_stage/bin/python3" -m compileall -q "$RELEASE/src"
        "$venv_stage/bin/python3" -c 'import bilibili_api, bilibili_podcast, sqlite3, uvicorn'
        "$PYTHON_BIN" -c '
import pathlib, sys
source, target = map(pathlib.Path, sys.argv[1:])
old = str(source).encode()
new = str(target).encode()
for path in (source / "bin").iterdir():
    if path.is_symlink() or not path.is_file():
        continue
    payload = path.read_bytes()
    if payload.startswith(b"#!") and old in payload:
        path.write_bytes(payload.replace(old, new))
' "$venv_stage" "$VENV"
        printf '%s\n' "$COMMIT" > "$venv_stage/.release-commit"
        printf '%s\n' "$wheel_manifest_sha256" > "$venv_stage/.wheel-manifest-sha256"
        printf '%s\n' "$ARTIFACT_SHA256" > "$venv_stage/.release-artifact-sha256"
        printf '%s\n' "$requirements_sha256" > "$venv_stage/.requirements-lock-sha256"
        mv "$venv_stage" "$VENV"
    fi
    verify_required_executables
    chmod -R a-w "$RELEASE" "$VENV"
    trap - EXIT
    release_deploy_lock
    echo "Release prepared without changing current symlinks or services."
    exit 0
fi

[ -n "$CONFIG_ROOT" ] || usage
[ -z "$ARTIFACT$ARTIFACT_SHA256$WHEELHOUSE$WHEEL_MANIFEST" ] || usage
case "$CONFIG_ROOT" in
    /*) ;;
    *) echo "ERROR: --config-root must be an absolute path" >&2; exit 2 ;;
esac
if [ ! -d "$RELEASE" ] || [ -L "$RELEASE" ] || [ ! -d "$VENV" ] || [ -L "$VENV" ]; then
    echo "ERROR: requested release is not completely prepared" >&2
    exit 3
fi
verify_marker "$RELEASE/.release-commit" "$COMMIT" "release commit"
verify_marker "$VENV/.release-commit" "$COMMIT" "virtualenv commit"
release_artifact_sha256="$(marker_value "$RELEASE/.release-artifact-sha256" "release artifact")"
venv_artifact_sha256="$(marker_value "$VENV/.release-artifact-sha256" "virtualenv release artifact")"
if ! printf '%s' "$release_artifact_sha256" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "ERROR: invalid release artifact completion marker" >&2
    exit 3
fi
[ "$release_artifact_sha256" = "$venv_artifact_sha256" ] || {
    echo "ERROR: release and virtualenv artifact markers differ" >&2
    exit 3
}
requirements_sha256="$(marker_value "$RELEASE/.requirements-lock-sha256" "requirements lock")"
verify_file "$RELEASE/requirements.lock" "$requirements_sha256" "requirements lock"
verify_marker "$VENV/.requirements-lock-sha256" "$requirements_sha256" "virtualenv requirements lock"
wheel_manifest_sha256="$(marker_value "$RELEASE/.wheel-manifest-sha256" "wheel manifest")"
verify_marker "$VENV/.wheel-manifest-sha256" "$wheel_manifest_sha256" "virtualenv wheel manifest"
if ! printf '%s' "$wheel_manifest_sha256" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "ERROR: invalid wheel manifest completion marker" >&2
    exit 3
fi
verify_required_executables
validate_python_metadata "$VENV/bin/python3" "$RELEASE"

candidate_config="$VENV/bin/bilibili-podcast-config"
"$candidate_config" --root "$CONFIG_ROOT" validate >/dev/null
status_json="$("$candidate_config" --root "$CONFIG_ROOT" status)"
"$VENV/bin/python3" -c '
import json, sys
value = json.loads(sys.argv[1])
if value.get("target_version") != 4 or value.get("source_version") != 4:
    raise SystemExit("candidate and installation versions differ")
if value.get("status") not in {"not_prepared", "finalized"}:
    raise SystemExit("installation has an active or pending upgrade plan")
' "$status_json"
permissions_json="$("$candidate_config" --root "$CONFIG_ROOT" permissions --format json)"
"$VENV/bin/python3" -c '
import json, sys
value = json.loads(sys.argv[1])
if value.get("noncompliant_directory_count") or value.get("noncompliant_file_count"):
    raise SystemExit("runtime permissions are not compliant")
' "$permissions_json"

echo "Release activation"
echo "  mode: $([ "$APPLY" = true ] && echo apply || echo dry-run)"
echo "  commit: $COMMIT"
echo "  service actions: none"
if [ "$APPLY" != true ]; then
    echo "Dry-run complete; no symlinks were changed."
    exit 0
fi

acquire_deploy_lock
trap release_deploy_lock EXIT
"$PYTHON_BIN" -c '
import os, pathlib, sys
root, release, venv = map(pathlib.Path, sys.argv[1:])
updates = []
for name, target in (("current", release), ("current-venv", venv)):
    current = root / name
    if current.exists() and not current.is_symlink():
        raise SystemExit(f"refusing to replace non-symlink {name}")
    temporary = root / f".{name}-new"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    updates.append((current, temporary, os.readlink(current) if current.is_symlink() else None))
replaced = []
try:
    for current, temporary, previous in updates:
        os.replace(temporary, current)
        replaced.append((current, previous))
except Exception:
    for current, previous in reversed(replaced):
        current.unlink(missing_ok=True)
        if previous is not None:
            current.symlink_to(previous)
    raise
descriptor = os.open(root, os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
' "$ROOT" "$RELEASE" "$VENV"
trap - EXIT
release_deploy_lock
echo "Release symlinks activated without service reload or restart."
