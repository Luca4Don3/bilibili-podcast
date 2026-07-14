#!/usr/bin/env bash
# Prepare and atomically select an immutable Bilibili Podcast release.
# Dry-run is the default. This script never reloads or restarts services.

set -euo pipefail

usage() {
    echo "ERROR: usage: scripts/deploy-release.sh [--apply] prepare --root PATH --commit SHA --artifact FILE --artifact-sha256 SHA --bootstrap-wheel FILE --bootstrap-wheel-sha256 SHA [--python PATH]" >&2
    echo "       scripts/deploy-release.sh [--apply] activate --root PATH --commit SHA [--python PATH]" >&2
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
BOOTSTRAP_WHEEL=""
BOOTSTRAP_WHEEL_SHA256=""
PYTHON_BIN="python3"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --root) [ "$#" -ge 2 ] || usage; ROOT="$2"; shift 2 ;;
        --commit) [ "$#" -ge 2 ] || usage; COMMIT="$2"; shift 2 ;;
        --artifact) [ "$#" -ge 2 ] || usage; ARTIFACT="$2"; shift 2 ;;
        --artifact-sha256) [ "$#" -ge 2 ] || usage; ARTIFACT_SHA256="$2"; shift 2 ;;
        --bootstrap-wheel) [ "$#" -ge 2 ] || usage; BOOTSTRAP_WHEEL="$2"; shift 2 ;;
        --bootstrap-wheel-sha256) [ "$#" -ge 2 ] || usage; BOOTSTRAP_WHEEL_SHA256="$2"; shift 2 ;;
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

validate_bootstrap_wheel() {
    "$PYTHON_BIN" -c '
import sys, zipfile
wheel = sys.argv[1]
if not zipfile.is_zipfile(wheel):
    raise SystemExit("invalid bootstrap wheel")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    if not any(name.endswith(".dist-info/WHEEL") for name in names):
        raise SystemExit("invalid bootstrap wheel metadata")
' "$BOOTSTRAP_WHEEL"
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
    [ -n "$BOOTSTRAP_WHEEL" ] && [ -n "$BOOTSTRAP_WHEEL_SHA256" ] || usage
    verify_file "$ARTIFACT" "$ARTIFACT_SHA256" "release artifact"
    verify_file "$BOOTSTRAP_WHEEL" "$BOOTSTRAP_WHEEL_SHA256" "bootstrap wheel"
    validate_release_artifact
    validate_bootstrap_wheel
    echo "Release preparation"
    echo "  mode: $([ "$APPLY" = true ] && echo apply || echo dry-run)"
    echo "  commit: $COMMIT"
    echo "  release: $RELEASE"
    echo "  virtualenv: $VENV"
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
        printf '%s\n' "$requirements_sha256" > "$release_stage/.requirements-lock-sha256"
        mv "$release_stage" "$RELEASE"
    fi

    if [ -e "$VENV" ]; then
        [ -d "$VENV" ] && [ ! -L "$VENV" ] || {
            echo "ERROR: existing virtualenv is not a regular directory" >&2
            exit 3
        }
        verify_marker "$VENV/.release-commit" "$COMMIT" "virtualenv commit"
        verify_marker "$VENV/.bootstrap-wheel-sha256" "$BOOTSTRAP_WHEEL_SHA256" "bootstrap wheel"
        verify_marker "$VENV/.release-artifact-sha256" "$ARTIFACT_SHA256" "virtualenv release artifact"
        verify_marker "$VENV/.requirements-lock-sha256" "$requirements_sha256" "virtualenv requirements lock"
    else
        "$PYTHON_BIN" -m venv "$venv_stage"
        "$venv_stage/bin/pip" install "$BOOTSTRAP_WHEEL"
        "$venv_stage/bin/pip" install -c "$RELEASE/requirements.lock" -r "$RELEASE/requirements.lock"
        "$venv_stage/bin/pip" install --no-deps "$RELEASE"
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
        printf '%s\n' "$BOOTSTRAP_WHEEL_SHA256" > "$venv_stage/.bootstrap-wheel-sha256"
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
bootstrap_sha256="$(marker_value "$VENV/.bootstrap-wheel-sha256" "bootstrap wheel")"
if ! printf '%s' "$bootstrap_sha256" | grep -Eq '^[0-9a-f]{64}$'; then
    echo "ERROR: invalid bootstrap wheel completion marker" >&2
    exit 3
fi
verify_required_executables

echo "Release activation"
echo "  mode: $([ "$APPLY" = true ] && echo apply || echo dry-run)"
echo "  commit: $COMMIT"
echo "  current release: $RELEASE"
echo "  current virtualenv: $VENV"
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
