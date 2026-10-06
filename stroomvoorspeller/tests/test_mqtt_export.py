from datetime import datetime, timedelta, timezone
import json

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
    assert len(discovery) == 5
    assert all('"device"' in payload for _, payload, _ in discovery)
    current_discovery = {json.loads(payload)["unique_id"]: json.loads(payload) for _, payload, _ in discovery}
    assert current_discovery["stroomvoorspeller_current_market_price"]["unit_of_measurement"] == "EUR/kWh"
    assert current_discovery["stroomvoorspeller_current_market_price"]["json_attributes_topic"] == "stroomvoorspeller/sensor/current_market_price/attributes"
    assert current_discovery["stroomvoorspeller_current_market_price"]["json_attributes_topic"] == "stroomvoorspeller/sensor/current_market_price/attributes"
    assert current_discovery["stroomvoorspeller_current_all_in_price"]["expire_after"] == 900

    slots = []
    for i in range(16):
        start = now + timedelta(minutes=15 * i)
        slots.append({"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(),
                      "price": .3 - i / 100, "status": "predicted"})
    current_prices = {"current_market_price": {"price": -.05, "start": now.isoformat(), "end": (now + timedelta(minutes=15)).isoformat(), "source": "Energy-Charts.info", "quality": "published", "fetched_at": now.isoformat()},
                      "current_all_in_price": {"price": .070, "start": now.isoformat(), "end": (now + timedelta(minutes=15)).isoformat(), "source": "Energy-Charts.info", "quality": "published", "fetched_at": now.isoformat()}}
    assert exporter.publish({}, {"slots": slots}, {"stale": False}, current_prices)
    states = {topic: payload for topic, payload, _ in transport.messages if "/sensor/" in topic}
    assert states["stroomvoorspeller/sensor/next_quarter_price"] == "0.29"
    assert states["stroomvoorspeller/sensor/cheapest_next_3h"] == "0.205"
    assert states["stroomvoorspeller/sensor/data_status"] == "fresh"
    assert states["stroomvoorspeller/sensor/current_market_price"] == "-0.05"
    assert states["stroomvoorspeller/sensor/current_all_in_price"] == "0.07"
    live_topics = {topic: retain for topic, _, retain in transport.messages if topic.endswith("/sensor/current_market_price") or topic.endswith("/sensor/current_all_in_price")}
    assert live_topics and not any(live_topics.values())
    assert '"status":"predicted"' in states["stroomvoorspeller/sensor/next_quarter_price/attributes"]
    assert '"contains_forecast":true' in states["stroomvoorspeller/sensor/cheapest_next_3h/attributes"]
    assert exporter.publish({}, {"slots": slots}, {"stale": False}, {})
    assert [payload for topic, payload, retain in transport.messages if topic.endswith("/sensor/current_market_price")][-1] == "None"


def test_stop_clears_retained_discovery_and_disconnects():
    transport = FakeTransport()
    exporter = MQTTExporter(enabled=True, service_loader=lambda: {}, transport_factory=lambda _: transport)
    assert exporter.start()
    exporter.stop(remove_entities=True)
    cleared = [m for m in transport.messages if m[0].endswith("/config") and m[1] == ""]
    assert len(cleared) == 5
    assert not transport.connected


def test_current_price_expires_at_exact_quarter_boundary():
    now = datetime(2026, 10, 6, 12, 14, 50, tzinfo=timezone.utc)
    transport = FakeTransport()
    exporter = MQTTExporter(enabled=True, service_loader=lambda: {}, transport_factory=lambda _: transport,
                             clock=lambda: now)
    current = {"current_market_price": {"price": .1, "start": "2026-10-06T12:00:00Z",
                                        "end": "2026-10-06T12:15:00Z", "source": "Energy-Charts.info",
                                        "quality": "published", "fetched_at": now.isoformat()}}
    assert exporter.publish({}, [], current_prices=current)
    config = [json.loads(payload) for topic, payload, _ in transport.messages
              if topic.endswith("current_market_price/config")][-1]
    assert config["expire_after"] == 10
    state = next((payload, retain) for topic, payload, retain in transport.messages
                 if topic.endswith("/sensor/current_market_price"))
    assert state == ("0.1", False)


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
