#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pinterest image fetcher & downloader

- Sessione HTTP con retry/backoff e User-Agent realistico
- Estrazione URL immagini:
  1) Rendering con requests_html e parsing di __PWS_DATA__ (initialReduxState.pins)
  2) Fallback alla JSON API interna di Pinterest (BaseSearchResource/get)
- Download con retry esponenziale per 403/429
- CLI:
    python main2.py "gatti" outdir -n 10
"""

import argparse
import json
import logging
import os
import sys
import time
from typing import Iterable, List
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    # opzionale: migliora le chance di trovare immagini perché rende il JS
    from requests_html import HTMLSession  # type: ignore
except Exception:  # pragma: no cover - requests_html potrebbe non essere installato
    HTMLSession = None  # type: ignore

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# -----------------------------------------------------------------------------
# HTTP Session con retry e User-Agent
# -----------------------------------------------------------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


def create_session(
    total_retries: int = 5,
    backoff_factor: float = 1.0,
    user_agent: str = DEFAULT_UA,
    accept_language: str = "it-IT,it;q=0.9,en-US;q=0.8",
) -> requests.Session:
    """Crea una Session con strategia di retry/backoff e header 'da browser'."""
    session = requests.Session()

    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Language": accept_language,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return session


# -----------------------------------------------------------------------------
# Fetch URLs da Pinterest
# -----------------------------------------------------------------------------
def fetch_image_urls(query: str, limit: int, session: requests.Session) -> List[str]:
    """Restituisce fino a *limit* URL di immagini per la *query*.

    Prova prima il rendering JS con requests_html (__PWS_DATA__), poi fallback
    all'endpoint JSON interno di Pinterest.
    """
    urls: List[str] = []

    # 1) Rendering JS con requests_html (se disponibile)
    if HTMLSession is not None:
        url = f"https://www.pinterest.com/search/pins/?q={quote(query)}"
        logger.info("Carico pagina risultati (render JS): %s", url)
        html_sess = HTMLSession()
        # eredita gli header 'da browser' dalla sessione principale
        try:
            html_sess.headers.update(session.headers)
            resp = html_sess.get(url, timeout=30)
            # il rendering può richiedere Chromium; tempo max 25s
            resp.html.render(timeout=25, sleep=1.0)
            script = resp.html.find('script#__PWS_DATA__', first=True)
            if script and script.text:
                try:
                    data = json.loads(script.text)
                    pins = data.get("initialReduxState", {}).get("pins", {})
                    for pin in pins.values():
                        image_url = (
                            pin.get("images", {})
                            .get("orig", {})
                            .get("url")
                        )
                        if image_url:
                            urls.append(image_url)
                            if len(urls) >= limit:
                                break
                except Exception as parse_err:
                    logger.debug("Errore parsing __PWS_DATA__: %s", parse_err)
        except Exception as render_err:
            logger.debug("Rendering JS fallito: %s", render_err)
        finally:
            html_sess.close()

        if urls:
            logger.info("Trovati %d URL via rendering JS", len(urls))
            return urls[:limit]

    # 2) Fallback: API JSON "BaseSearchResource/get"
    logger.info("Fallback JSON API di Pinterest")
    data = {
        "options": {"query": query, "scope": "pins", "page_size": max(25, limit)},
        "context": {},
    }
    params = {
        "source_url": f"/search/pins/?q={quote(query)}",
        "data": json.dumps(data),
        "_": int(time.time() * 1000),
    }
    api_url = "https://www.pinterest.com/resource/BaseSearchResource/get/"

    try:
        r = session.get(api_url, params=params, timeout=20)
        logger.info("API status: %s", r.status_code)
        r.raise_for_status()
        payload = r.json()
        results = (
            payload.get("resource_response", {})
            .get("data", {})
            .get("results", [])
        )
        for res in results:
            u = res.get("images", {}).get("orig", {}).get("url")
            if u:
                urls.append(u)
                if len(urls) >= limit:
                    break
    except requests.RequestException as exc:
        logger.warning("Errore nella chiamata API Pinterest: %s", exc)

    if not urls:
        logger.warning("Nessun URL immagine trovato per '%s'", query)
    return urls[:limit]


# -----------------------------------------------------------------------------
# Download immagini
# -----------------------------------------------------------------------------
def download_image(
    url: str,
    dest_path: str,
    session: requests.Session,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
) -> bool:
    """Scarica una singola immagine con retry esponenziale su 403/429."""
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=20)
        except requests.RequestException as exc:
            logger.warning("Errore di rete su %s: %s", url, exc)
            break

        if resp.status_code == 200:
            with open(dest_path, "wb") as fh:
                fh.write(resp.content)
            logger.info("Scaricata: %s", url)
            return True

        if resp.status_code in {403, 429}:
            backoff = backoff_factor * (2 ** attempt)
            logger.warning(
                "Accesso negato (%s) per %s. Riprovo tra %.1fs...",
                resp.status_code,
                url,
                backoff,
            )
            time.sleep(backoff)
            continue

        logger.warning("Status inatteso %s per %s. Skip.", resp.status_code, url)
        break

    logger.error("Skip %s per blocco persistente o errore.", url)
    return False


def download_images(
    urls: Iterable[str],
    output_dir: str,
    session: requests.Session,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
) -> int:
    """Scarica più immagini in *output_dir*. Ritorna il conteggio dei successi."""
    os.makedirs(output_dir, exist_ok=True)
    count = 0
    for idx, url in enumerate(urls, start=1):
        filename = os.path.join(output_dir, f"image_{idx}.jpg")
        if download_image(
            url, filename, session, max_retries=max_retries, backoff_factor=backoff_factor
        ):
            count += 1
    return count


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main2.py",
        description="Cerca immagini su Pinterest e le scarica su disco.",
    )
    p.add_argument("query", help="Testo della ricerca (es. 'gatti')")
    p.add_argument("output", help="Directory di output per le immagini")
    p.add_argument(
        "-n",
        "--num-images",
        type=int,
        default=20,
        help="Numero massimo di immagini da scaricare (default: 20)",
    )
    p.add_argument(
        "--ua",
        default=DEFAULT_UA,
        help="User-Agent da usare per le richieste (opzionale).",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retry totali a livello di Session (default: 5)",
    )
    p.add_argument(
        "--backoff",
        type=float,
        default=1.0,
        help="Backoff factor per i retry (default: 1.0)",
    )
    return p.parse_args(argv)


def main(argv: List[str] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    session = create_session(
        total_retries=args.retries, backoff_factor=args.backoff, user_agent=args.ua
    )

    urls = fetch_image_urls(args.query, limit=args.num_images, session=session)
    if not urls:
        logger.error("Niente da scaricare. Esco.")
        return 2

    ok = download_images(
        urls, output_dir=args.output, session=session, max_retries=3, backoff_factor=1.0
    )
    logger.info("Scaricati %d/%d file.", ok, len(urls))
    return 0 if ok > 0 else 1


if __name__ == "__main__":  # pragma: no cover - esecuzione manuale
    sys.exit(main())
