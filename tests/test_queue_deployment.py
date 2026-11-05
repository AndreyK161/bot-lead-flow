from pathlib import Path


def test_worker_hides_job_payload_and_rotates_logs():
    compose = Path("docker-compose.yml").read_text()
    worker = compose.split("  telegram-worker:", 1)[1].split("\n  planner:", 1)[0]

    assert "--disable-job-desc-logging" in worker
    assert 'max-size: "10m"' in worker
    assert 'max-file: "5"' in worker
