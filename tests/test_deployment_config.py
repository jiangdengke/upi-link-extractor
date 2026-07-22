from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_uses_named_runtime_volume() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./runtime:/app/runtime" not in compose
    assert "upi-link-runtime:/app/runtime" in compose
    assert "volumes:\n  upi-link-runtime:" in compose


def test_dockerfile_declares_runtime_volume() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'VOLUME ["/app/runtime"]' in dockerfile
