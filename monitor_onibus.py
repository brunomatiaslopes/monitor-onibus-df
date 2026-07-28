#!/usr/bin/env python3
"""
Monitor de ônibus do DF com alertas pelo Telegram.

Linhas padrão: 167 (também aceita 0.167) e 167.1
Parada: L2 Sul | SAUS (OAB / Colégio Galois)
Alertas: aproximadamente 30 e 15 minutos antes.

A estimativa é experimental: usa a posição dos veículos, o itinerário publicado
pela Semob, a velocidade observada e um fator de trânsito configurável.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


WFS_URL = "https://geoserver.semob.df.gov.br/geoserver/semob/ows"

POSITION_LAYER = "semob:ultima_posicao"
STOP_LAYER = "semob:ponto_parada_v2025"
ROUTE_LAYER = "semob:itinerario_espacial"

REQUEST_CONNECT_TIMEOUT = 15
REQUEST_READ_TIMEOUT = 90
EARTH_RADIUS_M = 6_371_000.0


def build_http_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=3,
        status=3,
        backoff_factor=3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "monitor-onibus-df/1.1 "
                "(consulta de dados publicos da Semob-DF)"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


HTTP = build_http_session()

LINE_ALIASES = (
    "linha", "numero_linha", "num_linha", "nr_linha", "codigo_linha",
    "cod_linha", "linha_numero", "servico", "numero", "codigo",
)
VEHICLE_ALIASES = (
    "prefixo", "veiculo", "numero_veiculo", "num_veiculo", "nr_veiculo",
    "codigo_veiculo", "cod_veiculo", "placa",
)
SPEED_ALIASES = (
    "velocidade", "velocidade_kmh", "veloc", "speed", "vel",
)
TIMESTAMP_ALIASES = (
    "data_hora", "datahora", "horario", "timestamp", "dh_posicao",
    "ultima_atualizacao", "data_posicao", "instante",
)
STOP_NAME_ALIASES = (
    "nome", "denominacao", "descricao", "desc_parada", "nome_parada",
    "endereco", "referencia", "ponto", "logradouro",
)
LAT_ALIASES = ("latitude", "lat", "y")
LON_ALIASES = ("longitude", "lon", "lng", "x")


@dataclass
class Config:
    lines: list[str]
    stop_keywords: list[str]
    stop_center_lat: float
    stop_center_lon: float
    stop_search_radius_m: float
    alerts_minutes: list[int]
    monitor_start: str
    monitor_end: str
    weekdays: list[int]
    poll_seconds: int
    timezone: str
    default_speed_kmh: float
    traffic_factor: float
    straight_line_factor: float
    minimum_movement_m: float
    stop_passed_radius_m: float
    max_vehicle_age_minutes: int
    debug: bool


@dataclass
class RouteProjection:
    remaining_m: float
    bus_offset_m: float
    stop_offset_m: float
    total_m: float


@dataclass
class VehicleObservation:
    line: str
    vehicle_id: str
    lat: float
    lon: float
    straight_distance_m: float
    remaining_m: float
    observed_at: datetime
    source_speed_kmh: float | None


def load_config(path: str | Path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Config(
        lines=[str(v) for v in raw["lines"]],
        stop_keywords=[str(v) for v in raw["stop"]["keywords"]],
        stop_center_lat=float(raw["stop"]["approximate_lat"]),
        stop_center_lon=float(raw["stop"]["approximate_lon"]),
        stop_search_radius_m=float(raw["stop"].get("search_radius_m", 3000)),
        alerts_minutes=sorted(
            [int(v) for v in raw.get("alerts_minutes", [30, 15])],
            reverse=True,
        ),
        monitor_start=str(raw.get("monitor_start", "16:00")),
        monitor_end=str(raw.get("monitor_end", "21:00")),
        weekdays=[int(v) for v in raw.get("weekdays", [0, 1, 2, 3, 4])],
        poll_seconds=max(30, int(raw.get("poll_seconds", 60))),
        timezone=str(raw.get("timezone", "America/Sao_Paulo")),
        default_speed_kmh=float(raw.get("default_speed_kmh", 25)),
        traffic_factor=float(raw.get("traffic_factor", 1.15)),
        straight_line_factor=float(raw.get("straight_line_factor", 1.35)),
        minimum_movement_m=float(raw.get("minimum_movement_m", 35)),
        stop_passed_radius_m=float(raw.get("stop_passed_radius_m", 120)),
        max_vehicle_age_minutes=int(raw.get("max_vehicle_age_minutes", 15)),
        debug=bool(raw.get("debug", False)),
    )


def norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def norm_line(value: Any) -> str:
    """Normaliza 0.167, 167, 0167 e pequenas variações."""
    text = str(value).strip().replace(",", ".")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^[A-Za-z]+", "", text)
    if re.fullmatch(r"0+\d+\.\d+", text):
        text = text.lstrip("0")
        if text.startswith("."):
            text = "0" + text
    elif re.fullmatch(r"0+\d+", text):
        text = text.lstrip("0") or "0"
    # A linha 0.167 é tratada como 167; 167.1 permanece 167.1.
    if re.fullmatch(r"0\.\d{3}", text):
        text = text[2:].lstrip("0") or "0"
    return text


def property_lookup(properties: dict[str, Any], aliases: Iterable[str]) -> Any | None:
    normalized = {norm_text(k): v for k, v in properties.items()}
    for alias in aliases:
        if norm_text(alias) in normalized:
            value = normalized[norm_text(alias)]
            if value not in (None, ""):
                return value
    return None


def find_property_name(features: list[dict[str, Any]], aliases: Iterable[str]) -> str | None:
    alias_set = {norm_text(a) for a in aliases}
    for feature in features:
        for key in feature.get("properties", {}):
            if norm_text(key) in alias_set:
                return key
    return None


def wfs_get(
    layer: str,
    *,
    max_features: int,
    cql_filter: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "maxFeatures": str(max_features),
    }
    if cql_filter:
        params["CQL_FILTER"] = cql_filter
    if bbox:
        min_lon, min_lat, max_lon, max_lat = bbox
        params["BBOX"] = (
            f"{min_lon:.7f},{min_lat:.7f},"
            f"{max_lon:.7f},{max_lat:.7f},EPSG:4326"
        )

    try:
        response = HTTP.get(
            WFS_URL,
            params=params,
            timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"A Semob não respondeu à consulta da camada {layer}. "
            f"Tentativas automáticas esgotadas: {exc}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        trecho = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"A Semob devolveu uma resposta que não é JSON para {layer}: {trecho}"
        ) from exc

    if not isinstance(data, dict) or "features" not in data:
        raise RuntimeError(f"Resposta inesperada da camada {layer}.")
    return data


def line_variants(lines: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for original in lines:
        text = str(original).strip()
        normalized = norm_line(text)
        result.update({text, normalized})
        if normalized == "167":
            result.add("0.167")
        if re.fullmatch(r"\d+\.\d+", normalized):
            result.add(normalized)
    return {v for v in result if v}


def extract_line(feature: dict[str, Any], field_name: str | None = None) -> str | None:
    props = feature.get("properties", {})
    value = props.get(field_name) if field_name else property_lookup(props, LINE_ALIASES)
    if value in (None, ""):
        # Último recurso: procura valor com formato típico de linha.
        for candidate in props.values():
            text = str(candidate).strip().replace(",", ".")
            if re.fullmatch(r"0?\d{1,3}(?:\.\d{1,2})?", text):
                value = candidate
                break
    return norm_line(value) if value not in (None, "") else None


def fetch_features_for_lines(
    layer: str,
    lines: list[str],
    *,
    all_limit: int,
    debug: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    target = {norm_line(v) for v in lines}
    sample = wfs_get(layer, max_features=100).get("features", [])
    line_field = find_property_name(sample, LINE_ALIASES)

    if debug:
        print(f"[debug] {layer}: campo provável da linha = {line_field!r}")

    # Primeiro tenta filtrar no servidor para reduzir o tráfego.
    if line_field:
        variants = sorted(line_variants(lines))
        quoted_values = ",".join("'" + v.replace("'", "''") + "'" for v in variants)
        filters = [f"{line_field} IN ({quoted_values})"]
        numeric_values = [v for v in variants if re.fullmatch(r"\d+(?:\.\d+)?", v)]
        if numeric_values:
            filters.append(f"{line_field} IN ({','.join(numeric_values)})")

        for cql in filters:
            try:
                items = wfs_get(layer, max_features=all_limit, cql_filter=cql).get("features", [])
                filtered = [f for f in items if extract_line(f, line_field) in target]
                if filtered:
                    return filtered, line_field
            except (requests.RequestException, ValueError, RuntimeError):
                pass

    # Compatibilidade: baixa a camada e filtra localmente.
    items = wfs_get(layer, max_features=all_limit).get("features", [])
    filtered = [f for f in items if extract_line(f, line_field) in target]
    return filtered, line_field


def point_from_feature(feature: dict[str, Any]) -> tuple[float, float] | None:
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates")
    geo_type = geometry.get("type")

    if geo_type == "Point" and isinstance(coords, list) and len(coords) >= 2:
        lon, lat = float(coords[0]), float(coords[1])
        return lat, lon
    if geo_type == "MultiPoint" and coords and len(coords[0]) >= 2:
        lon, lat = float(coords[0][0]), float(coords[0][1])
        return lat, lon

    props = feature.get("properties", {})
    lat = property_lookup(props, LAT_ALIASES)
    lon = property_lookup(props, LON_ALIASES)
    if lat not in (None, "") and lon not in (None, ""):
        try:
            return float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
        except ValueError:
            return None
    return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def all_text(properties: dict[str, Any]) -> str:
    return " ".join(norm_text(v).replace("_", " ") for v in properties.values() if v is not None)


def discover_stop(config: Config) -> tuple[dict[str, Any], float, float, str]:
    # Em vez de baixar todas as paradas do DF, consulta apenas a região da OAB.
    lat_delta = config.stop_search_radius_m / 111_320.0
    lon_scale = max(0.2, math.cos(math.radians(config.stop_center_lat)))
    lon_delta = config.stop_search_radius_m / (111_320.0 * lon_scale)

    bbox = (
        config.stop_center_lon - lon_delta,
        config.stop_center_lat - lat_delta,
        config.stop_center_lon + lon_delta,
        config.stop_center_lat + lat_delta,
    )

    features = wfs_get(
        STOP_LAYER,
        max_features=1_000,
        bbox=bbox,
    ).get("features", [])

    wanted = [norm_text(v).replace("_", " ") for v in config.stop_keywords]

    candidates: list[tuple[int, float, dict[str, Any], float, float, str]] = []
    for feature in features:
        point = point_from_feature(feature)
        if not point:
            continue
        lat, lon = point
        distance = haversine_m(
            config.stop_center_lat,
            config.stop_center_lon,
            lat,
            lon,
        )
        if distance > config.stop_search_radius_m:
            continue

        props = feature.get("properties", {})
        haystack = all_text(props)
        matches = sum(1 for word in wanted if word and word in haystack)
        name = property_lookup(props, STOP_NAME_ALIASES)
        if name is None:
            meaningful = [
                str(v) for v in props.values()
                if isinstance(v, str) and len(v.strip()) >= 5
            ]
            name = (
                " | ".join(meaningful[:3])
                if meaningful
                else str(feature.get("id", "parada"))
            )
        candidates.append((matches, distance, feature, lat, lon, str(name)))

    if not candidates:
        raise RuntimeError(
            "A Semob respondeu, mas nenhuma parada foi encontrada perto das "
            "coordenadas configuradas. Ajuste approximate_lat/approximate_lon "
            "no config.json."
        )

    candidates.sort(key=lambda item: (-item[0], item[1]))
    matches, distance, feature, lat, lon, name = candidates[0]

    if matches == 0:
        print(
            "[aviso] Nenhuma descrição coincidiu com as palavras-chave; "
            "foi escolhida a parada mais próxima das coordenadas."
        )

    print(
        f"[parada] {name}\n"
        f"         latitude={lat:.6f}, longitude={lon:.6f}, "
        f"distância do ponto aproximado={distance:.0f} m, "
        f"palavras coincidentes={matches}"
    )
    return feature, lat, lon, name


def flatten_route_geometry(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Retorna partes como listas de (lat, lon)."""
    geo_type = geometry.get("type")
    coords = geometry.get("coordinates") or []
    parts: list[list[tuple[float, float]]] = []

    if geo_type == "LineString":
        parts = [[(float(y), float(x)) for x, y, *_ in coords]]
    elif geo_type == "MultiLineString":
        parts = [[(float(y), float(x)) for x, y, *_ in part] for part in coords]
    elif geo_type == "GeometryCollection":
        for item in geometry.get("geometries", []):
            parts.extend(flatten_route_geometry(item))

    return [part for part in parts if len(part) >= 2]


def local_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def project_on_polyline(
    lat: float,
    lon: float,
    points: list[tuple[float, float]],
) -> tuple[float, float, float]:
    """Retorna (distância acumulada até a projeção, afastamento, total)."""
    ref_lat, ref_lon = lat, lon
    xy = [local_xy(p_lat, p_lon, ref_lat, ref_lon) for p_lat, p_lon in points]
    best_offset = float("inf")
    best_along = 0.0
    accumulated = 0.0
    total = 0.0

    segment_lengths: list[float] = []
    for index in range(len(xy) - 1):
        x1, y1 = xy[index]
        x2, y2 = xy[index + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        segment_lengths.append(length)
        total += length

    for index, length in enumerate(segment_lengths):
        x1, y1 = xy[index]
        x2, y2 = xy[index + 1]
        if length <= 0:
            continue
        vx, vy = x2 - x1, y2 - y1
        # O ponto consultado é a origem (0,0).
        t = max(0.0, min(1.0, (-(x1 * vx + y1 * vy)) / (length * length)))
        px, py = x1 + t * vx, y1 + t * vy
        offset = math.hypot(px, py)
        if offset < best_offset:
            best_offset = offset
            best_along = accumulated + t * length
        accumulated += length

    return best_along, best_offset, total


def build_route_parts(
    config: Config,
) -> dict[str, list[list[tuple[float, float]]]]:
    features, _ = fetch_features_for_lines(
        ROUTE_LAYER,
        config.lines,
        all_limit=20_000,
        debug=config.debug,
    )
    routes: dict[str, list[list[tuple[float, float]]]] = {
        norm_line(line): [] for line in config.lines
    }
    for feature in features:
        line = extract_line(feature)
        if not line:
            continue
        routes.setdefault(line, []).extend(flatten_route_geometry(feature.get("geometry") or {}))

    for line, parts in routes.items():
        print(f"[itinerário] linha {line}: {len(parts)} trecho(s) encontrado(s)")
    return routes


def route_remaining(
    line: str,
    bus_lat: float,
    bus_lon: float,
    stop_lat: float,
    stop_lon: float,
    routes: dict[str, list[list[tuple[float, float]]]],
) -> RouteProjection | None:
    best: tuple[float, RouteProjection] | None = None
    for part in routes.get(norm_line(line), []):
        bus_along, bus_offset, total = project_on_polyline(bus_lat, bus_lon, part)
        stop_along, stop_offset, _ = project_on_polyline(stop_lat, stop_lon, part)

        # Ignora geometrias que não representam o trecho atual.
        if bus_offset > 800 or stop_offset > 800:
            continue

        remaining = abs(stop_along - bus_along)
        projection = RouteProjection(
            remaining_m=remaining,
            bus_offset_m=bus_offset,
            stop_offset_m=stop_offset,
            total_m=total,
        )
        score = bus_offset + stop_offset + 0.002 * remaining
        if best is None or score < best[0]:
            best = (score, projection)

    return best[1] if best else None


def parse_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace(" ", "T"),
    ]
    for candidate in candidates:
        try:
            result = datetime.fromisoformat(candidate)
            if result.tzinfo is None:
                result = result.replace(tzinfo=tz)
            return result.astimezone(tz)
        except ValueError:
            pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def numeric_speed(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        speed = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return speed if 1 <= speed <= 100 else None


def vehicle_id(feature: dict[str, Any]) -> str:
    value = property_lookup(feature.get("properties", {}), VEHICLE_ALIASES)
    return str(value).strip() if value not in (None, "") else str(feature.get("id", "desconhecido"))


def get_position_timestamp(feature: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    value = property_lookup(feature.get("properties", {}), TIMESTAMP_ALIASES)
    return parse_datetime(value, tz)


def get_source_speed(feature: dict[str, Any]) -> float | None:
    value = property_lookup(feature.get("properties", {}), SPEED_ALIASES)
    return numeric_speed(value)


class Telegram:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_ids = os.getenv("TELEGRAM_CHAT_IDS", "")
        self.chat_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]

    def validate(self) -> None:
        if not self.token:
            raise RuntimeError("Defina TELEGRAM_BOT_TOKEN.")
        if not self.chat_ids:
            raise RuntimeError("Defina TELEGRAM_CHAT_IDS com um ou mais IDs separados por vírgula.")

    def send(self, text: str) -> None:
        self.validate()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        errors: list[str] = []
        for chat_id in self.chat_ids:
            try:
                response = requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=(REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT),
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get("ok"):
                    errors.append(f"{chat_id}: {payload}")
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{chat_id}: {exc}")
        if errors:
            raise RuntimeError("Falha no Telegram: " + " | ".join(errors))

    def show_updates(self) -> None:
        if not self.token:
            raise RuntimeError("Defina TELEGRAM_BOT_TOKEN antes de consultar as mensagens.")
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        response = requests.get(url, params={"timeout": 0, "limit": 100}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))

        found: dict[str, dict[str, Any]] = {}
        for update in payload.get("result", []):
            message = (
                update.get("message")
                or update.get("edited_message")
                or update.get("channel_post")
                or {}
            )
            chat = message.get("chat") or {}
            if "id" not in chat:
                continue
            cid = str(chat["id"])
            found[cid] = {
                "chat_id": cid,
                "nome": " ".join(
                    str(v) for v in (chat.get("first_name"), chat.get("last_name")) if v
                ).strip(),
                "usuario": chat.get("username"),
                "ultima_mensagem": message.get("text"),
            }

        if not found:
            print(
                "Nenhuma conversa apareceu. Peça à pessoa que abra o bot, toque em "
                "\"Iniciar\" e envie uma mensagem identificável; depois execute novamente."
            )
            return

        print("\nConversas encontradas:")
        for item in found.values():
            print(
                f"- chat_id={item['chat_id']} | nome={item['nome'] or '-'} "
                f"| usuário=@{item['usuario'] or '-'} "
                f"| mensagem={item['ultima_mensagem']!r}"
            )


class Monitor:
    def __init__(self, config: Config, telegram: Telegram) -> None:
        self.config = config
        self.telegram = telegram
        self.tz = ZoneInfo(config.timezone)
        self.stop_feature: dict[str, Any] | None = None
        self.stop_lat = 0.0
        self.stop_lon = 0.0
        self.stop_name = ""
        self.routes: dict[str, list[list[tuple[float, float]]]] = {}
        self.previous: dict[str, VehicleObservation] = {}
        self.sent: dict[str, set[int]] = {}
        self.passed: set[str] = set()

    def initialize(self) -> None:
        self.stop_feature, self.stop_lat, self.stop_lon, self.stop_name = discover_stop(self.config)
        self.routes = build_route_parts(self.config)

    def estimate_speed(
        self,
        previous: VehicleObservation | None,
        current_lat: float,
        current_lon: float,
        now: datetime,
        source_speed: float | None,
    ) -> float:
        observed: float | None = None
        if previous:
            seconds = (now - previous.observed_at).total_seconds()
            if seconds >= 10:
                moved_m = haversine_m(previous.lat, previous.lon, current_lat, current_lon)
                candidate = moved_m / seconds * 3.6
                if 5 <= candidate <= 80:
                    observed = candidate

        if observed is not None and source_speed is not None:
            speed = 0.7 * observed + 0.3 * source_speed
        elif observed is not None:
            speed = observed
        elif source_speed is not None:
            speed = source_speed
        else:
            speed = self.config.default_speed_kmh

        return max(8.0, min(60.0, speed))

    def process_vehicle(self, feature: dict[str, Any], now: datetime) -> None:
        point = point_from_feature(feature)
        line = extract_line(feature)
        if not point or not line:
            return

        target = {norm_line(v) for v in self.config.lines}
        if line not in target:
            return

        timestamp = get_position_timestamp(feature, self.tz)
        if timestamp and (now - timestamp).total_seconds() > self.config.max_vehicle_age_minutes * 60:
            return

        lat, lon = point
        prefix = vehicle_id(feature)
        key = f"{line}|{prefix}"
        straight = haversine_m(lat, lon, self.stop_lat, self.stop_lon)
        projection = route_remaining(
            line, lat, lon, self.stop_lat, self.stop_lon, self.routes
        )
        remaining = (
            projection.remaining_m
            if projection
            else straight * self.config.straight_line_factor
        )

        previous = self.previous.get(key)
        source_speed = get_source_speed(feature)
        speed_kmh = self.estimate_speed(previous, lat, lon, now, source_speed)
        eta = remaining / 1000 / speed_kmh * 60 * self.config.traffic_factor

        current = VehicleObservation(
            line=line,
            vehicle_id=prefix,
            lat=lat,
            lon=lon,
            straight_distance_m=straight,
            remaining_m=remaining,
            observed_at=now,
            source_speed_kmh=source_speed,
        )

        if straight <= self.config.stop_passed_radius_m:
            self.passed.add(key)
            self.previous[key] = current
            return

        approaching = False
        if previous:
            route_gain = previous.remaining_m - current.remaining_m
            straight_gain = previous.straight_distance_m - current.straight_distance_m
            approaching = (
                route_gain >= self.config.minimum_movement_m
                or straight_gain >= self.config.minimum_movement_m
            )

        if self.config.debug:
            print(
                f"[debug] linha={line} veículo={prefix} "
                f"reta={straight/1000:.2f} km restante={remaining/1000:.2f} km "
                f"vel={speed_kmh:.1f} km/h eta={eta:.1f} min "
                f"aproximando={approaching}"
            )

        if previous and approaching and key not in self.passed:
            already = self.sent.setdefault(key, set())
            for threshold in self.config.alerts_minutes:
                if threshold in already:
                    continue

                previous_speed = self.estimate_speed(
                    None,
                    previous.lat,
                    previous.lon,
                    previous.observed_at,
                    previous.source_speed_kmh,
                )
                previous_eta = (
                    previous.remaining_m / 1000 / previous_speed
                    * 60 * self.config.traffic_factor
                )

                crossed = eta <= threshold and (
                    previous_eta > threshold
                    or not already
                )
                # Tolera o primeiro cálculo confiável já dentro da faixa.
                not_too_late = eta >= max(2.0, threshold * 0.45)

                if crossed and not_too_late:
                    text = (
                        f"🚌 Linha {display_line(line)} se aproximando\n\n"
                        f"Estimativa: cerca de {threshold} minutos para chegar à parada "
                        f"{self.stop_name}.\n"
                        f"Veículo: {prefix}\n"
                        f"Distância estimada pelo trajeto: {remaining/1000:.1f} km\n\n"
                        f"⚠️ Previsão experimental, sujeita ao trânsito e à atualização "
                        f"dos dados do DF no Ponto."
                    )
                    self.telegram.send(text)
                    print(
                        f"[telegram] alerta de {threshold} min enviado: "
                        f"linha={line}, veículo={prefix}, ETA calculado={eta:.1f} min"
                    )
                    already.add(threshold)

        self.previous[key] = current

    def cycle(self) -> None:
        now = datetime.now(self.tz)
        features, _ = fetch_features_for_lines(
            POSITION_LAYER,
            self.config.lines,
            all_limit=10_000,
            debug=self.config.debug,
        )
        print(f"[{now:%d/%m/%Y %H:%M:%S}] {len(features)} posição(ões) das linhas-alvo")
        for feature in features:
            try:
                self.process_vehicle(feature, now)
            except Exception as exc:  # um veículo defeituoso não encerra o monitor
                print(f"[aviso] Não foi possível processar um veículo: {exc}", file=sys.stderr)

        # Evita crescimento indefinido do histórico.
        cutoff = now.timestamp() - 4 * 60 * 60
        self.previous = {
            key: value for key, value in self.previous.items()
            if value.observed_at.timestamp() >= cutoff
        }

    def run(self, once: bool = False) -> None:
        self.telegram.validate()
        self.initialize()
        if once:
            self.cycle()
            return

        print(
            f"[monitor] ativo de {self.config.monitor_start} a {self.config.monitor_end}, "
            f"fuso {self.config.timezone}, consulta a cada {self.config.poll_seconds}s"
        )

        while True:
            now = datetime.now(self.tz)
            if now.weekday() not in self.config.weekdays:
                print("[monitor] hoje não está na lista de dias configurados; encerrando.")
                return

            start = parse_clock(self.config.monitor_start)
            end = parse_clock(self.config.monitor_end)
            current = now.time().replace(tzinfo=None)

            if current < start:
                wait = min(60, seconds_until(now, start))
                time.sleep(max(1, wait))
                continue
            if current > end:
                print("[monitor] fim do intervalo; encerrando.")
                return

            try:
                self.cycle()
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                print(f"[erro temporário] {exc}", file=sys.stderr)
            time.sleep(self.config.poll_seconds)


def display_line(line: str) -> str:
    return "0.167" if norm_line(line) == "167" else str(line)


def parse_clock(value: str) -> dt_time:
    hour, minute = value.split(":", 1)
    return dt_time(int(hour), int(minute))


def seconds_until(now: datetime, target: dt_time) -> int:
    target_dt = now.replace(
        hour=target.hour,
        minute=target.minute,
        second=0,
        microsecond=0,
    )
    return max(0, int((target_dt - now).total_seconds()))


def discover(config: Config) -> None:
    print("=== DESCOBERTA DA CONFIGURAÇÃO ===")
    stop, lat, lon, name = discover_stop(config)
    print(f"\nParada selecionada: {name} ({lat:.6f}, {lon:.6f})")
    print("Propriedades da parada:")
    print(json.dumps(stop.get("properties", {}), ensure_ascii=False, indent=2, default=str))

    for layer in (POSITION_LAYER, ROUTE_LAYER):
        print(f"\nCamada: {layer}")
        features, field = fetch_features_for_lines(
            layer,
            config.lines,
            all_limit=10_000,
            debug=True,
        )
        print(f"Campo de linha detectado: {field!r}")
        print(f"Registros encontrados: {len(features)}")
        if features:
            sample = features[0]
            print("Exemplo de propriedades:")
            print(json.dumps(sample.get("properties", {}), ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de ônibus do DF com Telegram.")
    parser.add_argument("--config", default="config.json", help="Caminho do config.json")
    parser.add_argument("--once", action="store_true", help="Executa uma única consulta")
    parser.add_argument("--discover", action="store_true", help="Mostra campos e parada detectados")
    parser.add_argument("--test-telegram", action="store_true", help="Envia uma mensagem de teste")
    parser.add_argument(
        "--show-updates",
        action="store_true",
        help="Mostra chat_ids que enviaram mensagens ao bot",
    )
    args = parser.parse_args()

    telegram = Telegram()

    try:
        if args.show_updates:
            telegram.show_updates()
            return 0
        if args.test_telegram:
            telegram.send(
                "✅ Teste concluído: o monitor das linhas 0.167 e 167.1 "
                "consegue enviar mensagens para este Telegram."
            )
            print("Mensagem de teste enviada para todos os chat_ids.")
            return 0

        config = load_config(args.config)
        if args.discover:
            discover(config)
            return 0

        Monitor(config, telegram).run(once=args.once)
        return 0
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")
        return 130
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
