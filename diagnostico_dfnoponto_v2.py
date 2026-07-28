#!/usr/bin/env python3
"""
Diagnóstico corrigido da fonte dfnoponto.com.

Localiza corretamente os scripts Next.js usados pelo mapa e procura:
- endpoints de API;
- chamadas fetch/WebSocket/EventSource;
- termos ligados a veículos, posições e previsões.

Não usa bibliotecas externas.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html import unescape

BASE = "https://dfnoponto.com"
PAGES = [
    f"{BASE}/horario/0.167",
    f"{BASE}/horario/167.1",
]

TIMEOUT = 60
MAX_BYTES = 12_000_000
MAX_SCRIPTS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
    ),
    "Accept": "text/html,application/javascript,application/json,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
}

NETWORK_WORDS = (
    "fetch(",
    "axios",
    "XMLHttpRequest",
    "WebSocket",
    "EventSource",
    "graphql",
    "supabase",
    "firebase",
)

BUS_WORDS = (
    "vehicle",
    "vehicles",
    "veiculo",
    "veiculos",
    "posição",
    "posicao",
    "positions",
    "latitude",
    "longitude",
    "arrival",
    "arrivals",
    "chegada",
    "previsao",
    "prediction",
    "realtime",
    "tempo real",
    "frota",
    "linha",
    "route",
    "direction",
    "sentido",
)


def fetch(url: str) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_BYTES + 1)[:MAX_BYTES]
        charset = response.headers.get_content_charset() or "utf-8"
        return (
            response.status,
            response.headers.get("Content-Type", ""),
            raw.decode(charset, errors="replace"),
        )


def unique(values):
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def find_script_urls(html: str, page_url: str) -> list[str]:
    # Corrige o erro da versão anterior: \b é limite de palavra,
    # e não uma barra literal seguida de "b".
    sources = re.findall(
        r'<script\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )

    # Alguns chunks podem aparecer como preload/modulepreload.
    sources.extend(
        re.findall(
            r'<link\b[^>]*\b(?:rel=["\'](?:preload|modulepreload)["\'])'
            r'[^>]*\bhref\s*=\s*["\']([^"\']+\.js[^"\']*)["\']',
            html,
            flags=re.I,
        )
    )

    return unique(
        urllib.parse.urljoin(page_url, unescape(source))
        for source in sources
        if ".js" in source
    )


def candidate_urls(text: str, base_url: str) -> list[str]:
    candidates = []

    # URLs absolutas.
    candidates.extend(
        re.findall(r'https?://[^\s"\'`<>\\]+', text, flags=re.I)
    )

    # Strings relativas com aparência de endpoint.
    relative_patterns = (
        r'["\'](/api/[^"\']+)["\']',
        r'["\'](/graphql[^"\']*)["\']',
        r'["\'](/trpc/[^"\']+)["\']',
        r'["\'](/(?:vehicles?|veiculos?|positions?|posicoes?|arrivals?|chegadas?)'
        r'[^"\']*)["\']',
    )
    for pattern in relative_patterns:
        for path in re.findall(pattern, text, flags=re.I):
            candidates.append(urllib.parse.urljoin(base_url, path))

    cleaned = []
    for candidate in candidates:
        candidate = unescape(candidate).replace("\\/", "/")
        candidate = candidate.rstrip("),;]}")
        lower = candidate.lower()

        # Remove fontes, analytics e publicidade sem relação com o mapa.
        if any(
            blocked in lower
            for blocked in (
                "google-analytics",
                "googletagmanager",
                "googlesyndication",
                "doubleclick",
                "fonts.googleapis",
                "fonts.gstatic",
            )
        ):
            continue

        if (
            "/api/" in lower
            or "graphql" in lower
            or "trpc" in lower
            or any(word in lower for word in BUS_WORDS)
        ):
            cleaned.append(candidate)

    return unique(cleaned)


def contexts(text: str, words: tuple[str, ...], limit: int = 40) -> list[str]:
    lower = text.lower()
    snippets = []

    for word in words:
        needle = word.lower()
        start = 0
        while len(snippets) < limit:
            index = lower.find(needle, start)
            if index < 0:
                break

            left = max(0, index - 240)
            right = min(len(text), index + 520)
            snippet = re.sub(r"\s+", " ", text[left:right]).strip()

            if len(snippet) >= 50:
                snippets.append(snippet)

            start = index + max(1, len(needle))

    return unique(snippets)[:limit]


def inspect_script(url: str, index: int, total: int) -> None:
    print("\n" + "-" * 90)
    print(f"SCRIPT {index}/{total}: {url}")

    try:
        status, content_type, text = fetch(url)
    except Exception as error:
        print(f"FALHA AO BAIXAR: {type(error).__name__}: {error}")
        return

    print(f"HTTP {status} | {content_type} | {len(text):,} caracteres")

    endpoints = candidate_urls(text, url)
    if endpoints:
        print("\nCANDIDATOS A ENDPOINT:")
        for endpoint in endpoints[:100]:
            print("  ", endpoint)

    network = contexts(text, NETWORK_WORDS, limit=30)
    if network:
        print("\nCHAMADAS DE REDE ENCONTRADAS:")
        for number, snippet in enumerate(network, 1):
            print(f"[REDE {number}] {snippet[:1500]}")

    bus = contexts(text, BUS_WORDS, limit=40)
    if bus:
        print("\nTRECHOS SOBRE ÔNIBUS/MAPA:")
        for number, snippet in enumerate(bus, 1):
            print(f"[ÔNIBUS {number}] {snippet[:1500]}")

    if not endpoints and not network and not bus:
        print("Nenhum indício relevante neste script.")


def main() -> int:
    print("=== DIAGNÓSTICO V2 DO DFNOPONTO.COM ===")
    print("Linhas 0.167 e 167.1 | OAB/Galois | sentido para o Guará")

    all_scripts = []

    for page_url in PAGES:
        print("\n" + "=" * 90)
        print("PÁGINA:", page_url)

        try:
            status, content_type, html = fetch(page_url)
        except Exception as error:
            print(f"ERRO AO ACESSAR: {type(error).__name__}: {error}")
            continue

        print(f"HTTP {status} | {content_type} | {len(html):,} caracteres")
        print("Contém OAB/Galois:", bool(re.search(r"OAB|Galois", html, re.I)))

        scripts = find_script_urls(html, page_url)
        print(f"SCRIPTS EXTERNOS ENCONTRADOS: {len(scripts)}")

        for script in scripts:
            print("  ", script)

        all_scripts.extend(scripts)

    all_scripts = unique(all_scripts)[:MAX_SCRIPTS]

    if not all_scripts:
        print("\nERRO: nenhum script foi encontrado.")
        return 1

    print("\n" + "=" * 90)
    print(f"TOTAL DE SCRIPTS ÚNICOS A INSPECIONAR: {len(all_scripts)}")

    for index, script_url in enumerate(all_scripts, 1):
        inspect_script(script_url, index, len(all_scripts))

    print("\n" + "=" * 90)
    print("DIAGNÓSTICO CONCLUÍDO.")
    print(
        "Envie o trecho do log que contenha "
        "'CANDIDATOS A ENDPOINT' ou 'CHAMADAS DE REDE ENCONTRADAS'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
