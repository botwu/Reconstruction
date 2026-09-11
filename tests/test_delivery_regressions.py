"""交付边界回归：Harbor job 结果必须定位到实际 job 目录。"""

from pathlib import Path

from traceforge.reconstruction.workflow import _latest_job_dir


def test_latest_job_dir_returns_nested_job_not_jobs_root(tmp_path: Path) -> None:
    jobs_root = tmp_path / "hermes"
    job_dir = jobs_root / "job-001"
    (job_dir / "trial-001").mkdir(parents=True)
    (job_dir / "result.json").write_text("{}\n", encoding="utf-8")

    assert _latest_job_dir(jobs_root, set()) == job_dir


def test_latest_job_dir_excludes_preexisting_job(tmp_path: Path) -> None:
    jobs_root = tmp_path / "hermes"
    old_job = jobs_root / "old-job"
    (old_job / "trial-001").mkdir(parents=True)
    (old_job / "result.json").write_text("{}\n", encoding="utf-8")
    new_job = jobs_root / "new-job"
    (new_job / "trial-001").mkdir(parents=True)
    (new_job / "result.json").write_text("{}\n", encoding="utf-8")

    assert _latest_job_dir(jobs_root, {old_job}) == new_job
