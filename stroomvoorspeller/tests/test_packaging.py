from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ingress_only_and_no_host_port():
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "homeassistant_api: true" in config
    assert "ingress: true" in config
    assert "ingress_port: 8099" in config
    assert "panel_admin: true" in config
    assert "ports: {}" in config


def test_container_has_required_ha_labels_and_persistent_data():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'io.hass.type="app"' in dockerfile
    assert 'io.hass.arch="aarch64|amd64"' in dockerfile
    assert "DATA_DIR=/data" in dockerfile
    assert "EXPOSE 8099" in dockerfile
