#!/usr/bin/env python3
"""
Monitor definitivo das linhas 0.167 e 167.1.

Parada: Via L2 Sul / SAUS, Quadra 5 — OAB / Galois
Sentido: Plano Piloto -> Guará
Alertas: aproximadamente 30 e 15 minutos antes
Fonte: serviço em tempo real utilizado pelo dfnoponto.com

O programa:
- lê os itinerários publicados nas páginas das linhas;
- localiza a parada OAB/Galois;
- conecta ao Socket.IO por HTTP polling;
- recebe as posições dos ônibus;
- projeta cada veículo no itinerário correto;
- estima o tempo restante;
- envia os alertas para todos os chat_ids do Telegram.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


SITE = "https://dfnoponto.com"
SOCKET_SERVER = "https://nopontoserver.onrender.com"
SOCKET_PATH = f"{SOCKET_SERVER}/socket.io/"
SOCKET_ORIGIN = SITE
RECORD_SEPARATOR = "\x1e"

EARTH_RADIUS_M = 6_371_000.0
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 90

# A 0.167 faz o percurso para o Guará no sentido IDA.
# Na 167.1, esse mesmo percurso aparece como VOLTA.
ROUTE_DEFINITIONS = {
    "0.167": {
        "direction": "IDA",
        "jsonld_key": "itinerary",
        "page": f"{SITE}/horario/0.167",
        # O JSON-LD da página resume o itinerário nas primeiras 20 paradas,
        # embora a OAB/Galois seja a parada 30 no trajeto completo.
        "target_position": 30,
        "target_name": (
            "Via L2 Sul - SAUS, Quadra 5 "
            "(Edifício Sede OAB / GALOIS)"
        ),
        "target_lat": -15.79733,
        "target_lon": -47.87610,
        "fallback_anchors": [
            (-15.7868, -47.8767),
            (-15.79733, -47.87610),
        ],
    },
    "167.1": {
        "direction": "VOLTA",
        "jsonld_key": "returnTrip",
        "page": f"{SITE}/horario/167.1",
        # No sentido Asa Norte -> Guará, a parada fica depois do trecho
        # da Esplanada. A posição é aproximada porque os sentidos têm
        # quantidades de paradas ligeiramente diferentes.
        "target_position": 34,
        "target_name": (
            "Via L2 Sul - SGAS 601 "
            "(Galois / Edifício Sede OAB)"
        ),
        "target_lat": -15.79733,
        "target_lon": -47.87610,
        "fallback_anchors": [
            (-15.7855, -47.8755),
            (-15.7995, -47.8605),
            (-15.7960, -47.8690),
            (-15.79733, -47.87610),
        ],
    },
}


@dataclass
class Config:
    monitor_start: str
    monitor_end: str
    weekdays: list[int]
    timezone: str
    alerts_minutes: list[int]
    stop_keywords: list[str]
    default_speed_kmh: float
    minimum_speed_kmh: float
    maximum_speed_kmh: float
    traffic_factor: float
    dwell_minutes_per_stop: float
    max_route_offset_m: float
    passed_margin_m: float
    stale_vehicle_minutes: int
    debug: bool


@dataclass
class Route:
    line: str
    direction: str
    points: list[tuple[float, float]]
    names: list[str]
    cumulative_m: list[float]
    stop_index: int
    stop_name: str
    stop_along_m: float


@dataclass
class VehicleState:
    along_m: float
    distance_to_stop_m: float
    observed_monotonic: float
    eta_minutes: float
    speed_kmh: float
    sent_alerts: set[int] = field(default_factory=set)
    last_seen_epoch: float = field(default_factory=time.time)


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Config(
        monitor_start=str(raw.get("monitor_start", "16:00")),
        monitor_end=str(raw.get("monitor_end", "21:00")),
        weekdays=[int(v) for v in raw.get("weekdays", [0, 1, 2, 3, 4])],
        timezone=str(raw.get("timezone", "America/Sao_Paulo")),
        alerts_minutes=sorted(
            [int(v) for v in raw.get("alerts_minutes", [30, 15])],
            reverse=True,
        ),
        stop_keywords=[
            str(v).lower()
            for v in raw.get("stop_keywords", ["galois", "oab"])
        ],
        default_speed_kmh=float(raw.get("default_speed_kmh", 23)),
        minimum_speed_kmh=float(raw.get("minimum_speed_kmh", 8)),
        maximum_speed_kmh=float(raw.get("maximum_speed_kmh", 55)),
        traffic_factor=float(raw.get("traffic_factor", 1.12)),
        dwell_minutes_per_stop=float(raw.get("dwell_minutes_per_stop", 0.22)),
        max_route_offset_m=float(raw.get("max_route_offset_m", 1200)),
        passed_margin_m=float(raw.get("passed_margin_m", 100)),
        stale_vehicle_minutes=int(raw.get("stale_vehicle_minutes", 10)),
        debug=bool(raw.get("debug", False)),
    )


def haversine_m(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(value))


def cumulative_distances(
    points: list[tuple[float, float]],
) -> list[float]:
    result = [0.0]
    for index in range(1, len(points)):
        result.append(
            result[-1]
            + haversine_m(
                points[index - 1][0],
                points[index - 1][1],
                points[index][0],
                points[index][1],
            )
        )
    return result


def local_xy(
    lat: float,
    lon: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    x = (
        math.radians(lon - ref_lon)
        * EARTH_RADIUS_M
        * math.cos(math.radians(ref_lat))
    )
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def project_on_route(
    lat: float,
    lon: float,
    route: Route,
    previous_along_m: float | None = None,
) -> tuple[float, float, int]:
    """
    Retorna:
    - distância acumulada no itinerário;
    - afastamento lateral do itinerário;
    - índice do segmento.

    Quando há cruzamentos no itinerário, a posição anterior ajuda a escolher
    o segmento coerente.
    """
    candidates: list[tuple[float, float, float, int]] = []

    for index in range(len(route.points) - 1):
        lat1, lon1 = route.points[index]
        lat2, lon2 = route.points[index + 1]

        x1, y1 = local_xy(lat1, lon1, lat, lon)
        x2, y2 = local_xy(lat2, lon2, lat, lon)

        vx = x2 - x1
        vy = y2 - y1
        segment_m = math.hypot(vx, vy)
        if segment_m <= 0:
            continue

        t = max(
            0.0,
            min(
                1.0,
                -(x1 * vx + y1 * vy) / (segment_m * segment_m),
            ),
        )

        px = x1 + t * vx
        py = y1 + t * vy
        offset_m = math.hypot(px, py)
        along_m = route.cumulative_m[index] + t * segment_m

        continuity_penalty = 0.0
        if previous_along_m is not None:
            continuity_penalty = min(
                3000.0,
                abs(along_m - previous_along_m) * 0.12,
            )

        score = offset_m + continuity_penalty
        candidates.append((score, along_m, offset_m, index))

    if not candidates:
        raise RuntimeError(f"Itinerário inválido para a linha {route.line}.")

    _, along_m, offset_m, segment_index = min(
        candidates,
        key=lambda item: item[0],
    )
    return along_m, offset_m, segment_index


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def extract_jsonld(html_text: str) -> list[Any]:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>'
        r'(.*?)</script>',
        html_text,
        flags=re.I | re.S,
    )

    parsed = []
    for block in blocks:
        try:
            parsed.append(json.loads(html.unescape(block).strip()))
        except json.JSONDecodeError:
            continue
    return parsed


def find_trip_data(
    document: Any,
    key: str,
) -> dict[str, Any] | None:
    for item in walk_dicts(document):
        candidate = item.get(key)
        if (
            isinstance(candidate, dict)
            and isinstance(candidate.get("itemListElement"), list)
        ):
            return candidate
    return None


def parse_trip_stops(trip: dict[str, Any]) -> tuple[
    list[tuple[float, float]],
    list[str],
]:
    records = sorted(
        trip.get("itemListElement", []),
        key=lambda item: int(item.get("position", 0)),
    )

    points: list[tuple[float, float]] = []
    names: list[str] = []

    for record in records:
        item = record.get("item") or {}
        geo = item.get("geo") or {}

        try:
            lat = float(geo["latitude"])
            lon = float(geo["longitude"])
        except (KeyError, TypeError, ValueError):
            continue

        name = str(item.get("name") or f"Parada {len(points) + 1}")
        points.append((lat, lon))
        names.append(name)

    if len(points) < 2:
        raise RuntimeError("O itinerário não contém paradas suficientes.")

    return points, names



def interpolate_fallback_path(
    start: tuple[float, float],
    anchors: list[tuple[float, float]],
    count: int,
) -> list[tuple[float, float]]:
    """
    Gera pontos intermediários ao longo de um caminho aproximado.

    O site publica o itinerário completo na página, mas o JSON-LD usado
    para obter coordenadas pode trazer somente as primeiras 20 paradas.
    Esta função completa o trecho restante até a OAB/Galois.
    """
    if count <= 0:
        return []

    path = [start, *anchors]
    segment_lengths = [
        haversine_m(
            path[index][0],
            path[index][1],
            path[index + 1][0],
            path[index + 1][1],
        )
        for index in range(len(path) - 1)
    ]
    total = sum(segment_lengths)

    if total <= 0:
        return [anchors[-1]] * count

    result: list[tuple[float, float]] = []

    for step in range(1, count + 1):
        desired = total * step / count
        accumulated = 0.0

        for index, segment_length in enumerate(segment_lengths):
            if desired <= accumulated + segment_length or index == len(segment_lengths) - 1:
                fraction = (
                    0.0
                    if segment_length <= 0
                    else (desired - accumulated) / segment_length
                )
                fraction = max(0.0, min(1.0, fraction))

                lat1, lon1 = path[index]
                lat2, lon2 = path[index + 1]
                result.append(
                    (
                        lat1 + (lat2 - lat1) * fraction,
                        lon1 + (lon2 - lon1) * fraction,
                    )
                )
                break

            accumulated += segment_length

    # Garante que o último ponto seja exatamente a parada configurada.
    result[-1] = anchors[-1]
    return result


def add_target_fallback(
    line: str,
    definition: dict[str, Any],
    points: list[tuple[float, float]],
    names: list[str],
) -> tuple[list[tuple[float, float]], list[str], int]:
    target_position = int(definition["target_position"])
    target_name = str(definition["target_name"])
    target = (
        float(definition["target_lat"]),
        float(definition["target_lon"]),
    )

    missing = max(1, target_position - len(points))
    anchors = [
        (float(lat), float(lon))
        for lat, lon in definition.get("fallback_anchors", [target])
    ]

    if not anchors or anchors[-1] != target:
        anchors.append(target)

    generated = interpolate_fallback_path(points[-1], anchors, missing)

    for index, point in enumerate(generated, start=1):
        points.append(point)
        if index == len(generated):
            names.append(target_name)
        else:
            names.append(
                f"Trecho intermediário aproximado {len(names) + 1}"
            )

    stop_index = len(points) - 1
    print(
        f"[itinerário] a página resumiu as coordenadas da linha {line}; "
        f"foi aplicado o ponto fixo da OAB/Galois na posição "
        f"{stop_index + 1}."
    )
    return points, names, stop_index

def load_route(
    line: str,
    definition: dict[str, str],
    config: Config,
) -> Route:
    response = requests.get(
        definition["page"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    response.raise_for_status()

    documents = extract_jsonld(response.text)
    trip = None
    for document in documents:
        trip = find_trip_data(document, definition["jsonld_key"])
        if trip:
            break

    if not trip:
        raise RuntimeError(
            f"Não encontrei o itinerário {definition['jsonld_key']} "
            f"da linha {line}."
        )

    points, names = parse_trip_stops(trip)

    stop_candidates = [
        index
        for index, name in enumerate(names)
        if any(keyword in name.lower() for keyword in config.stop_keywords)
    ]

    if stop_candidates:
        stop_index = stop_candidates[0]
    else:
        points, names, stop_index = add_target_fallback(
            line,
            definition,
            points,
            names,
        )

    cumulative = cumulative_distances(points)

    route = Route(
        line=line,
        direction=definition["direction"],
        points=points,
        names=names,
        cumulative_m=cumulative,
        stop_index=stop_index,
        stop_name=names[stop_index],
        stop_along_m=cumulative[stop_index],
    )

    print(
        f"[itinerário] linha {line} | sentido {route.direction} | "
        f"{len(points)} paradas | alvo nº {stop_index + 1}: "
        f"{route.stop_name}"
    )
    return route


def extract_coordinates(vehicle: dict[str, Any]) -> tuple[float, float] | None:
    location = vehicle.get("localizacao") or {}

    lat = location.get("latitude", location.get("lat"))
    lon = location.get("longitude", location.get("lng"))

    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def extract_speed(vehicle: dict[str, Any]) -> float | None:
    value = vehicle.get("velocidade")

    if isinstance(value, dict):
        value = value.get("valor")

    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None

    return speed if 0 <= speed <= 120 else None


def canonical_vehicle_number(vehicle: dict[str, Any]) -> str:
    number = str(vehicle.get("numero") or vehicle.get("key") or "desconhecido")
    return re.sub(r"^mb-", "", number, flags=re.I)


def detailed_score(vehicle: dict[str, Any]) -> int:
    return sum(
        1
        for key in ("horario", "velocidade", "direcao", "codigoImei", "valid")
        if key in vehicle
    )


def deduplicate_vehicles(
    vehicles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}

    for vehicle in vehicles:
        line = str(vehicle.get("linha") or "")
        number = canonical_vehicle_number(vehicle)
        key = (line, number)

        current = selected.get(key)
        if current is None or detailed_score(vehicle) > detailed_score(current):
            selected[key] = vehicle

    return list(selected.values())


def is_stale(vehicle: dict[str, Any], config: Config) -> bool:
    raw = vehicle.get("horario")
    if raw is None:
        return False

    try:
        timestamp = float(raw)
    except (TypeError, ValueError):
        return False

    if timestamp > 10_000_000_000:
        timestamp /= 1000.0

    age_seconds = time.time() - timestamp
    return age_seconds > config.stale_vehicle_minutes * 60


def parse_clock(value: str) -> clock_time:
    hour, minute = value.split(":", 1)
    return clock_time(int(hour), int(minute))


class Telegram:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_ids = [
            item.strip()
            for item in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
            if item.strip()
        ]

    def validate(self) -> None:
        if not self.token:
            raise RuntimeError("Secret TELEGRAM_BOT_TOKEN não configurado.")
        if not self.chat_ids:
            raise RuntimeError("Secret TELEGRAM_CHAT_IDS não configurado.")

    def send(self, text: str) -> None:
        self.validate()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        failures = []

        for chat_id in self.chat_ids:
            try:
                response = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
                response.raise_for_status()
            except requests.RequestException as error:
                failures.append(f"{chat_id}: {error}")

        if failures:
            raise RuntimeError(
                "Falha ao enviar ao Telegram: " + " | ".join(failures)
            )


class PollingSocket:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Origin": SOCKET_ORIGIN,
                "Referer": SOCKET_ORIGIN + "/",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                ),
                "Accept": "*/*",
            }
        )
        self.sid: str | None = None

    def params(self) -> dict[str, str]:
        values = {
            "EIO": "4",
            "transport": "polling",
            "t": str(int(time.time() * 1000)),
        }
        if self.sid:
            values["sid"] = self.sid
        return values

    def get(self) -> str:
        response = self.session.get(
            SOCKET_PATH,
            params=self.params(),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        response.raise_for_status()
        return response.text

    def post(self, payload: str) -> None:
        response = self.session.post(
            SOCKET_PATH,
            params=self.params(),
            data=payload.encode("utf-8"),
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        response.raise_for_status()

    @staticmethod
    def split(payload: str) -> list[str]:
        return [
            packet
            for packet in payload.split(RECORD_SEPARATOR)
            if packet
        ]

    def connect(self) -> None:
        initial = self.split(self.get())
        open_packet = next(
            (packet for packet in initial if packet.startswith("0")),
            None,
        )
        if not open_packet:
            raise RuntimeError("Handshake Engine.IO inválido.")

        info = json.loads(open_packet[1:])
        self.sid = str(info["sid"])
        self.post("40")

        deadline = time.time() + 60
        while time.time() < deadline:
            for packet in self.poll():
                if packet.startswith("40"):
                    print(f"[fonte] Socket conectado. SID {self.sid}")
                    return
                if packet.startswith("44"):
                    raise RuntimeError(
                        "O servidor recusou o namespace: " + packet
                    )

        raise TimeoutError("O Socket.IO não confirmou a conexão.")

    def emit(self, event: str, data: Any) -> None:
        payload = "42" + json.dumps(
            [event, data],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.post(payload)

    def poll(self) -> list[str]:
        packets = self.split(self.get())
        for packet in packets:
            if packet == "2":
                self.post("3")
        return packets


def parse_event(packet: str) -> tuple[str, Any] | None:
    if not packet.startswith("42"):
        return None

    try:
        content = json.loads(packet[2:])
    except json.JSONDecodeError:
        return None

    if not isinstance(content, list) or len(content) < 2:
        return None

    return str(content[0]), content[1]


class Monitor:
    def __init__(
        self,
        config: Config,
        telegram: Telegram,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.timezone = ZoneInfo(config.timezone)
        self.routes: dict[str, Route] = {}
        self.states: dict[str, VehicleState] = {}

    def initialize_routes(self) -> None:
        for line, definition in ROUTE_DEFINITIONS.items():
            self.routes[line] = load_route(line, definition, self.config)

    def vehicle_speed(
        self,
        previous: VehicleState | None,
        along_m: float,
        reported_speed: float | None,
        now_monotonic: float,
    ) -> float:
        observed_speed = None

        if previous:
            seconds = now_monotonic - previous.observed_monotonic
            movement_m = along_m - previous.along_m

            if seconds >= 10 and movement_m > 15:
                candidate = movement_m / seconds * 3.6
                if (
                    self.config.minimum_speed_kmh
                    <= candidate
                    <= self.config.maximum_speed_kmh
                ):
                    observed_speed = candidate

        values = []
        if observed_speed is not None:
            values.extend([observed_speed, observed_speed])
        if (
            reported_speed is not None
            and self.config.minimum_speed_kmh
            <= reported_speed
            <= self.config.maximum_speed_kmh
        ):
            values.append(reported_speed)
        if previous and (
            self.config.minimum_speed_kmh
            <= previous.speed_kmh
            <= self.config.maximum_speed_kmh
        ):
            values.append(previous.speed_kmh)

        if values:
            speed = sum(values) / len(values)
        else:
            speed = self.config.default_speed_kmh

        return max(
            self.config.minimum_speed_kmh,
            min(self.config.maximum_speed_kmh, speed),
        )

    def estimate_eta(
        self,
        route: Route,
        distance_m: float,
        segment_index: int,
        speed_kmh: float,
    ) -> float:
        travel_minutes = (
            distance_m / 1000.0 / speed_kmh * 60.0
            * self.config.traffic_factor
        )

        remaining_stops = max(
            0,
            route.stop_index - segment_index - 1,
        )
        dwell_minutes = (
            remaining_stops * self.config.dwell_minutes_per_stop
        )
        return travel_minutes + dwell_minutes

    def choose_alert(
        self,
        previous_eta: float | None,
        current_eta: float,
        already_sent: set[int],
    ) -> int | None:
        """
        Escolhe somente o marco mais próximo do ETA atual.

        Exemplos:
        - ETA 31 min -> alerta de 30;
        - ETA 16,8 min -> alerta de 15;
        - ETA 10 min -> nenhum alerta atrasado.
        """
        candidates: list[tuple[float, int]] = []

        for threshold in self.config.alerts_minutes:
            if threshold in already_sent:
                continue

            # Tolerância proporcional: 4,5 min para o alerta de 30
            # e 3 min para o alerta de 15.
            tolerance = max(3.0, threshold * 0.15)
            difference = abs(current_eta - threshold)

            # Primeira posição observada: envia apenas se o ETA estiver
            # realmente próximo de um dos marcos.
            if previous_eta is None:
                if difference <= tolerance:
                    candidates.append((difference, threshold))
                continue

            # Posições seguintes: dispara ao atravessar o marco ou quando
            # estiver dentro da tolerância, sem mandar alerta muito atrasado.
            crossed = previous_eta > threshold >= current_eta
            near = difference <= tolerance
            not_too_late = current_eta >= threshold - tolerance

            if (crossed or near) and not_too_late:
                candidates.append((difference, threshold))

        if not candidates:
            return None

        # Escolhe o marco numericamente mais próximo do ETA atual.
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][1]

    def process_vehicle(
        self,
        vehicle: dict[str, Any],
        allow_alerts: bool = True,
    ) -> None:
        line = str(vehicle.get("linha") or "")
        route = self.routes.get(line)
        if route is None:
            return

        direction = str(vehicle.get("sentido") or "").upper()
        if direction != route.direction:
            return

        if vehicle.get("valid") is False:
            return
        if is_stale(vehicle, self.config):
            return

        coordinates = extract_coordinates(vehicle)
        if coordinates is None:
            return

        number = canonical_vehicle_number(vehicle)
        state_key = f"{line}|{number}"
        previous = self.states.get(state_key)

        lat, lon = coordinates
        along_m, offset_m, segment_index = project_on_route(
            lat,
            lon,
            route,
            previous.along_m if previous else None,
        )

        if offset_m > self.config.max_route_offset_m:
            if self.config.debug:
                print(
                    f"[ignorado] {line}/{number}: "
                    f"{offset_m:.0f} m fora do itinerário"
                )
            return

        distance_m = route.stop_along_m - along_m

        # O veículo já chegou ou passou da OAB.
        if distance_m <= self.config.passed_margin_m:
            return

        now_monotonic = time.monotonic()
        speed_kmh = self.vehicle_speed(
            previous,
            along_m,
            extract_speed(vehicle),
            now_monotonic,
        )
        eta_minutes = self.estimate_eta(
            route,
            distance_m,
            segment_index,
            speed_kmh,
        )

        sent_alerts = set(previous.sent_alerts) if previous else set()
        alert = self.choose_alert(
            previous.eta_minutes if previous else None,
            eta_minutes,
            sent_alerts,
        )

        if self.config.debug:
            print(
                f"[posição] linha={line} veículo={number} "
                f"sentido={direction} distância={distance_m/1000:.2f} km "
                f"velocidade={speed_kmh:.1f} km/h ETA={eta_minutes:.1f} min "
                f"fora_da_rota={offset_m:.0f} m"
            )

        if alert is not None and allow_alerts:
            rounded_eta = max(1, round(eta_minutes))
            message = (
                f"🚌 Linha {line} em direção ao Guará\n\n"
                f"⏱️ Alerta de aproximadamente {alert} minutos.\n"
                f"Estimativa atual: {rounded_eta} min para a parada "
                f"OAB/Galois, na L2 Sul.\n\n"
                f"Veículo: {number}\n"
                f"Distância estimada pelo itinerário: "
                f"{distance_m/1000:.1f} km\n\n"
                f"⚠️ O tempo é aproximado e pode variar conforme o trânsito, "
                f"as paradas e a atualização do GPS."
            )
            self.telegram.send(message)
            sent_alerts.add(alert)
            print(
                f"[telegram] alerta {alert} min enviado | "
                f"linha {line} | veículo {number} | ETA {eta_minutes:.1f}"
            )

        self.states[state_key] = VehicleState(
            along_m=along_m,
            distance_to_stop_m=distance_m,
            observed_monotonic=now_monotonic,
            eta_minutes=eta_minutes,
            speed_kmh=speed_kmh,
            sent_alerts=sent_alerts,
            last_seen_epoch=time.time(),
        )

    def process_positions(
        self,
        data: Any,
        allow_alerts: bool = True,
    ) -> None:
        if not isinstance(data, list):
            return

        vehicles = [
            item for item in data
            if isinstance(item, dict)
        ]

        for vehicle in deduplicate_vehicles(vehicles):
            try:
                self.process_vehicle(vehicle, allow_alerts=allow_alerts)
            except Exception as error:
                print(
                    f"[aviso] veículo não processado: {error}",
                    file=sys.stderr,
                )

        cutoff = time.time() - 4 * 60 * 60
        self.states = {
            key: state
            for key, state in self.states.items()
            if state.last_seen_epoch >= cutoff
        }

    def connect_source(self) -> PollingSocket:
        socket = PollingSocket()
        socket.connect()

        for line in ROUTE_DEFINITIONS:
            socket.emit("linha", line)
            print(f"[fonte] linha solicitada: {line}")

        return socket

    def run_source_test(self, seconds: int = 120) -> int:
        self.initialize_routes()
        socket = self.connect_source()
        deadline = time.time() + seconds
        nonempty_events = 0

        while time.time() < deadline:
            for packet in socket.poll():
                event = parse_event(packet)
                if not event:
                    continue

                name, data = event
                if name != "posicoes":
                    continue

                count = len(data) if isinstance(data, list) else 0
                print(f"[teste] evento posicoes: {count} veículo(s)")

                if count:
                    nonempty_events += 1
                    self.process_positions(data, allow_alerts=False)

                if nonempty_events >= 1:
                    print(
                        "[teste] fonte em tempo real funcionando. "
                        "Nenhum alerta foi enviado no modo de teste."
                    )
                    return 0

        print(
            "[teste] conexão aceita, mas não houve veículo ativo "
            "durante o período."
        )
        return 0

    def run(
        self,
        force_minutes: int | None = None,
    ) -> None:
        self.telegram.validate()
        self.initialize_routes()

        force_deadline = (
            time.time() + force_minutes * 60
            if force_minutes
            else None
        )

        while True:
            now = datetime.now(self.timezone)

            if force_deadline and time.time() >= force_deadline:
                print("[monitor] período manual encerrado.")
                return

            if not force_deadline:
                if now.weekday() not in self.config.weekdays:
                    print("[monitor] dia fora da configuração.")
                    return

                start = parse_clock(self.config.monitor_start)
                end = parse_clock(self.config.monitor_end)
                current = now.time().replace(tzinfo=None)

                if current < start:
                    print(
                        f"[monitor] aguardando {self.config.monitor_start}..."
                    )
                    time.sleep(min(60, max(
                        1,
                        int(
                            (
                                now.replace(
                                    hour=start.hour,
                                    minute=start.minute,
                                    second=0,
                                    microsecond=0,
                                )
                                - now
                            ).total_seconds()
                        ),
                    )))
                    continue

                if current > end:
                    print("[monitor] fim do intervalo diário.")
                    return

            try:
                socket = self.connect_source()

                while True:
                    if force_deadline and time.time() >= force_deadline:
                        return

                    now = datetime.now(self.timezone)
                    if (
                        not force_deadline
                        and now.time().replace(tzinfo=None)
                        > parse_clock(self.config.monitor_end)
                    ):
                        print("[monitor] fim do intervalo diário.")
                        return

                    for packet in socket.poll():
                        event = parse_event(packet)
                        if not event:
                            continue

                        name, data = event
                        if name == "posicoes":
                            self.process_positions(data)

            except (
                requests.RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
            ) as error:
                print(
                    f"[fonte] conexão interrompida: {error}. "
                    f"Nova tentativa em 20 segundos.",
                    file=sys.stderr,
                )
                time.sleep(20)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor das linhas 0.167 e 167.1."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Caminho do config.json.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Envia uma mensagem de teste.",
    )
    parser.add_argument(
        "--test-source",
        action="store_true",
        help="Testa itinerários e fonte em tempo real.",
    )
    parser.add_argument(
        "--force-minutes",
        type=int,
        help="Executa imediatamente por N minutos.",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        telegram = Telegram()

        if args.test_telegram:
            telegram.send(
                "✅ Monitor definitivo configurado.\n\n"
                "Os alertas das linhas 0.167 e 167.1 serão enviados "
                "para este Telegram."
            )
            print("Mensagem enviada para todos os destinatários.")
            return 0

        monitor = Monitor(config, telegram)

        if args.test_source:
            return monitor.run_source_test()

        monitor.run(force_minutes=args.force_minutes)
        return 0

    except KeyboardInterrupt:
        print("\nEncerrado.")
        return 130
    except Exception as error:
        print(f"ERRO: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
