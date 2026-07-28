#!/usr/bin/env python3
"""
Teste da fonte em tempo real utilizada pelo dfnoponto.com.

1. Consulta os dados das linhas pela API do site.
2. Conecta ao Socket.IO do servidor de posições.
3. Envia as linhas 0.167 e 167.1.
4. Imprime os primeiros eventos "posicoes" recebidos.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import requests
import socketio


SITE = "https://dfnoponto.com"
SOCKET_SERVER = "https://nopontoserver.onrender.com"
LINES = ["0.167", "167.1"]

REQUEST_TIMEOUT = 60
SOCKET_WAIT_SECONDS = 150
MAX_EVENTS = 4


def compact(value: Any, limit: int = 25_000) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    if len(text) > limit:
        return text[:limit] + "\n... [conteúdo truncado]"
    return text


def test_rest_api() -> None:
    print("\n" + "=" * 90)
    print("1. TESTE DA API DE DADOS DAS LINHAS")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"{SITE}/",
        }
    )

    for line in LINES:
        url = f"{SITE}/api/find/line/{line}/all"
        print(f"\nConsultando linha {line}: {url}")

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            print(
                f"HTTP {response.status_code} | "
                f"{response.headers.get('content-type', '-')}"
            )
            response.raise_for_status()
            payload = response.json()

            print("TIPO DA RESPOSTA:", type(payload).__name__)
            if isinstance(payload, dict):
                print("CHAVES PRINCIPAIS:", sorted(payload.keys()))
            elif isinstance(payload, list):
                print("QUANTIDADE DE ITENS:", len(payload))

            print("AMOSTRA DA RESPOSTA:")
            print(compact(payload, limit=12_000))
        except Exception as error:
            print(f"FALHA NA API DA LINHA {line}: {type(error).__name__}: {error}")


def test_socket() -> int:
    print("\n" + "=" * 90)
    print("2. TESTE DAS POSIÇÕES EM TEMPO REAL")
    print("Servidor:", SOCKET_SERVER)
    print("Linhas:", ", ".join(LINES))
    print(
        "O servidor gratuito pode levar até dois minutos para despertar. "
        "Aguarde o encerramento do teste."
    )

    received: list[Any] = []
    finished = threading.Event()

    client = socketio.Client(
        logger=False,
        engineio_logger=False,
        reconnection=True,
        reconnection_attempts=5,
        reconnection_delay=3,
        request_timeout=60,
    )

    @client.event
    def connect() -> None:
        print("\nSOCKET CONECTADO.")
        for line in LINES:
            print(f"Enviando evento 'linha': {line}")
            client.emit("linha", line)

    @client.event
    def connect_error(data: Any) -> None:
        print("ERRO DE CONEXÃO SOCKET:", repr(data))

    @client.event
    def disconnect(reason: Any = None) -> None:
        print("SOCKET DESCONECTADO.", repr(reason))

    @client.on("posicoes")
    def on_positions(data: Any) -> None:
        event_number = len(received) + 1
        received.append(data)

        print("\n" + "#" * 90)
        print(f"EVENTO POSICOES Nº {event_number}")
        print("TIPO:", type(data).__name__)

        if isinstance(data, list):
            print("QUANTIDADE DE VEÍCULOS:", len(data))
            if data:
                first = data[0]
                print("TIPO DO PRIMEIRO ITEM:", type(first).__name__)
                if isinstance(first, dict):
                    print("CAMPOS DO PRIMEIRO VEÍCULO:", sorted(first.keys()))
        elif isinstance(data, dict):
            print("CHAVES PRINCIPAIS:", sorted(data.keys()))

        print("CONTEÚDO:")
        print(compact(data))

        if len(received) >= MAX_EVENTS:
            finished.set()

    try:
        client.connect(
            SOCKET_SERVER,
            transports=["websocket"],
            wait_timeout=120,
        )
    except Exception as error:
        print(f"\nFALHA AO CONECTAR: {type(error).__name__}: {error}")
        return 1

    finished.wait(SOCKET_WAIT_SECONDS)

    try:
        client.disconnect()
    except Exception:
        pass

    print("\n" + "=" * 90)
    if not received:
        print(
            "NENHUM EVENTO 'posicoes' FOI RECEBIDO. "
            "O servidor pode estar adormecido ou sem veículos ativos."
        )
        return 2

    print(f"TESTE CONCLUÍDO: {len(received)} evento(s) recebido(s).")
    print(
        "Envie o trecho que começa em 'EVENTO POSICOES Nº 1', "
        "especialmente os CAMPOS DO PRIMEIRO VEÍCULO e o CONTEÚDO."
    )
    return 0


def main() -> int:
    print("=== TESTE DA FONTE EM TEMPO REAL DO DFNOPONTO.COM ===")
    test_rest_api()
    return test_socket()


if __name__ == "__main__":
    raise SystemExit(main())
