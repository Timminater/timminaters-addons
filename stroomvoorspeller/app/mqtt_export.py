"""Optional MQTT discovery export for Home Assistant.

The exporter is deliberately independent of the HTTP backend. The caller owns
its lifecycle and supplies dashboard/timeline snapshots via ``publish``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib.request import Request, urlopen

LOG = logging.getLogger("stroomvoorspeller.mqtt")
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "stroomvoorspeller"
DEVICE = {
    "identifiers": ["stroomvoorspeller"],
    "name": "Stroomvoorspeller",
    "manufacturer": "Timminater",
    "model": "Lokale kwartierprijsprognose",
}


class Transport(Protocol):
    def connect(self) -> None: ...
    def publish(self, topic: str, payload: str, retain: bool = True) -> None: ...
    def disconnect(self) -> None: ...


def discover_mqtt_service(token: str, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    """Read broker connection details from Supervisor's services API."""
    if not token:
        raise RuntimeError("Supervisor token unavailable")
    req = Request("http://supervisor/services/mqtt",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with opener(req, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    # Supervisor APIs may wrap service details in {data: ...}.
    service = body.get("data", body) if isinstance(body, Mapping) else {}
    if not isinstance(service, Mapping) or not service.get("host") or not service.get("port"):
        raise RuntimeError("MQTT service is not configured")
    return dict(service)


class PahoTransport:
    """Small Paho adapter; Paho supplies reconnect handling after connection."""
    def __init__(self, service: Mapping[str, Any], client_id: str = "stroomvoorspeller"):
        import paho.mqtt.client as mqtt

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        username, password = service.get("username"), service.get("password")
        if username:
            self._client.username_pw_set(str(username), str(password or ""))
        if bool(service.get("ssl")) or str(service.get("protocol", "")).lower().startswith("mqtts"):
            self._client.tls_set()
        self._host, self._port = str(service["host"]), int(service["port"])
        self._connected = threading.Event()
        self._reconnect_callback: Callable[[], None] | None = None
        self._client.will_set(f"{BASE_TOPIC}/availability", "offline", qos=1, retain=True)

        def on_connect(_client, _userdata, _flags, reason, _properties):
            if getattr(reason, "value", reason) == 0:
                first = not self._connected.is_set()
                self._connected.set()
                if not first and self._reconnect_callback:
                    self._reconnect_callback()

        self._client.on_connect = on_connect

    def set_reconnect_callback(self, callback: Callable[[], None]) -> None:
        self._reconnect_callback = callback

    def connect(self) -> None:
        self._client.connect(self._host, self._port, keepalive=45)
        self._client.loop_start()
        if not self._connected.wait(8):
            self._client.loop_stop()
            raise RuntimeError("MQTT broker connection timed out")

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        result = self._client.publish(topic, payload, qos=1, retain=retain)
        if result.rc != 0:
            raise RuntimeError("MQTT publish failed")

    def disconnect(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()


class MQTTExporter:
    """Publish read-only price sensors; safe to leave disabled or unavailable."""
    SENSOR_SPECS = (
        ("next_quarter_price", "Prijs volgend kwartier", "EUR/kWh", "mdi:flash-outline"),
        ("cheapest_next_3h", "Goedkoopste 3 uur", "EUR/kWh", "mdi:clock-check-outline"),
        ("data_status", "Datastatus", None, "mdi:database-clock-outline"),
    )

    def __init__(self, enabled: bool = False, token: str | None = None,
                 service_loader: Callable[[], Mapping[str, Any]] | None = None,
                 transport_factory: Callable[[Mapping[str, Any]], Transport] = PahoTransport,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.enabled = enabled
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self.service_loader = service_loader or (lambda: discover_mqtt_service(self.token))
        self.transport_factory = transport_factory
        self.clock = clock
        self.transport: Transport | None = None
        self._lock = threading.RLock()
        self._unit: str | None = None
        self._last_values: dict[str, str] = {}

    def start(self) -> bool:
        """Connect and announce sensors. Returns false when disabled/unavailable."""
        with self._lock:
            if not self.enabled:
                return False
            if self.transport is not None:
                return True
            try:
                transport = self.transport_factory(self.service_loader())
                self.transport = transport
                if hasattr(transport, "set_reconnect_callback"):
                    transport.set_reconnect_callback(self._after_reconnect)
                transport.connect()
                self._send( f"{BASE_TOPIC}/availability", "online")
                self._publish_discovery()
                return True
            except Exception:
                # A partial discovery announcement must not leave retained ghosts.
                if "transport" in locals():
                    for key, *_ in self.SENSOR_SPECS:
                        try:
                            transport.publish(f"{DISCOVERY_PREFIX}/sensor/{BASE_TOPIC}/{key}/config", "", retain=True)
                        except Exception:
                            pass
                    try:
                        transport.disconnect()
                    except Exception:
                        pass
                self.transport = None
                # Never include service response, username, password, or token in logs.
                LOG.info("MQTT export unavailable; continuing without MQTT")
                return False

    def publish(self, dashboard: Mapping[str, Any] | None,
                timeline: Mapping[str, Any] | list[Mapping[str, Any]] | None,
                status: Mapping[str, Any] | None = None) -> bool:
        """Publish latest dashboard/timeline snapshot and optional backend status."""
        with self._lock:
            if self.transport is None and not self.start():
                return False
            slots = timeline.get("slots", []) if isinstance(timeline, Mapping) else timeline or []
            now = self.clock().astimezone(timezone.utc)
            future = []
            for slot in slots:
                try:
                    start = _parse_time(slot.get("start"))
                    if start >= now and slot.get("price") is not None and slot.get("status") != "missing":
                        future.append((start, float(slot["price"]), slot))
                except (TypeError, ValueError, OverflowError):
                    continue
            future.sort(key=lambda item: item[0])
            next_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + timedelta(minutes=15)
            if future and future[0][2].get("unit") in {"EUR/kWh", "EUR/MWh", "€/kWh", "€/MWh"}:
                unit = future[0][2]["unit"]
                self._unit = {"€/kWh": "EUR/kWh", "€/MWh": "EUR/MWh"}.get(unit, unit)
                try:
                    self._publish_discovery()
                except Exception:
                    LOG.info("MQTT discovery update unavailable")
            next_slot = next((item for item in future if item[0] == next_start), None)
            next_price: float | None = next_slot[1] if next_slot else None
            next_intervals = [item for item in future if item[0] >= next_start]
            cheapest: float | None = None
            cheapest_window = None
            for index in range(len(next_intervals)):
                chunk = next_intervals[index:index + 12]
                if len(chunk) != 12:
                    continue
                if all(chunk[i][0] - chunk[i - 1][0] == timedelta(minutes=15)
                       for i in range(1, 12)):
                    average = sum(item[1] for item in chunk) / 12
                    if cheapest is None or average < cheapest:
                        cheapest = average
                        cheapest_window = chunk
            stale_value = None
            source = status or dashboard or {}
            if isinstance(source.get("stale"), bool):
                stale_value = "stale" if source["stale"] else "fresh"
            values = {
                "next_quarter_price": _as_number(next_price),
                "cheapest_next_3h": _as_number(cheapest),
                "data_status": stale_value or "unknown",
            }
            try:
                self._send(f"{BASE_TOPIC}/availability", "online")
                for key, value in values.items():
                    self._send(f"{BASE_TOPIC}/sensor/{key}", "" if value is None else str(value))
                    self._last_values[key] = "" if value is None else str(value)
                next_attrs = ({"start": _iso(next_slot[0]),
                               "end": _iso(next_slot[0] + timedelta(minutes=15)),
                               "status": next_slot[2].get("status")}
                              if next_slot else {})
                self._send(f"{BASE_TOPIC}/sensor/next_quarter_price/attributes",
                           json.dumps(next_attrs, separators=(",", ":")))
                attrs = ({"start": _iso(cheapest_window[0][0]),
                          "end": _iso(cheapest_window[-1][0] + timedelta(minutes=15)),
                          "contains_forecast": any(item[2].get("status") == "predicted"
                                                   for item in cheapest_window)}
                         if cheapest_window else {})
                self._send(f"{BASE_TOPIC}/sensor/cheapest_next_3h/attributes", json.dumps(attrs, separators=(",", ":")))
                return True
            except Exception:
                LOG.info("MQTT publish unavailable; will retry on next update")
                return False

    def stop(self, remove_entities: bool = False) -> None:
        """Mark sensors offline; clear retained discovery only when explicitly disabled."""
        with self._lock:
            transport, self.transport = self.transport, None
            if transport is None:
                return
            if remove_entities:
                for key, *_ in self.SENSOR_SPECS:
                    topic = f"{DISCOVERY_PREFIX}/sensor/{BASE_TOPIC}/{key}/config"
                    try:
                        transport.publish(topic, "", retain=True)
                    except Exception:
                        pass
            try:
                transport.publish(f"{BASE_TOPIC}/availability", "offline", retain=True)
            except Exception:
                pass
            try:
                transport.disconnect()
            except Exception:
                pass

    def _send(self, topic: str, payload: str) -> None:
        assert self.transport is not None
        self.transport.publish(topic, payload, retain=True)

    def _publish_discovery(self) -> None:
        for key, name, has_unit, icon in self.SENSOR_SPECS:
            config = {
                "name": name, "unique_id": f"stroomvoorspeller_{key}",
                "state_topic": f"{BASE_TOPIC}/sensor/{key}",
                "availability_topic": f"{BASE_TOPIC}/availability",
                "payload_available": "online", "payload_not_available": "offline",
                "device": DEVICE, "icon": icon,
            }
            if has_unit and self._unit:
                config.update(unit_of_measurement=self._unit, state_class="measurement")
            if key in {"next_quarter_price", "cheapest_next_3h"}:
                config["json_attributes_topic"] = f"{BASE_TOPIC}/sensor/{key}/attributes"
            topic = f"{DISCOVERY_PREFIX}/sensor/{BASE_TOPIC}/{key}/config"
            self._send(topic, json.dumps(config, separators=(",", ":")))

    def _after_reconnect(self) -> None:
        with self._lock:
            try:
                self._send(f"{BASE_TOPIC}/availability", "online")
                self._publish_discovery()
                for key, value in self._last_values.items():
                    self._send(f"{BASE_TOPIC}/sensor/{key}", value)
            except Exception:
                LOG.info("MQTT reconnect refresh unavailable")


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("missing timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_number(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
