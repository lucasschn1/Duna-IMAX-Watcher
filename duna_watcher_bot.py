"""
Bot leve que escuta comandos no Telegram (long polling) e responde com o
status/resumo do watcher, lendo o state.json que o check_duna_imax.py já
mantém — não faz nenhuma checagem nova nem escreve em sessoes_vistas.

Ao contrário do check_duna_imax.py (rodado sob demanda por um timer/cron),
este script é um processo contínuo: fica em loop long-polling a API do
Telegram, então precisa de um serviço systemd Type=simple dedicado (veja o
README), separado do timer principal.

Comandos aceitos (sem diferenciar maiúsc./minúsc., com ou sem "/" na
frente, "?"/"!" no final tanto faz):
  status  -> snapshot rápido: sessões já vistas, última execução, alertas
             de canário (fonte com problema)
  resumo  -> mesmo texto do resumo periódico, mas sob demanda e sem
             resetar o contador
  problema -> último erro registrado (data/hora, origem, fonte, causa
              provável e stack) + lista dos anteriores

Só responde a mensagens vindas do TELEGRAM_CHAT_ID configurado — qualquer
outro remetente que escreva pro bot é ignorado.
"""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import check_duna_imax as watcher

OFFSET_FILE = Path(__file__).parent / "bot_offset.txt"
POLL_TIMEOUT = 30

# Sem checagem há mais que isso, o /status considera o watcher parado (🔴).
ATRASO_MAXIMO = timedelta(hours=2)


def carregar_offset() -> int | None:
    if OFFSET_FILE.exists():
        texto = OFFSET_FILE.read_text().strip()
        return int(texto) if texto else None
    return None


def salvar_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def montar_status() -> str:
    """Snapshot do watcher com um semáforo no topo, que aqui indica a saúde
    do monitoramento (não se achou sessão): 🟢 tudo ok, 🟡 alguma fonte com
    problema, 🔴 watcher parado (sem execução recente) ou todas as fontes
    com problema."""
    state = watcher.load_state()
    agora = datetime.now(timezone.utc)
    esc = watcher.html.escape

    ultima_execucao = state.get("last_run")
    ultima_dt = datetime.fromisoformat(ultima_execucao) if ultima_execucao else None
    parado = ultima_dt is None or agora - ultima_dt > ATRASO_MAXIMO

    fontes_monitoradas = list(state.get("contagem_por_fonte") or {})
    alertas = [f for f, ativo in (state.get("canario_alertado") or {}).items() if ativo]

    if parado or (fontes_monitoradas and len(alertas) >= len(fontes_monitoradas)):
        semaforo, situacao = "🔴", "ATENÇÃO"
    elif alertas:
        semaforo, situacao = "🟡", "FUNCIONANDO COM FALHAS"
    else:
        semaforo, situacao = "🟢", "TUDO OK"

    linhas = [
        f"{semaforo} <b>STATUS: {situacao}</b> {semaforo}",
        f"📟 {esc(watcher.ORIGEM.upper())}",
        "",
    ]
    if ultima_dt:
        aviso = " ⚠️ ATRASADA" if parado else ""
        linhas.append(
            f"🕐 ÚLTIMA CHECAGEM: {watcher.formatar_tempo_relativo(ultima_dt, agora)} "
            f"({watcher.formatar_data_hora(ultima_dt)}){aviso}"
        )
    else:
        linhas.append("🕐 ÚLTIMA CHECAGEM: DESCONHECIDA ⚠️")

    ultima_nova = state.get("ultima_sessao_nova")
    if ultima_nova:
        linhas.append(
            f"🆕 ÚLTIMA SESSÃO NOVA: "
            f"{watcher.formatar_tempo_relativo(datetime.fromisoformat(ultima_nova), agora)}"
        )

    fontes = watcher.linha_fontes(state)
    if fontes:
        linhas += ["", "<b>FONTES</b>", *fontes]

    linhas += ["", f"<b>🎬 SESSÕES CONHECIDAS ({len(state.get('sessoes_vistas', []))})</b>"]
    atuais = state.get("sessoes_atuais")
    if atuais:
        por_data: dict[str, list[dict]] = {}
        for s in atuais:
            por_data.setdefault(s["data"], []).append(s)
        for data in sorted(por_data, key=lambda d: d.split("/")[::-1]):
            horarios = " · ".join(
                f'<a href="{esc(s["link"], quote=True)}">{esc(s["hora"])}</a> '
                f'{esc("/".join(t[:3] for t in s["tags"] if t.upper() != "IMAX").upper())}'
                if s.get("link") else esc(s["hora"])
                for s in sorted(por_data[data], key=lambda s: s["hora"])
            )
            linhas.append(f"  🗓 {esc(data)}: {horarios}")
    elif not state.get("sessoes_vistas"):
        linhas.append("  NENHUMA AINDA.")
    else:
        linhas.append("  (DETALHES DISPONÍVEIS APÓS A PRÓXIMA CHECAGEM)")
    return "\n".join(linhas)


def montar_resumo_atual() -> str:
    state = watcher.load_state()
    if not (state.get("resumo") or {}).get("desde"):
        return "📊 <b>AINDA NÃO HÁ DADOS DE RESUMO ACUMULADOS.</b>"
    return watcher.montar_resumo(state, datetime.now(timezone.utc))


def montar_problema() -> str:
    """Detalha o problema mais recente (data/hora, origem, fonte, causa
    provável e stack) e lista os anteriores numa linha cada."""
    state = watcher.load_state()
    esc = watcher.html.escape
    problemas = state.get("problemas") or []
    if not problemas:
        return "🟢 <b>NENHUM PROBLEMA REGISTRADO</b> 🟢"

    agora = datetime.now(timezone.utc)
    ultimo = problemas[-1]
    quando = datetime.fromisoformat(ultimo["quando"])
    fonte = watcher.NOME_FONTE.get(ultimo["fonte"], ultimo["fonte"])

    linhas = [
        "🔴 <b>ÚLTIMO PROBLEMA</b> 🔴",
        f"🕐 {watcher.formatar_data_hora(quando)} ({watcher.formatar_tempo_relativo(quando, agora)})",
        f"📟 ORIGEM: {esc(ultimo.get('origem', '?').upper())}",
        f"🌐 FONTE: {esc(fonte.upper())}",
        f"🧩 ERRO: <code>{esc(ultimo['tipo'])}</code>: {esc(ultimo['mensagem'])}",
        "",
        f"💡 <b>PROVÁVEL CAUSA:</b> {esc(ultimo['causa'])}",
    ]
    if ultimo.get("stack"):
        # Limite do Telegram é 4096 caracteres por mensagem; o fim do stack
        # (onde estourou) é o que importa.
        linhas += ["", "<b>STACK</b>", f"<pre>{esc(ultimo['stack'][-2500:])}</pre>"]

    anteriores = problemas[-6:-1]
    if anteriores:
        linhas += ["", f"<b>ANTERIORES ({len(problemas) - 1} NO TOTAL)</b>"]
        for p in reversed(anteriores):
            nome = watcher.NOME_FONTE.get(p["fonte"], p["fonte"])
            linhas.append(
                f"  • {watcher.formatar_data_hora(datetime.fromisoformat(p['quando']))} · "
                f"{esc(nome.upper())} · {esc(p['tipo'])}"
            )
    return "\n".join(linhas)


COMANDOS = {
    "status": montar_status,
    "resumo": montar_resumo_atual,
    "problema": montar_problema,
    "problemas": montar_problema,
}


def processar_texto(texto: str) -> str | None:
    normalizado = texto.strip().lower().lstrip("/").rstrip("?!.")
    comando = COMANDOS.get(normalizado)
    return comando() if comando else None


def main() -> None:
    if not watcher.TELEGRAM_TOKEN or not watcher.TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não configurados; encerrando.", file=sys.stderr)
        sys.exit(1)

    url = f"https://api.telegram.org/bot{watcher.TELEGRAM_TOKEN}/getUpdates"
    offset = carregar_offset()
    print("Bot de comandos iniciado, escutando...")

    while True:
        params = {"timeout": POLL_TIMEOUT}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = requests.get(url, params=params, timeout=POLL_TIMEOUT + 10)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException as e:
            print(f"Erro consultando o Telegram: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            salvar_offset(offset)

            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            texto = msg.get("text", "")

            if chat_id != str(watcher.TELEGRAM_CHAT_ID):
                continue  # ignora qualquer remetente que não seja o dono

            resposta = processar_texto(texto)
            if resposta:
                watcher.send_telegram(resposta, parse_mode="HTML")


if __name__ == "__main__":
    main()
