"""Static safety contract for the local restore operator."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "ops/restore_local.sh"


def test_restore_operator_verifies_before_mutating_explicit_targets() -> None:
    script = SCRIPT.read_text()

    assert "manifest_value postgres_dump_sha256" in script
    assert "manifest_value object_snapshot_sha256" in script
    assert "docker exec -i \"$database_container\"" in script
    assert "--clean" in script and "--if-exists" in script
    assert '"$object_volume:/data:ro"' in script
    assert '[[ -z "$object_entries" ]]' in script
    assert '"$confirmed_target" == "$database_container:$object_volume"' in script
    assert script.index("actual_postgres=") < script.index('docker exec -i "$database_container"')
    assert "docker compose" not in script


def test_restore_operator_rejects_relative_or_incomplete_backups(tmp_path: Path) -> None:
    relative = subprocess.run(
        [
            str(SCRIPT),
            "relative",
            "--database-container",
            "target",
            "--object-volume",
            "volume",
            "--confirm-target",
            "target:volume",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert relative.returncode == 2

    incomplete = tmp_path / "backup"
    incomplete.mkdir()
    result = subprocess.run(
        [
            str(SCRIPT),
            str(incomplete),
            "--database-container",
            "target",
            "--object-volume",
            "volume",
            "--confirm-target",
            "target:volume",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "incomplete" in result.stderr


def test_restore_operator_requires_empty_target_and_never_deletes_canonical_data() -> None:
    script = SCRIPT.read_text()

    assert "Object target volume must be empty" in script
    assert "rm -rf" not in script
    assert "docker compose down" not in script
