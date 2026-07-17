import hashlib
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy-release.sh"
COMMIT = "a" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n")
    (source / "requirements.lock").write_text("# fixture\n")
    (source / "src").mkdir()
    artifact = tmp_path / "release.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        for path in source.rglob("*"):
            archive.add(path, arcname=path.relative_to(source))
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "fixture-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fixture/__init__.py", "")
        archive.writestr("fixture-1.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    manifest = tmp_path / "wheelhouse.SHA256SUMS"
    manifest.write_text(f"{_sha256(wheel)}  {wheel.name}\n", encoding="ascii")
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -m ] && [ \"$2\" = venv ]; then\n"
        "  target=$3\n"
        "  mkdir -p \"$target/bin\"\n"
        "  printf '#!/bin/sh\\nexit 0\\n' > \"$target/bin/pip\"\n"
        "  printf '#!/bin/sh\\nexit 0\\n' > \"$target/bin/python3\"\n"
        "  for name in bilibili-podcast bilibili-podcast-admin bilibili-podcast-web bilibili-podcast-publish bilibili-podcast-crontab; do\n"
        "    printf '#!%s/bin/python3\\nexit 0\\n' \"$target\" > \"$target/bin/$name\"\n"
        "    chmod 755 \"$target/bin/$name\"\n"
        "  done\n"
        "  printf '#!/bin/sh\\ncase \" $* \" in *\" status \"*) echo '\\''{\"category\":\"status\",\"status\":\"finalized\",\"source_version\":4,\"target_version\":4,\"steps\":[],\"plan_id\":\"fixture\"}'\\'' ;; *\" permissions \"*) echo '\\''{\"category\":\"permissions\",\"status\":\"dry-run\",\"noncompliant_directory_count\":0,\"noncompliant_file_count\":0}'\\'' ;; esac\\nexit 0\\n' > \"$target/bin/bilibili-podcast-config\"\n"
        "  chmod 755 \"$target/bin/bilibili-podcast-config\"\n"
        "  chmod 755 \"$target/bin/pip\" \"$target/bin/python3\"\n"
        "  exit 0\n"
        "fi\n"
        f"exec '{sys.executable}' \"$@\"\n"
    )
    fake_python.chmod(0o755)
    return artifact, wheelhouse, manifest, fake_python


def _prepare_command(
    root: Path,
    artifact: Path,
    wheelhouse: Path,
    manifest: Path,
    python: Path,
    *,
    apply: bool,
) -> list[str]:
    command = [str(SCRIPT)]
    if apply:
        command.append("--apply")
    command.extend([
        "prepare", "--root", str(root), "--commit", COMMIT,
        "--artifact", str(artifact), "--artifact-sha256", _sha256(artifact),
        "--wheelhouse", str(wheelhouse),
        "--wheel-manifest", str(manifest),
        "--python", str(python),
    ])
    return command


def test_release_prepare_is_dry_run_by_default_and_activation_is_explicit(tmp_path: Path) -> None:
    artifact, wheelhouse, manifest, python = _inputs(tmp_path)
    root = tmp_path / "install"

    dry = subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=False
        ),
        capture_output=True, text=True, check=True,
    )
    assert "no files were written" in dry.stdout
    assert not root.exists()

    applied = subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=True
        ),
        capture_output=True, text=True, check=True,
    )
    assert "without changing current symlinks" in applied.stdout
    release = root / "releases" / COMMIT
    venv = root / "venvs" / COMMIT
    assert (release / ".release-commit").read_text().strip() == COMMIT
    assert (venv / ".release-commit").read_text().strip() == COMMIT
    assert (release / ".release-artifact-sha256").read_text().strip() == _sha256(artifact)
    assert (venv / ".release-artifact-sha256").read_text().strip() == _sha256(artifact)
    assert (venv / "bin" / "bilibili-podcast-web").read_text().splitlines()[0] == (
        f"#!{venv}/bin/python3"
    )
    assert (release.stat().st_mode & 0o222) == 0
    assert (venv.stat().st_mode & 0o222) == 0
    assert not (root / "current").exists()

    config_root = tmp_path / "config"
    config_root.mkdir()
    activate = subprocess.run([
        str(SCRIPT), "--apply", "activate", "--root", str(root),
        "--commit", COMMIT, "--config-root", str(config_root),
        "--python", str(python),
    ], capture_output=True, text=True, check=True)
    assert "without service reload or restart" in activate.stdout
    assert os.readlink(root / "current") == str(release)
    assert os.readlink(root / "current-venv") == str(venv)


def test_release_prepare_rejects_checksum_mismatch_without_writes(tmp_path: Path) -> None:
    artifact, wheelhouse, manifest, python = _inputs(tmp_path)
    root = tmp_path / "install"
    command = _prepare_command(
        root, artifact, wheelhouse, manifest, python, apply=True
    )
    command[command.index("--artifact-sha256") + 1] = "0" * 64
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 3
    assert "SHA-256 mismatch" in result.stderr
    assert not root.exists()


def test_release_dry_run_rejects_unsafe_archive_without_writes(tmp_path: Path) -> None:
    artifact, wheelhouse, manifest, python = _inputs(tmp_path)
    with tarfile.open(artifact, "w:gz") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 0
        archive.addfile(info)
    root = tmp_path / "install"

    result = subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=False
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe release archive path" in result.stderr
    assert not root.exists()


def test_release_dry_run_rejects_stale_build_output(tmp_path: Path) -> None:
    artifact, wheelhouse, manifest, python = _inputs(tmp_path)
    with tarfile.open(artifact, "w:gz") as archive:
        for name, payload in {
            "pyproject.toml": b"[project]\nname='fixture'\nversion='1'\n",
            "requirements.lock": b"",
            "build/lib/stale.py": b"stale = True\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    root = tmp_path / "install"

    result = subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=False
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "generated or local-only path" in result.stderr
    assert not root.exists()


def test_release_reuse_rejects_modified_requirements_lock(tmp_path: Path) -> None:
    artifact, wheelhouse, manifest, python = _inputs(tmp_path)
    root = tmp_path / "install"
    subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=True
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    requirements = root / "releases" / COMMIT / "requirements.lock"
    requirements.chmod(0o644)
    requirements.write_text("modified==1\n")

    result = subprocess.run(
        _prepare_command(
            root, artifact, wheelhouse, manifest, python, apply=True
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert "requirements lock SHA-256 mismatch" in result.stderr
    assert not (root / ".deploy.lock").exists()
