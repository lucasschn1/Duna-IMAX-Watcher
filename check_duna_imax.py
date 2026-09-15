"""
Verifica se há novas sessões de "Duna - Parte 3" no IMAX Shopping Palladium
(Curitiba) via ingresso.com e notifica no Telegram quando encontrar novidade.

A página é renderizada via JS, então usamos Playwright (Chromium headless)
em vez de um simples requests.get(). O estado (sessões já vistas) fica em
state.json, que o workflow do GitHub Actions commita de volta no repo.
"""

import json
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL_SESSOES = "https://www.ingresso.com/cinema/imax-shopping-palladium/sessoes?city=curitiba"
STATE_FILE = Path(__file__).parent / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Textos que indicam "sem sessões ainda" na página do ingresso.com
NO_SESSIONS_MARKERS = [
    "ainda não temos sessões",
    "não há sessões",
    "nenhuma sessão encontrada",
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sessoes_vistas": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_telegram(mensagem: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não configurados; pulando envio.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensagem,
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


def buscar_sessoes() -> list[str]:
    """Abre a página com Playwright e extrai textos que pareçam sessões.

    Retorna uma lista de strings simples (ex: '17/12 20:00 - Dublado 2D IMAX').
    Ajuste os seletores abaixo se a estrutura do site mudar.
    """
    sessoes: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL_SESSOES, timeout=30000)

        # Espera o conteúdo dinâmico carregar
        page.wait_for_timeout(4000)

        body_text = page.inner_text("body").lower()

        if any(marker in body_text for marker in NO_SESSIONS_MARKERS):
            browser.close()
            return []

        # Tenta capturar cards/linhas de sessão de forma genérica.
        # ATENÇÃO: seletor provisório — inspecione o HTML real do site
        # quando as sessões forem abertas e ajuste aqui se necessário.
        candidatos = page.query_selector_all(
            "[class*='session'], [class*='sessao'], [data-testid*='session']"
        )
        for el in candidatos:
            texto = el.inner_text().strip()
            if texto and len(texto) < 300:
                sessoes.append(texto)

        browser.close()

    return sessoes


def main() -> None:
    state = load_state()
    vistas = set(state.get("sessoes_vistas", []))

    try:
        sessoes_atuais = buscar_sessoes()
    except Exception as e:
        print(f"Erro ao checar a página: {e}", file=sys.stderr)
        # Não falha o workflow por instabilidade pontual do site
        sys.exit(0)

    novas = [s for s in sessoes_atuais if s not in vistas]

    if novas:
        msg = "🎬 Nova sessão de Duna - Parte 3 no IMAX Palladium (Curitiba)!\n\n"
        msg += "\n".join(f"• {s}" for s in novas)
        msg += f"\n\n{URL_SESSOES}"
        send_telegram(msg)
        print(f"Notificação enviada: {len(novas)} sessão(ões) nova(s).")

        vistas.update(sessoes_atuais)
        state["sessoes_vistas"] = sorted(vistas)
        save_state(state)
    else:
        print("Nenhuma sessão nova encontrada.")


if __name__ == "__main__":
    main()
