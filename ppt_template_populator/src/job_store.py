from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from uuid import uuid4


@dataclass
class GenerationJob:
    job_id: str
    status: str = "queued"
    stages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    output_path: Path | None = None


class InMemoryJobStore:
    """Process-local prototype store; deploy one API worker when using it."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}
        self._lock = Lock()

    def create(self) -> GenerationJob:
        job = GenerationJob(job_id=uuid4().hex)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> GenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: object) -> GenerationJob:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            return job
