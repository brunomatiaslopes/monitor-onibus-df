#!/usr/bin/env python3
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
TIMEOUT = 45
MAX_BYTES = 8_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
}

KEYWORDS = (
    "api", "vehicle", "veiculo", "frota", "position", "posicao",
    "arrival", "chegada", "prediction", "previsao", "realtime",
    "latitude", "longitude", "route", "rota", "sentido", "direction",
    "map", "linha",
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
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def find_scripts(html: str, page_url: str) -> list[str]:
    sources = re.findall(
        r"<script\\b[^>]*\\bsrc\\s*=\\s*[\"']([^\"']+)[\"']",
        html,
        flags=re.I,
    )
    return unique(
        urllib.parse.urljoin(page_url, unescape(source))
        for source in sources
    )


def candidate_urls(text: str, base_url: str) -> list[str]:
    found = re.findall(r"https?://[^\\s\"'`<>\\\\]+", text, flags=re.I)
    paths = re.findall(
        r"[\"'`](/(?:api|graphql|trpc|ajax|dados|data|mapa|map|frota|"
        r"veiculos|vehicles|posicoes|positions|chegadas|arrivals)"
        r"[^\"'`\\\\\\s<>]*)[\"'`]",
        text,
        flags=re.I,
    )
    found.extend(urllib.parse.urljoin(base_url, path) for path in paths)

    cleaned = []
    for url in found:
        url = unescape(url).replace("\\/", "/").rstrip("),;")
        if any(keyword in url.lower() for keyword in KEYWORDS):
            cleaned.append(url)
    return unique(cleaned)


def contexts(text: str, limit: int = 40) -> list[str]:
    lower = text.lower()
    result = []

    for keyword in KEYWORDS:
        start = 0
        while len(result) < limit:
            index = lower.find(keyword, start)
            if index < 0:
                break
            left = max(0, index - 160)
            right = min(len(text), index + 340)
            snippet = re.sub(r"\\s+", " ", text[left:right]).strip()
            if len(snippet) > 40:
                result.append(snippet)
            start = index + len(keyword)

    return unique(result)[:limit]


def inspect_text(text: str, base_url: str, label: str) -> None:
    print(f"\n--- {label} ({len(text):,} caracteres)")

    urls = candidate_urls(text, base_url)
    if urls:
        print("CANDIDATOS A ENDPOINT:")
        for url in urls[:100]:
            print("  ", url)

    snippets = contexts(text)
    if snippets:
        print("TRECHOS RELEVANTES:")
        for index, snippet in enumerate(snippets[:30], 1):
            print(f"[{index}] {snippet[:900]}")


def main() -> int:
    print("=== DIAGNÓSTICO DA NOVA FONTE DFNOPONTO.COM ===")
    print("Linhas 0.167 e 167.1 | OAB/Galois | sentido Asa Norte/Esplanada -> Guará")

    success = False

    for page_url in PAGES:
        print("\n" + "=" * 90)
        print("PÁGINA:", page_url)

        try:
            status, content_type, html = fetch(page_url)
        except Exception as error:
            print(f"ERRO AO ACESSAR: {type(error).__name__}: {error}")
            continue

        success = True
        print(f"HTTP {status} | {content_type} | {len(html):,} caracteres")
        print("Contém Galois/OAB:", bool(re.search(r"Galois|OAB", html, re.I)))
        print("Contém Carregando mapa:", "Carregando mapa" in html)
        print("Next.js:", "__NEXT_DATA__" in html or "self.__next_f" in html)
        print("Nuxt:", "__NUXT_DATA__" in html)

        inspect_text(html, page_url, "HTML DA PÁGINA")

        scripts = find_scripts(html, page_url)
        print(f"\nScripts externos encontrados: {len(scripts)}")
        for script in scripts:
            print("  ", script)

        for index, script_url in enumerate(scripts, 1):
            print(f"\nBaixando script {index}/{len(scripts)}: {script_url}")
            try:
                script_status, script_type, script_text = fetch(script_url)
                print(
                    f"HTTP {script_status} | {script_type} | "
                    f"{len(script_text):,} caracteres"
                )
                inspect_text(
                    script_text,
                    script_url,
                    f"SCRIPT {index}/{len(scripts)}",
                )
            except Exception as error:
                print(f"FALHA NO SCRIPT: {type(error).__name__}: {error}")

    if not success:
        print("\nRESULTADO: o GitHub não conseguiu acessar dfnoponto.com.")
        return 1

    print("\n" + "=" * 90)
    print("RESULTADO: o GitHub conseguiu acessar a nova fonte.")
    print("Envie o trecho do log com CANDIDATOS A ENDPOINT e TRECHOS RELEVANTES.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
