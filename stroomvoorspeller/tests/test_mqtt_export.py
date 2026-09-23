from datetime import datetime, timedelta, timezone

from app.mqtt_export import MQTTExporter, discover_mqtt_service


class FakeTransport:
    def __init__(self, service=None):
        self.service = service
        self.messages = []
        self.connected = False

    def connect(self):
        self.connected = True

    def publish(self, topic, payload, retain=True):
        self.messages.append((topic, payload, retain))

    def disconnect(self):
        self.connected = False


def test_disabled_exporter_does_not_discover_or_connect():
    exporter = MQTTExporter(enabled=False, service_loader=lambda: (_ for _ in ()).throw(AssertionError()))
    assert exporter.start() is False
    assert exporter.publish({}, []) is False


def test_discovery_and_publish_of_useful_read_only_sensors():
    now = datetime(2026, 9, 24, 10, tzinfo=timezone.utc)
    transport = FakeTransport()
    exporter = MQTTExporter(enabled=True, service_loader=lambda: {"host": "broker", "port": 1883},
                             transport_factory=lambda _: transport, clock=lambda: now)
    assert exporter.start()
    discovery = [m for m in transport.messages if m[0].endswith("/config")]
    assert len(discovery) == 3
    assert all('"device"' in payload for _, payload, _ in discovery)

    slots = []
    for i in range(16):
        start = now + timedelta(minutes=15 * i)
        slots.append({"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(),
                      "price": .3 - i / 100, "status": "predicted"})
    assert exporter.publish({}, {"slots": slots}, {"stale": False})
    states = {topic: payload for topic, payload, _ in transport.messages if "/sensor/" in topic}
    assert states["stroomvoorspeller/sensor/next_quarter_price"] == "0.29"
    assert states["stroomvoorspeller/sensor/cheapest_next_3h"] == "0.205"
    assert states["stroomvoorspeller/sensor/data_status"] == "fresh"
    assert '"status":"predicted"' in states["stroomvoorspeller/sensor/next_quarter_price/attributes"]
    assert '"contains_forecast":true' in states["stroomvoorspeller/sensor/cheapest_next_3h/attributes"]


def test_stop_clears_retained_discovery_and_disconnects():
    transport = FakeTransport()
    exporter = MQTTExporter(enabled=True, service_loader=lambda: {}, transport_factory=lambda _: transport)
    assert exporter.start()
    exporter.stop(remove_entities=True)
    cleared = [m for m in transport.messages if m[0].endswith("/config") and m[1] == ""]
    assert len(cleared) == 3
    assert not transport.connected


def test_start_unavailable_is_quiet_and_retryable(caplog):
    transport = FakeTransport()
    calls = 0

    def load():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret-password")
        return {"host": "broker", "port": 1883}

    exporter = MQTTExporter(enabled=True, service_loader=load, transport_factory=lambda _: transport)
    assert not exporter.start()
    assert "secret-password" not in caplog.text
    assert exporter.start()


def test_supervisor_service_payload_unwraps_data():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return b'{"data":{"host":"mqtt","port":"1883","username":"u"}}'

    service = discover_mqtt_service("token", opener=lambda *_args, **_kwargs: Response())
    assert service == {"host": "mqtt", "port": "1883", "username": "u"}
