"""
Verifica se há novas sessões de "Duna - Parte 3" no IMAX Shopping Palladium
(Curitiba) e notifica no Telegram quando encontrar novidade.

Monitora duas fontes independentes, para não depender de uma só:
- ingresso.com: API JSON (api-content.ingresso.com) que o próprio site usa
  pra montar a página do cinema, consultada data a data.
- imaxpalladium.com.br: página do próprio cinema dedicada a este filme.
  É HTML estático, então usamos só requests + BeautifulSoup.

Nenhuma das duas precisa de navegador. Dá pra rodar só uma parte das fontes
via a env var FONTES_ATIVAS (lista separada por vírgula, ex:
"imax_palladium").

O estado (sessões já vistas) fica em state.json. No GitHub Actions, o
workflow commita esse arquivo de volta no repo; num deploy local (ex: um
servidor próprio), ele só precisa existir no disco — não tem por que
versionar no git.
"""

import html
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL_INGRESSO = "https://www.ingresso.com/cinema/imax-shopping-palladium?city=curitiba"
# API que a página acima chama por baixo (city 18 = Curitiba, theater 795 =
# IMAX Shopping Palladium). Ids obtidos observando as requisições da página.
API_INGRESSO = "https://api-content.ingresso.com/v0/sessions/city/18/theater/795"
URL_IMAX_PALLADIUM = "https://imaxpalladium.com.br/filmes/duna-parte-3/"
STATE_FILE = Path(__file__).parent / "state.json"

# Quais fontes rodar nesta máquina. Por padrão as duas.
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

# Liga o resumo periódico (execuções, erros por fonte, sessões novas no
# período). Pensado pro deploy que roda com mais frequência (o servidor
# próprio): dá uma noção de "está tudo funcionando" mais informativa que um
# heartbeat simples, sem exigir que cada deploy mande o seu.
RESUMO_ATIVO = os.environ.get("RESUMO_ATIVO", "0") != "0"
RESUMO_INTERVALO = timedelta(days=float(os.environ.get("RESUMO_INTERVALO_DIAS", "3")))

# Quantos problemas (erros de fonte, canário) ficam guardados no state pro
# comando "problema" do bot.
MAX_PROBLEMAS = 20

# Pausa automática de uma fonte quando o site bloqueia o acesso (ver
# aplicar_pausas): começa em PAUSA_BLOQUEIO_MIN e dobra a cada bloqueio
# seguido, até PAUSA_MAXIMA.
STATUS_BLOQUEIO = {403, 429}
PAUSA_BLOQUEIO = timedelta(minutes=float(os.environ.get("PAUSA_BLOQUEIO_MIN", "60")))
PAUSA_MAXIMA = timedelta(hours=6)

# Quantas checagens seguidas uma sessão precisa estar ausente de todas as
# fontes pra sair o aviso de "sessão removida" (ver checar_removidas).
AUSENCIAS_PRA_AVISAR = 2

# Horário de Brasília pra exibir nas mensagens (sem horário de verão desde
# 2019, então um offset fixo basta e evita depender do tzdata no Windows).
FUSO_BR = timezone(timedelta(hours=-3))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Usado para filtrar, dentre todos os filmes em cartaz no cinema, só as
# sessões de Duna - Parte 3.
TITULO_FILME = "duna"


@dataclass
class Sessao:
    """Uma sessão de cinema, já normalizada pro mesmo formato nas duas
    fontes, pra dar pra montar uma mensagem de Telegram arrumada."""

    chave: str  # sessionId (dedupe entre fontes e detecção de novidade)
    fonte: str  # "Ingresso.com" ou "IMAX Palladium"
    data: str  # dd/mm/aaaa
    hora: str  # HH:MM
    link: str
    sala: str = ""
    tags: list[str] = field(default_factory=list)
    preco: float | None = None  # só a API do ingresso.com informa

    def para_state(self) -> dict:
        return {
            "chave": self.chave,
            "data": self.data,
            "hora": self.hora,
            "sala": self.sala,
            "tags": self.tags,
            "link": self.link,
            "preco": self.preco,
        }


@dataclass
class ResultadoBusca:
    """Resultado de buscar_sessoes(): as sessões já deduplicadas, mais a
    contagem bruta e os erros por fonte (antes do dedupe) — usados pro
    canário (fonte que parou de achar sessão) e pro resumo periódico."""

    sessoes: list[Sessao] = field(default_factory=list)
    contagens: dict[str, int] = field(default_factory=dict)
    erros: dict[str, str] = field(default_factory=dict)
    # Um registro por erro (causa provável + stack), pro comando "problema"
    # do bot. Ver descrever_excecao().
    problemas: list[dict] = field(default_factory=list)


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


def send_telegram(
    mensagem: str,
    parse_mode: str | None = None,
    silencioso: bool = False,
    botoes: list[list[tuple[str, str]]] | None = None,
) -> None:
    """Envia a mensagem pro chat configurado.

    `silencioso` entrega sem som/vibração — usado nos avisos de rotina
    (nada novo, resumo), pra que o celular só toque quando há algo a fazer.
    `botoes` são linhas de botões de link (texto, url) embaixo da mensagem.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não configurados; pulando envio.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        # Sem preview: uma mensagem com vários links de sessão geraria uma
        # parede de cards de preview embaixo do texto.
        "disable_web_page_preview": True,
        "disable_notification": silencioso,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if botoes:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": t, "url": u} for t, u in linha] for linha in botoes]}
        )
    resp = requests.post(url, data=payload, timeout=15)
    resp.raise_for_status()


def extrair_session_id(link: str) -> str | None:
    """Extrai o sessionId do link de checkout.ingresso.com.

    As duas fontes monitoradas linkam para o mesmo checkout.ingresso.com, e
    esse id é o jeito confiável de saber que duas entradas (uma de cada
    fonte) são a mesma sessão real — evita notificar duas vezes a mesma
    sessão só porque cada site descreve ela com um texto diferente.
    """
    m = re.search(r"sessionId=(\d+)", link)
    return m.group(1) if m else None


def buscar_sessoes_ingresso() -> list[Sessao]:
    """Busca as sessões de Duna - Parte 3 na API do ingresso.com.

    A página do cinema no site só embute (no JSON-LD) as sessões do dia
    atual; as outras datas são carregadas sob demanda por esta API quando o
    usuário clica no seletor de data. Então consultamos a lista de datas
    com sessão e, pra cada uma, as sessões do dia. A resposta lista todos
    os filmes do cinema, então filtramos pelo título.
    """
    resp = requests.get(f"{API_INGRESSO}/dates/partnership/home", headers=HTTP_HEADERS, timeout=15)
    resp.raise_for_status()
    datas = [d["date"] for d in resp.json() if d.get("date")]

    sessoes: list[Sessao] = []
    for data_iso in datas:
        resp = requests.get(
            f"{API_INGRESSO}/partnership/home/groupBy/sessionType",
            params={"date": data_iso},
            headers=HTTP_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        for dia in resp.json():
            for filme in dia.get("movies", []):
                if TITULO_FILME not in (filme.get("title") or "").lower():
                    continue
                for tipo in filme.get("sessionTypes", []):
                    for s in tipo.get("sessions", []):
                        inicio = (s.get("date") or {}).get("localDate", "")
                        try:
                            dt = datetime.fromisoformat(inicio)
                            data, hora = dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
                        except ValueError:
                            data, hora = data_iso, s.get("time", "")
                        link = s.get("siteURL", "")
                        chave = str(s.get("id") or extrair_session_id(link) or f"{inicio}|{link}")
                        sessoes.append(
                            Sessao(
                                chave=chave,
                                fonte="Ingresso.com",
                                data=data,
                                hora=hora,
                                link=link,
                                sala=s.get("room") or "",
                                tags=list(s.get("type") or []),
                                preco=s.get("price"),
                            )
                        )

    return sessoes


def buscar_sessoes_imax_palladium() -> list[Sessao]:
    """Busca as sessões de Duna - Parte 3 direto na página do IMAX Palladium.

    Ao contrário do ingresso.com, essa página é dedicada só a este filme
    neste cinema (não precisa filtrar por título) e é HTML estático
    renderizado no servidor — não precisa de navegador, só requests.
    """
    resp = requests.get(URL_IMAX_PALLADIUM, headers=HTTP_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    sessoes: list[Sessao] = []
    for item in soup.select("a.sessao-item"):
        dia = item.get("data-dia", "")
        ano = item.get("data-ano", "")
        hora = item.get("data-hora", "")
        sala_el = item.select_one(".sessao-item__sala")
        sala = sala_el.get_text(strip=True) if sala_el else ""
        tags = [t.get_text(strip=True) for t in item.select(".sessao-item__tag")]
        if "bg-imax" in item.get("class", []):
            tags.insert(0, "IMAX")
        link = item.get("href", "")

        data = f"{dia}/{ano}" if dia and ano else dia
        chave = extrair_session_id(link) or f"{data}|{hora}|{link}"
        sessoes.append(
            Sessao(chave=chave, fonte="IMAX Palladium", data=data, hora=hora, link=link, sala=sala, tags=tags)
        )
    return sessoes


def buscar_sessoes(pular: set[str] = frozenset()) -> ResultadoBusca:
    """Junta as sessões das fontes ativas nesta máquina (FONTES_ATIVAS),
    exceto as em `pular` (pausadas por bloqueio, ver aplicar_pausas).

    Cada fonte é isolada em seu próprio try/except: se uma delas falhar (ex:
    site fora do ar, mudança de estrutura), a outra continua funcionando
    normalmente em vez de derrubar o run inteiro. Se as duas fontes acharem
    a mesma sessão (mesmo sessionId), só a primeira ocorrência é mantida.
    """
    resultado = ResultadoBusca()
    chaves_vistas: set[str] = set()

    fontes = {
        "ingresso": buscar_sessoes_ingresso,
        "imax_palladium": buscar_sessoes_imax_palladium,
    }
    for nome, buscar in fontes.items():
        if nome not in FONTES_ATIVAS:
            continue
        if nome in pular:
            print(f"Fonte '{nome}' pausada por bloqueio; pulando.")
            continue
        try:
            encontradas = buscar()
        except Exception as e:
            print(f"Erro ao checar fonte '{nome}': {e}", file=sys.stderr)
            resultado.erros[nome] = str(e)
            resultado.problemas.append(descrever_excecao(nome, e))
            continue

        resultado.contagens[nome] = len(encontradas)
        for sessao in encontradas:
            if sessao.chave in chaves_vistas:
                continue
            chaves_vistas.add(sessao.chave)
            resultado.sessoes.append(sessao)

    return resultado


def provavel_causa(e: Exception) -> str:
    """Traduz a exceção numa causa provável legível, pra não precisar ler o
    stack pra ter uma ideia do que aconteceu."""
    if isinstance(e, requests.Timeout):
        return "O site demorou demais pra responder (lento ou sobrecarregado)."
    if isinstance(e, requests.ConnectionError):
        return "Não conseguiu conectar no site (fora do ar, DNS ou sem internet na máquina)."
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in (401, 403, 429):
            return f"O site recusou o acesso (HTTP {status}): provável bloqueio anti-robô ou excesso de requisições."
        if status == 404:
            return "Página/endereço não encontrado (HTTP 404): a URL provavelmente mudou."
        if status >= 500:
            return f"Erro interno do site (HTTP {status}): instabilidade do lado deles."
        return f"O site respondeu com erro HTTP {status}."
    if isinstance(e, (json.JSONDecodeError, KeyError, TypeError, AttributeError, IndexError)):
        return "A resposta veio num formato inesperado: o site/API provavelmente mudou de estrutura."
    return "Erro inesperado: veja o stack abaixo."


def descrever_excecao(fonte: str, e: Exception) -> dict:
    """Registro de problema a partir de uma exceção capturada. Tem que ser
    chamada dentro do except, pra format_exc() pegar o stack certo."""
    resposta = getattr(e, "response", None)
    retry_after = resposta.headers.get("Retry-After", "") if resposta is not None else ""
    return {
        "fonte": fonte,
        "tipo": type(e).__name__,
        "mensagem": str(e)[:500],
        "causa": provavel_causa(e),
        # Usados por aplicar_pausas() pra detectar bloqueio e respeitar o
        # tempo de espera pedido pelo site (só o formato em segundos).
        "status_http": resposta.status_code if resposta is not None else None,
        "retry_after": int(retry_after) if retry_after.isdigit() else None,
        # O fim do stack é o que interessa (onde estourou); corta o começo
        # pra não inchar o state.json.
        "stack": traceback.format_exc()[-3000:],
    }


def registrar_problema(state: dict, problema: dict, agora: datetime) -> None:
    """Guarda o problema (com data/hora e origem) no state, mantendo só os
    MAX_PROBLEMAS mais recentes. Lido pelo comando "problema" do bot."""
    problemas = state.setdefault("problemas", [])
    problemas.append({"quando": agora.isoformat(), "origem": ORIGEM, **problema})
    del problemas[:-MAX_PROBLEMAS]


def fontes_pausadas(state: dict, agora: datetime) -> set[str]:
    return {
        fonte
        for fonte, ate in (state.get("pausa_ate") or {}).items()
        if ate and datetime.fromisoformat(ate) > agora
    }


def aplicar_pausas(state: dict, resultado: ResultadoBusca, agora: datetime) -> list[str]:
    """Pausa a fonte que respondeu com bloqueio (403/429) em vez de continuar
    insistindo, o que só prolongaria o bloqueio. A pausa dobra a cada
    bloqueio seguido (PAUSA_BLOQUEIO, 2x, 4x... até PAUSA_MAXIMA) e respeita
    o Retry-After do site se ele pedir mais. Quando a fonte volta a
    responder, zera o contador. Retorna os avisos pro Telegram (🟡 ao
    pausar, 🟢 ao voltar)."""
    pausa_ate = state.setdefault("pausa_ate", {})
    seguidos = state.setdefault("bloqueios_seguidos", {})
    avisos = []

    for problema in resultado.problemas:
        if problema.get("status_http") not in STATUS_BLOQUEIO:
            continue
        fonte = problema["fonte"]
        seguidos[fonte] = seguidos.get(fonte, 0) + 1
        duracao = min(PAUSA_BLOQUEIO * 2 ** (seguidos[fonte] - 1), PAUSA_MAXIMA)
        if problema.get("retry_after"):
            duracao = max(duracao, timedelta(seconds=problema["retry_after"]))
        fim = agora + duracao
        pausa_ate[fonte] = fim.isoformat()
        minutos = int(duracao.total_seconds() // 60)
        avisos.append(
            f"🟡 <b>FONTE {html.escape(NOME_FONTE.get(fonte, fonte).upper())} PAUSADA</b> 🟡\n"
            f"🚫 O SITE BLOQUEOU O ACESSO (HTTP {problema['status_http']}), "
            f"{seguidos[fonte]}º BLOQUEIO SEGUIDO.\n"
            f"⏸ PAUSADA POR {minutos} MIN, ATÉ {formatar_data_hora(fim)} "
            f"({html.escape(ORIGEM.upper())}).\n"
            "AS OUTRAS FONTES CONTINUAM MONITORANDO. MANDE <code>problema</code> PRA DETALHES."
        )

    for fonte in resultado.contagens:  # fontes que responderam sem erro
        if seguidos.get(fonte):
            avisos.append(
                f"🟢 <b>FONTE {html.escape(NOME_FONTE.get(fonte, fonte).upper())} VOLTOU</b> 🟢\n"
                f"O SITE VOLTOU A RESPONDER NORMALMENTE ({html.escape(ORIGEM.upper())})."
            )
        seguidos[fonte] = 0
        pausa_ate.pop(fonte, None)

    return avisos


def checar_canario(state: dict, contagens: dict[str, int]) -> list[str]:
    """Compara a contagem de sessões desta execução com a da anterior, por
    fonte, e retorna avisos se uma fonte que antes achava sessão passou a
    achar 0 — sinal de que o site pode ter mudado de estrutura (o parser
    quebrou) em vez de simplesmente "ainda não tem sessão".

    O aviso é disparado só na transição (>0 -> 0), não a cada execução
    enquanto continuar zerado, pra não virar spam.
    """
    anteriores = state.setdefault("contagem_por_fonte", {})
    alertados = state.setdefault("canario_alertado", {})
    avisos = []

    for fonte, atual in contagens.items():
        anterior = anteriores.get(fonte, 0)
        if atual == 0 and anterior > 0 and not alertados.get(fonte):
            avisos.append(
                f"⚠️ <b>Possível problema na fonte {html.escape(NOME_FONTE.get(fonte, fonte))}</b>\n"
                f"Achava {anterior} sessão(ões) e agora não acha nenhuma. Pode ser "
                "o site fora do ar ou uma mudança de estrutura — vale checar manualmente."
            )
            alertados[fonte] = True
            registrar_problema(
                state,
                {
                    "fonte": fonte,
                    "tipo": "Canário",
                    "mensagem": f"Achava {anterior} sessão(ões) e passou a achar 0 (sem exceção).",
                    "causa": "O parser rodou sem erro mas não achou nada: provável mudança de "
                    "estrutura do site, ou as sessões foram retiradas.",
                    "stack": "",
                },
                datetime.now(timezone.utc),
            )
        elif atual > 0:
            alertados[fonte] = False
        anteriores[fonte] = atual

    return avisos


def atualizar_resumo(state: dict, sessoes_novas: int, erros: dict[str, str], agora: datetime) -> None:
    resumo = state.setdefault(
        "resumo", {"desde": None, "execucoes": 0, "sessoes_novas": 0, "erros_por_fonte": {}}
    )
    if not resumo.get("desde"):
        resumo["desde"] = agora.isoformat()
    resumo["execucoes"] = resumo.get("execucoes", 0) + 1
    resumo["sessoes_novas"] = resumo.get("sessoes_novas", 0) + sessoes_novas
    erros_por_fonte = resumo.setdefault("erros_por_fonte", {})
    for fonte in erros:
        erros_por_fonte[fonte] = erros_por_fonte.get(fonte, 0) + 1


def resumo_devido(state: dict) -> bool:
    desde = (state.get("resumo") or {}).get("desde")
    if not desde:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(desde) >= RESUMO_INTERVALO


def formatar_data_hora(dt: datetime) -> str:
    """dd/mm HH:MM no horário de Brasília (os timestamps do state são UTC)."""
    return dt.astimezone(FUSO_BR).strftime("%d/%m %H:%M")


def formatar_tempo_relativo(dt: datetime, agora: datetime) -> str:
    """"HÁ 12 MIN", "HÁ 3 H", "HÁ 2 DIAS" — pra leitura rápida no celular."""
    minutos = int((agora - dt).total_seconds() // 60)
    if minutos < 1:
        return "AGORA MESMO"
    if minutos < 60:
        return f"HÁ {minutos} MIN"
    if minutos < 48 * 60:
        return f"HÁ {minutos // 60} H"
    return f"HÁ {minutos // (24 * 60)} DIAS"


def linha_fontes(state: dict) -> list[str]:
    """Uma linha por fonte com a contagem da última execução, se o canário
    está disparado pra ela e se está pausada por bloqueio — usado no resumo
    e no /status do bot."""
    contagens = state.get("contagem_por_fonte") or {}
    alertados = state.get("canario_alertado") or {}
    pausa_ate = state.get("pausa_ate") or {}
    pausadas = fontes_pausadas(state, datetime.now(timezone.utc))
    linhas = []
    for fonte, nome in NOME_FONTE.items():
        if fonte in pausadas:
            fim = formatar_data_hora(datetime.fromisoformat(pausa_ate[fonte]))
            linhas.append(f"  ⏸ {html.escape(nome.upper())}: PAUSADA POR BLOQUEIO ATÉ {fim}")
            continue
        if fonte not in contagens:
            continue
        icone = "⚠️" if alertados.get(fonte) else "✅"
        linhas.append(f"  {icone} {html.escape(nome.upper())}: {contagens[fonte]} SESSÃO(ÕES)")
    return linhas


def montar_resumo(state: dict, agora: datetime) -> str:
    """Resumo do período: 🟢 se apareceu sessão nova, 🔴 se não — mesma
    convenção das notificações de sessão —, com ⚠️ à parte pros erros."""
    resumo = state.get("resumo", {})
    desde_dt = datetime.fromisoformat(resumo["desde"])
    dias = max(1, round((agora - desde_dt).total_seconds() / 86400))
    execucoes = resumo.get("execucoes", 0)
    sessoes_novas = resumo.get("sessoes_novas", 0)
    erros_por_fonte = resumo.get("erros_por_fonte", {})
    total_erros = sum(erros_por_fonte.values())

    cor = "🟢" if sessoes_novas else "🔴"
    linhas = [
        f"{cor} <b>RESUMO DOS ÚLTIMOS {dias} DIA(S)</b> {cor}",
        f"📊 {html.escape(ORIGEM.upper())} · {formatar_data_hora(desde_dt)} → {formatar_data_hora(agora)}",
        "",
        f"🔁 {execucoes} CHECAGEM(NS)",
        f"🎬 {sessoes_novas} SESSÃO(ÕES) NOVA(S) · {len(state.get('sessoes_vistas', []))} CONHECIDA(S) NO TOTAL",
    ]
    if total_erros:
        detalhes = ", ".join(
            f"{html.escape(NOME_FONTE.get(f, f).upper())}: {n}" for f, n in erros_por_fonte.items() if n
        )
        taxa = f" ({total_erros * 100 // execucoes}% DAS CHECAGENS)" if execucoes else ""
        linhas.append(f"⚠️ {total_erros} ERRO(S){taxa} — {detalhes}")
    else:
        linhas.append("✅ SEM ERROS NO PERÍODO")

    fontes = linha_fontes(state)
    if fontes:
        linhas += ["", "<b>FONTES (ÚLTIMA CHECAGEM)</b>", *fontes]
    return "\n".join(linhas)


def reiniciar_resumo(state: dict, agora: datetime) -> None:
    state["resumo"] = {
        "desde": agora.isoformat(),
        "execucoes": 0,
        "sessoes_novas": 0,
        "erros_por_fonte": {},
    }


DIAS_SEMANA = ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"]

# Acima disso, a mensagem de sessão nova não ganha um botão por sessão (vira
# uma parede de botões) — os links "COMPRAR" ficam no texto.
MAX_BOTOES_SESSAO = 8


def chave_data(data_str: str) -> datetime:
    """Pra ordenar datas dd/mm/aaaa; data em formato inesperado vai pro fim."""
    try:
        return datetime.strptime(data_str, "%d/%m/%Y")
    except ValueError:
        return datetime.max


def rotulo_data(data_str: str, com_ano: bool = True) -> str:
    """"15/12/2026" -> "TER 15/12/2026" (ou "TER 15/12" sem o ano)."""
    try:
        dt = datetime.strptime(data_str, "%d/%m/%Y")
    except ValueError:
        return data_str
    return f"{DIAS_SEMANA[dt.weekday()]} {dt.strftime('%d/%m/%Y' if com_ano else '%d/%m')}"


def formatar_preco(preco: float | None) -> str:
    return f"R$ {preco:.2f}".replace(".", ",") if preco else ""


def inicio_sessao(data: str, hora: str) -> datetime | None:
    try:
        return datetime.strptime(f"{data} {hora}", "%d/%m/%Y %H:%M").replace(tzinfo=FUSO_BR)
    except ValueError:
        return None


def descrever_sessao(s: dict) -> str:
    """"SALA 1 · IMAX · DUBLADO · R$ 47,76" a partir de Sessao.para_state()."""
    return " · ".join(filter(None, [s.get("sala"), *s.get("tags", []), formatar_preco(s.get("preco"))])).upper()


def montar_mensagem_novas(
    novas: list[Sessao], datas_novas: set[str]
) -> tuple[str, list[list[tuple[str, str]]]]:
    """Monta a mensagem HTML de "sessão nova", agrupada por data (com dia da
    semana) e ordenada por horário, mais os botões de compra.

    Tudo em maiúsculas e com 🟢 (o Telegram não permite colorir texto), pra
    diferenciar de relance do aviso de "nada encontrado" (🔴). Só o texto
    visível vai pra maiúsculas — as URLs dos links ficam intactas.

    `datas_novas` são as datas que não tinham nenhuma sessão antes: abrir a
    venda de um dia novo é mais urgente que um horário extra num dia já
    aberto, então ganha título e marcação 🆕 próprios.
    """
    por_data: dict[str, list[Sessao]] = {}
    for s in novas:
        por_data.setdefault(s.data, []).append(s)
    datas = sorted(por_data, key=chave_data)

    if datas_novas:
        titulo = "NOVA DATA" if len(datas_novas) == 1 else "NOVAS DATAS"
        linhas = [f"🟢🟢🟢 <b>{titulo}: DUNA - PARTE 3</b> 🟢🟢🟢"]
        for data in sorted(datas_novas, key=chave_data):
            linhas.append(
                f"🆕 <b>ABRIU A VENDA DE {html.escape(rotulo_data(data))}</b> "
                f"({len(por_data.get(data, []))} SESSÃO(ÕES))"
            )
    else:
        linhas = ["🟢🟢🟢 <b>NOVA SESSÃO: DUNA - PARTE 3</b> 🟢🟢🟢"]
    linhas += [f"📍 IMAX PALLADIUM (CURITIBA) · VIA <i>{html.escape(ORIGEM.upper())}</i>", ""]

    com_botao_por_sessao = len(novas) <= MAX_BOTOES_SESSAO
    for data in datas:
        marca = "🆕 " if data in datas_novas else ""
        linhas.append(f"🗓 <b>{marca}{html.escape(rotulo_data(data))}</b>")
        for s in sorted(por_data[data], key=lambda s: s.hora):
            detalhes = html.escape(descrever_sessao(s.para_state()))
            linha = f"  🕐 {html.escape(s.hora)} · {detalhes}"
            if s.link and not com_botao_por_sessao:
                linha += f' — <a href="{html.escape(s.link, quote=True)}">COMPRAR</a>'
            linhas.append(linha)
        linhas.append("")

    botoes_sessao = []
    if com_botao_por_sessao:
        for data in datas:
            for s in sorted(por_data[data], key=lambda s: s.hora):
                if s.link:
                    idioma = "/".join(t[:3] for t in s.tags if t.upper() != "IMAX").upper()
                    botoes_sessao.append(
                        (f"🎟 {rotulo_data(data, com_ano=False)} {s.hora} {idioma}".strip(), s.link)
                    )
    else:
        linhas.append("👆 TOQUE EM <b>COMPRAR</b> NA SESSÃO DESEJADA")
    botoes = [botoes_sessao[i : i + 2] for i in range(0, len(botoes_sessao), 2)]
    botoes.append([("📋 INGRESSO.COM", URL_INGRESSO), ("📋 IMAX PALLADIUM", URL_IMAX_PALLADIUM)])
    return "\n".join(linhas).rstrip(), botoes


def checar_removidas(state: dict, resultado: ResultadoBusca, agora: datetime) -> list[str]:
    """Detecta sessões já vistas que sumiram das fontes (esgotou, cancelou ou
    mudou de horário) e as que voltaram depois de sumir.

    Pra não gerar alarme falso:
    - só avalia quando TODAS as fontes ativas responderam sem erro/pausa, e
      não quando nenhuma sessão veio (aí é mais provável o parser ter
      quebrado — isso é papel do canário);
    - sessão que já aconteceu é ignorada (some naturalmente);
    - só avisa depois de AUSENCIAS_PRA_AVISAR checagens seguidas sem ela.
    Os detalhes da sessão ausente ficam em state["ausentes"], porque
    sessoes_atuais é sobrescrita a cada execução.
    """
    if set(resultado.contagens) != FONTES_ATIVAS or not resultado.sessoes:
        return []

    atuais = {s.chave for s in resultado.sessoes}
    anteriores = {s["chave"]: s for s in state.get("sessoes_atuais") or [] if s.get("chave")}
    ausentes = state.setdefault("ausentes", {})
    for chave, info in anteriores.items():
        if chave not in atuais and chave not in ausentes:
            ausentes[chave] = {"info": info, "checagens": 0, "avisado": False}

    removidas, voltaram = [], []
    for chave in list(ausentes):
        registro = ausentes[chave]
        info = registro["info"]
        inicio = inicio_sessao(info.get("data", ""), info.get("hora", ""))
        if inicio is None or inicio <= agora:
            del ausentes[chave]  # já aconteceu: sumir é o esperado
            continue
        if chave in atuais:
            if registro["avisado"]:
                voltaram.append(info)
            del ausentes[chave]
            continue
        registro["checagens"] += 1
        if registro["checagens"] >= AUSENCIAS_PRA_AVISAR and not registro["avisado"]:
            registro["avisado"] = True
            removidas.append(info)

    def listar(sessoes: list[dict]) -> list[str]:
        return [
            f"🗓 {html.escape(rotulo_data(s['data']))} · 🕐 {html.escape(s['hora'])} · "
            f"{html.escape(descrever_sessao(s))}"
            for s in sorted(sessoes, key=lambda s: (chave_data(s["data"]), s["hora"]))
        ]

    avisos = []
    if removidas:
        avisos.append(
            "\n".join(
                [f"🟠 <b>SESSÃO(ÕES) REMOVIDA(S): DUNA - PARTE 3</b> ({html.escape(ORIGEM.upper())})"]
                + listar(removidas)
                + ["SUMIU DE TODAS AS FONTES: PODE TER ESGOTADO, SIDO CANCELADA OU MUDADO DE HORÁRIO."]
            )
        )
    if voltaram:
        avisos.append(
            "\n".join(
                [f"🟢 <b>SESSÃO(ÕES) DE VOLTA: DUNA - PARTE 3</b> ({html.escape(ORIGEM.upper())})"]
                + listar(voltaram)
                + ["VOLTOU A APARECER À VENDA (PODEM TER LIBERADO INGRESSOS)."]
            )
        )
    return avisos


def montar_mensagem_sem_novidade() -> str:
    """Heartbeat de "continuo monitorando, nada encontrado": contraparte 🔴
    da mensagem de sessão nova (🟢), também toda em maiúsculas."""
    fontes_label = ", ".join(
        NOME_FONTE[f] for f in ("ingresso", "imax_palladium") if f in FONTES_ATIVAS
    )
    return (
        "🔴 <b>NENHUMA SESSÃO NOVA</b> 🔴\n"
        f"🔎 DUNA IMAX WATCHER ({html.escape(ORIGEM.upper())})\n"
        f"MONITORANDO IMAX PALLADIUM (CURITIBA) VIA {html.escape(fontes_label.upper())}.\n"
        "NENHUMA SESSÃO NOVA DE DUNA - PARTE 3 ATÉ AGORA."
    )


def main() -> None:
    state = load_state()
    vistas = set(state.get("sessoes_vistas", []))
    agora = datetime.now(timezone.utc)
    # Ao contrário de last_heartbeat (só atualiza quando um aviso sai), esse
    # campo marca toda execução — é o que o bot de comandos usa pra
    # responder "última execução" no /status.
    state["last_run"] = agora.isoformat()

    try:
        resultado = buscar_sessoes(pular=fontes_pausadas(state, agora))
    except Exception as e:
        print(f"Erro ao checar a página: {e}", file=sys.stderr)
        registrar_problema(state, descrever_excecao("geral", e), agora)
        save_state(state)
        # Não falha o workflow por instabilidade pontual do site
        sys.exit(0)

    for problema in resultado.problemas:
        registrar_problema(state, problema, agora)

    for aviso in aplicar_pausas(state, resultado, agora):
        send_telegram(aviso, parse_mode="HTML")
        print("Aviso de pausa/retomada de fonte enviado.")

    novas = [s for s in resultado.sessoes if s.chave not in vistas]

    for aviso in checar_canario(state, resultado.contagens):
        send_telegram(aviso, parse_mode="HTML")
        print("Alerta de canário enviado.")

    atualizar_resumo(state, sessoes_novas=len(novas), erros=resultado.erros, agora=agora)

    # Tem que rodar antes de sobrescrever sessoes_atuais: compara com ela.
    for aviso in checar_removidas(state, resultado, agora):
        send_telegram(aviso, parse_mode="HTML")
        print("Aviso de sessão removida/de volta enviado.")

    # Datas que já tiveram sessão, pra destacar quando abre a venda de um
    # dia novo. Na primeira vez, parte do que já se conhecia (sessões da
    # execução anterior, ou as já vistas), pra não anunciar tudo como novo.
    if "datas_conhecidas" not in state:
        base = {s["data"] for s in state.get("sessoes_atuais") or []}
        state["datas_conhecidas"] = sorted(base or {s.data for s in resultado.sessoes if s.chave in vistas})
    conhecidas = set(state["datas_conhecidas"])
    datas_novas = {s.data for s in novas} - conhecidas
    state["datas_conhecidas"] = sorted(conhecidas | {s.data for s in resultado.sessoes}, key=chave_data)

    # Detalhes das sessões atuais, pro /status do bot listá-las e pro
    # checar_removidas (sessoes_vistas guarda só os ids). Não sobrescreve se
    # todas as fontes falharam, pra não apagar a lista por instabilidade.
    if resultado.contagens:
        state["sessoes_atuais"] = [s.para_state() for s in resultado.sessoes]

    if novas:
        state["ultima_sessao_nova"] = agora.isoformat()
        texto, botoes = montar_mensagem_novas(novas, datas_novas)
        send_telegram(texto, parse_mode="HTML", botoes=botoes)
        print(f"Notificação enviada: {len(novas)} sessão(ões) nova(s), {len(datas_novas)} data(s) nova(s).")

        vistas.update(s.chave for s in resultado.sessoes)
        state["sessoes_vistas"] = sorted(vistas)
        state["last_heartbeat"] = agora.isoformat()  # a notificação já conta como aviso
    else:
        print("Nenhuma sessão nova encontrada.")
        if not HEARTBEAT_ATIVO:
            print("Heartbeat desativado nesta máquina (HEARTBEAT_ATIVO=0).")
        elif heartbeat_devido(state):
            send_telegram(montar_mensagem_sem_novidade(), parse_mode="HTML", silencioso=True)
            state["last_heartbeat"] = agora.isoformat()
            print("Heartbeat enviado.")
        else:
            print("Heartbeat ainda não é devido.")

    if RESUMO_ATIVO and resumo_devido(state):
        send_telegram(montar_resumo(state, agora), parse_mode="HTML", silencioso=True)
        reiniciar_resumo(state, agora)
        print("Resumo enviado.")

    save_state(state)


if __name__ == "__main__":
    main()