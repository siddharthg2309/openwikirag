import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "ops/backup_local.sh"


def test_backup_operator_contract_covers_both_durability_domains() -> None:
    script = SCRIPT.read_text()

    assert "pg_dump" in script
    assert "--format=custom" in script
    assert "--no-owner" in script
    assert '"$object_volume:/data:ro"' in script
    assert "objects.tar.gz" in script
    assert "manifest.txt" in script
    assert "shasum -a 256" in script
    assert "if [[ -e \"$backup_dir\" ]]" in script
    assert "Backup bundle created" in script


def test_backup_operator_rejects_relative_or_existing_destinations(tmp_path: Path) -> None:
    relative = subprocess.run(
        [str(SCRIPT), "relative-backup"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert relative.returncode == 2
    assert "absolute path" in relative.stderr

    existing = tmp_path / "already-there"
    existing.mkdir()
    result = subprocess.run(
        [str(SCRIPT), str(existing)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "refusing to overwrite" in result.stderr


def test_backup_operator_writes_manifest_last_and_does_not_restore() -> None:
    script = SCRIPT.read_text()
    assert script.index('cat > "$backup_dir/manifest.txt"') > script.index("objects_checksum=")
    assert "pg_restore" not in script
    assert "rm -rf" not in script
