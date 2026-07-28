#!/usr/bin/env python3
"""
Teste V2 da fonte em tempo real do dfnoponto.com.

Conecta ao Socket.IO por HTTP polling, com a origem autorizada do site,
sem usar WebSocket. Imprime os eventos "posicoes" recebidos para as
linhas 0.167 e 167.1.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests


SERVER = "https://nopontoserver.onrender.com"
SOCKET_PATH = f"{SERVER}/socket.io/"
ORIGIN = "https://dfnoponto.com"
LINES = ["0.167", "167.1"]

CONNECT_TIMEOUT = 30
READ_TIMEOUT = 90
TEST_DURATION_SECONDS = 150
MAX_POSITION_EVENTS = 6

RECORD_SEPARATOR = "\x1e"


def now_token() -> str:
    return str(int(time.time() * 1000))


def compact(value: Any, limit: int = 30_000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + "\n... [conteúdo truncado]"
    return text


class EngineIOPollingClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Origin": ORIGIN,
                "Referer": ORIGIN + "/",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                ),
                "Accept": "*/*",
            }
        )
        self.sid: str | None = None

    def params(self) -> dict[str, str]:
        result = {
            "EIO": "4",
            "transport": "polling",
            "t": now_token(),
        }
        if self.sid:
            result["sid"] = self.sid
        return result

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
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
            },
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        response.raise_for_status()
        if response.text not in ("", "ok"):
            print("Resposta do POST:", repr(response.text[:500]))

    def handshake(self) -> dict[str, Any]:
        print("Abrindo sessão Engine.IO por HTTP polling...")
        payload = self.get()
        print("Resposta inicial:", repr(payload[:500]))

        packets = split_packets(payload)
        open_packet = next((p for p in packets if p.startswith("0")), None)

        if not open_packet:
            raise RuntimeError(
                "A resposta não contém o pacote de abertura Engine.IO."
            )

        info = json.loads(open_packet[1:])
        self.sid = str(info["sid"])

        print("Sessão aberta.")
        print("SID:", self.sid)
        print("Upgrades oferecidos:", info.get("upgrades"))
        print("Ping interval:", info.get("pingInterval"))
        print("Ping timeout:", info.get("pingTimeout"))
        return info

    def connect_namespace(self) -> None:
        print("\nConectando ao namespace principal...")
        self.post("40")

        deadline = time.time() + 45
        while time.time() < deadline:
            packets = split_packets(self.get())
            for packet in packets:
                print("Pacote de conexão:", repr(packet[:500]))

                if packet == "2":
                    self.post("3")
                elif packet.startswith("40"):
                    print("Namespace conectado.")
                    return
                elif packet.startswith("44"):
                    raise RuntimeError(
                        "O namespace recusou a conexão: " + packet
                    )

        raise TimeoutError("O namespace não confirmou a conexão.")

    def emit(self, event: str, data: Any) -> None:
        packet = "42" + json.dumps(
            [event, data],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.post(packet)

    def poll(self) -> list[str]:
        packets = split_packets(self.get())
        for packet in packets:
            if packet == "2":
                self.post("3")
        return packets


def split_packets(payload: str) -> list[str]:
    return [
        packet
        for packet in payload.split(RECORD_SEPARATOR)
        if packet != ""
    ]


def parse_socket_event(packet: str) -> tuple[str, Any] | None:
    if not packet.startswith("42"):
        return None

    content = json.loads(packet[2:])
    if not isinstance(content, list) or len(content) < 2:
        return None

    return str(content[0]), content[1]


def main() -> int:
    print("=== TESTE V2 DA FONTE EM TEMPO REAL ===")
    print("Servidor:", SERVER)
    print("Origem enviada:", ORIGIN)
    print("Transporte: HTTP polling")
    print("Linhas:", ", ".join(LINES))
    print(
        "\nO servidor gratuito pode levar algum tempo para despertar. "
        "Aguarde o encerramento do teste.\n"
    )

    client = EngineIOPollingClient()

    try:
        client.handshake()
        client.connect_namespace()

        for line in LINES:
            print(f"Enviando evento 'linha': {line}")
            client.emit("linha", line)

        received = 0
        deadline = time.time() + TEST_DURATION_SECONDS

        while time.time() < deadline and received < MAX_POSITION_EVENTS:
            packets = client.poll()

            for packet in packets:
                if packet in ("2", "3") or packet.startswith("40"):
                    continue

                event = parse_socket_event(packet)
                if not event:
                    print("Outro pacote:", repr(packet[:1_500]))
                    continue

                event_name, data = event
                print("\n" + "#" * 90)
                print("EVENTO RECEBIDO:", event_name)

                if event_name != "posicoes":
                    print(compact(data, limit=8_000))
                    continue

                received += 1
                print(f"EVENTO POSICOES Nº {received}")
                print("TIPO:", type(data).__name__)

                if isinstance(data, list):
                    print("QUANTIDADE DE VEÍCULOS:", len(data))
                    if data and isinstance(data[0], dict):
                        print(
                            "CAMPOS DO PRIMEIRO VEÍCULO:",
                            sorted(data[0].keys()),
                        )
                elif isinstance(data, dict):
                    print("CHAVES PRINCIPAIS:", sorted(data.keys()))

                print("CONTEÚDO:")
                print(compact(data))

        print("\n" + "=" * 90)

        if received == 0:
            print(
                "A conexão foi aceita, mas nenhum evento 'posicoes' chegou. "
                "Pode não haver veículos ativos agora."
            )
            return 2

        print(f"SUCESSO: {received} evento(s) de posições recebido(s).")
        print(
            "Envie o primeiro EVENTO POSICOES, especialmente os campos "
            "e o conteúdo do primeiro veículo."
        )
        return 0

    except requests.HTTPError as error:
        response = error.response
        print("\nERRO HTTP:", error)
        if response is not None:
            print("Status:", response.status_code)
            print("Resposta:", repr(response.text[:3_000]))
        return 1
    except Exception as error:
        print(f"\nERRO: {type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
