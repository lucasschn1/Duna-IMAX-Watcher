"""
Verifica se há novas sessões de "Duna - Parte 3" no IMAX Shopping Palladium
(Curitiba) e notifica no Telegram quando encontrar novidade.

Monitora duas fontes independentes, para não depender de uma só:
- ingresso.com: página de sessões do cinema (lista todos os filmes em
  cartaz). É renderizada via JS, então usamos Playwright para essa parte.
- imaxpalladium.com.br: página do próprio cinema dedicada a este filme.
  É HTML estático, então usamos só requests + BeautifulSoup, sem navegador.

Dá pra rodar só uma parte das fontes via a env var FONTES_ATIVAS (lista
separada por vírgula, ex: "imax_palladium"). Isso existe porque essa fonte
é bem mais leve que a do ingresso.com/Playwright — dá pra rodar num servidor
fraco (pouca RAM, HD) com bastante frequência, deixando a fonte pesada só
no GitHub Actions. O import do Playwright é adiado (só acontece se a fonte
"ingresso" estiver ativa), pra essa máquina leve nem precisar ter o pacote
instalado.

O estado (sessões já vistas) fica em state.json. No GitHub Actions, o
workflow commita esse arquivo de volta no repo; num deploy local (ex: um
servidor próprio), ele só precisa existir no disco — não tem por que
versionar no git.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL_INGRESSO = "https://www.ingresso.com/cinema/imax-shopping-palladium/sessoes?city=curitiba"
URL_IMAX_PALLADIUM = "https://imaxpalladium.com.br/filmes/duna-parte-3/"
STATE_FILE = Path(__file__).parent / "state.json"

# Quais fontes rodar nesta máquina. Por padrão as duas; um deploy leve (ex:
# servidor com pouca RAM) pode setar FONTES_ATIVAS=imax_palladium pra pular
# o Playwright/Chromium por completo.
FONTES_ATIVAS = {
    f.strip() for f in os.environ.get("FONTES_ATIVAS", "ingresso,imax_palladium").split(",") if f.strip()
}

# Nome de exibição de cada fonte, usado nas mensagens do Telegram.
NOME_FONTE = {
    "ingresso": "Ingresso.com",
    "imax_palladium": "IMAX Palladium",
}

# Identifica de onde a mensagem foi mandada (GitHub Actions, servidor
# próprio, etc.), já que mais de um deploy roda em paralelo e cada um avisa
# pelo mesmo bot do Telegram.
ORIGEM = os.environ.get("ORIGEM", "GitHub Actions")

# Desliga o aviso periódico de "continuo monitorando, nada encontrado".
# Útil quando mais de um deploy roda em paralelo (ex: GitHub Actions +
# servidor local) e o heartbeat de um já basta pra saber que está tudo
# funcionando.
HEARTBEAT_ATIVO = os.environ.get("HEARTBEAT_ATIVO", "1") != "0"

# requests.get sem um User-Agent de navegador é bloqueado por alguns sites
# quando a requisição vem de um IP de datacenter (como os runners do GitHub
# Actions).
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Intervalo mínimo entre avisos de "ainda monitorando, nada encontrado".
# O script pode rodar a cada 30 min (via cron), mas só manda esse aviso
# quando já tiver passado esse tempo desde o último heartbeat enviado.
HEARTBEAT_INTERVAL = timedelta(hours=1)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Usado para filtrar, dentre todos os filmes em cartaz no cinema, só as
# sessões de Duna - Parte 3.
TITULO_FILME = "duna"


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("sessoes_vistas", [])
        state.setdefault("last_heartbeat", None)
        return state
    return {"sessoes_vistas": [], "last_heartbeat": None}


def heartbeat_devido(state: dict) -> bool:
    ultimo = state.get("last_heartbeat")
    if not ultimo:
        return True
    ultimo_dt = datetime.fromisoformat(ultimo)
    return datetime.now(timezone.utc) - ultimo_dt >= HEARTBEAT_INTERVAL


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


def extrair_screening_events(page) -> list[dict]:
    """Lê os blocos <script type="application/ld+json"> da página e retorna
    os objetos ScreeningEvent (schema.org) do @graph.

    O ingresso.com usa Tailwind puro: não há nenhuma classe ou data-testid
    com "session"/"sessao" no HTML, então um seletor CSS não tem como achar
    as sessões. O JSON-LD é dado estruturado pensado para SEO/Google, então
    é uma fonte muito mais estável.
    """
    eventos: list[dict] = []
    for script in page.query_selector_all("script[type='application/ld+json']"):
        try:
            data = json.loads(script.inner_text())
        except json.JSONDecodeError:
            continue
        for item in data.get("@graph", []):
            if item.get("@type") == "ScreeningEvent":
                eventos.append(item)
    return eventos


def extrair_session_id(link: str) -> str | None:
    """Extrai o sessionId do link de checkout.ingresso.com.

    As duas fontes monitoradas linkam para o mesmo checkout.ingresso.com, e
    esse id é o jeito confiável de saber que duas entradas (uma de cada
    fonte) são a mesma sessão real — evita notificar duas vezes a mesma
    sessão só porque cada site descreve ela com um texto diferente.
    """
    m = re.search(r"sessionId=(\d+)", link)
    return m.group(1) if m else None


def buscar_sessoes_ingresso() -> list[tuple[str, str]]:
    """Abre a página de sessões do cinema no ingresso.com (via Playwright) e
    extrai as sessões de Duna - Parte 3.

    A página lista TODOS os filmes em cartaz, então filtramos pelo título do
    filme. Retorna pares (chave, texto): chave é o sessionId (usado para
    detectar novidade e para dedupe entre fontes), texto é a descrição
    mandada no Telegram.
    """
    from playwright.sync_api import sync_playwright

    sessoes: list[tuple[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL_INGRESSO, timeout=30000)

        # Espera o conteúdo dinâmico carregar
        page.wait_for_timeout(4000)

        for evento in extrair_screening_events(page):
            titulo = (evento.get("workPerformed") or {}).get("name", "")
            if TITULO_FILME not in titulo.lower():
                continue
            inicio = evento.get("startDate", "")
            formato = evento.get("name", "")
            link = (evento.get("offers") or {}).get("url", "")
            texto = f"[Ingresso.com] {inicio} | {formato} | {link}"
            chave = extrair_session_id(link) or texto
            sessoes.append((chave, texto))

        browser.close()

    return sessoes


def buscar_sessoes_imax_palladium() -> list[tuple[str, str]]:
    """Busca as sessões de Duna - Parte 3 direto na página do IMAX Palladium.

    Ao contrário do ingresso.com, essa página é dedicada só a este filme
    neste cinema (não precisa filtrar por título) e é HTML estático
    renderizado no servidor — não precisa de navegador, só requests.
    """
    resp = requests.get(URL_IMAX_PALLADIUM, headers=HTTP_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    sessoes: list[tuple[str, str]] = []
    for item in soup.select("a.sessao-item"):
        dia = item.get("data-dia", "")
        ano = item.get("data-ano", "")
        hora = item.get("data-hora", "")
        sala_el = item.select_one(".sessao-item__sala")
        sala = sala_el.get_text(strip=True) if sala_el else ""
        tags = [t.get_text(strip=True) for t in item.select(".sessao-item__tag")]
        link = item.get("href", "")
        texto = f"[IMAX Palladium] {dia}/{ano} {hora} - {sala} - {'/'.join(tags)} - {link}"
        chave = extrair_session_id(link) or texto
        sessoes.append((chave, texto))
    return sessoes


def buscar_sessoes() -> list[tuple[str, str]]:
    """Junta as sessões das fontes ativas nesta máquina (FONTES_ATIVAS).

    Cada fonte é isolada em seu próprio try/except: se uma delas falhar (ex:
    site fora do ar, mudança de estrutura), a outra continua funcionando
    normalmente em vez de derrubar o run inteiro. Se as duas fontes acharem
    a mesma sessão (mesmo sessionId), só a primeira ocorrência é mantida.
    """
    sessoes: list[tuple[str, str]] = []
    chaves_vistas: set[str] = set()

    fontes = {
        "ingresso": buscar_sessoes_ingresso,
        "imax_palladium": buscar_sessoes_imax_palladium,
    }
    for nome, buscar in fontes.items():
        if nome not in FONTES_ATIVAS:
            continue
        try:
            for chave, texto in buscar():
                if chave in chaves_vistas:
                    continue
                chaves_vistas.add(chave)
                sessoes.append((chave, texto))
        except Exception as e:
            print(f"Erro ao checar fonte '{nome}': {e}", file=sys.stderr)

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

    novas = [(chave, texto) for chave, texto in sessoes_atuais if chave not in vistas]
    agora = datetime.now(timezone.utc)

    if novas:
        msg = f"🎬 Nova sessão de Duna - Parte 3 no IMAX Palladium (Curitiba)! (via {ORIGEM})\n\n"
        msg += "\n".join(f"• {texto}" for _, texto in novas)
        msg += f"\n\n{URL_INGRESSO}\n{URL_IMAX_PALLADIUM}"
        send_telegram(msg)
        print(f"Notificação enviada: {len(novas)} sessão(ões) nova(s).")

        vistas.update(chave for chave, _ in sessoes_atuais)
        state["sessoes_vistas"] = sorted(vistas)
        state["last_heartbeat"] = agora.isoformat()  # a notificação já conta como aviso
        save_state(state)
    else:
        print("Nenhuma sessão nova encontrada.")
        if not HEARTBEAT_ATIVO:
            print("Heartbeat desativado nesta máquina (HEARTBEAT_ATIVO=0).")
        elif heartbeat_devido(state):
            fontes_label = ", ".join(
                NOME_FONTE[f] for f in ("ingresso", "imax_palladium") if f in FONTES_ATIVAS
            )
            send_telegram(
                f"🔎 Duna IMAX Watcher ({ORIGEM}): continuo monitorando o IMAX "
                f"Palladium (Curitiba) via {fontes_label}. Nenhuma sessão de "
                "Duna - Parte 3 encontrada até agora."
            )
            state["last_heartbeat"] = agora.isoformat()
            save_state(state)
            print("Heartbeat enviado.")
        else:
            print("Heartbeat ainda não é devido.")


if __name__ == "__main__":
    main()