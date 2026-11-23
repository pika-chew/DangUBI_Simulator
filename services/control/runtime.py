"""로컬 실행과 컨테이너에서 공유하는 파일 경로 설정."""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    worker: Path
    database: Path
    checkpoints: Path
    mqtt_profile: Path
    registry: Path

    @classmethod
    def from_env(cls, root: Path, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ

        def configured(name, default):
            value = env.get(name)
            path = Path(value).expanduser() if value else default
            return path if path.is_absolute() else root / path

        data = configured("DANGUBI_DATA_DIR", root / ".local")
        return cls(
            worker=configured("DANGUBI_WORKER", root / ".local" / ("worker.exe" if os.name == "nt" else "worker")),
            database=configured("DANGUBI_DB", data / "runs.sqlite3"),
            checkpoints=configured("DANGUBI_CHECKPOINT_DB", data / "obd-state.sqlite3"),
            mqtt_profile=configured("DANGUBI_MQTT_PROFILE", root / ".local" / "mqtt.json"),
            registry=configured("DANGUBI_REGISTRY_DB", data / "fleet-registry.sqlite3"),
        )
