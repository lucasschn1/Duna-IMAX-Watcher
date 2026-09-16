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

Só responde a mensagens vindas do TELEGRAM_CHAT_ID configurado — qualquer
outro remetente que escreva pro bot é ignorado.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import check_duna_imax as watcher

OFFSET_FILE = Path(__file__).parent / "bot_offset.txt"
POLL_TIMEOUT = 30


def carregar_offset() -> int | None:
    if OFFSET_FILE.exists():
        texto = OFFSET_FILE.read_text().strip()
        return int(texto) if texto else None
    return None


def salvar_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def montar_status() -> str:
    state = watcher.load_state()
    vistas = state.get("sessoes_vistas", [])

    ultima_execucao = state.get("last_run")
    if ultima_execucao:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(ultima_execucao)
        minutos = int(delta.total_seconds() // 60)
        quando = f"há {minutos} min" if minutos else "agora mesmo"
    else:
        quando = "desconhecida"

    alertas = [
        watcher.NOME_FONTE.get(fonte, fonte)
        for fonte, ativo in (state.get("canario_alertado") or {}).items()
        if ativo
    ]

    linhas = [
        f"📟 <b>Status</b> ({watcher.html.escape(watcher.ORIGEM)})",
        f"🎬 {len(vistas)} sessão(ões) de Duna já vista(s)",
        f"🕐 Última execução: {quando}",
    ]
    if alertas:
        nomes = ", ".join(watcher.html.escape(a) for a in alertas)
        linhas.append(f"⚠️ Fonte(s) com problema: {nomes}")
    else:
        linhas.append("✅ Nenhum problema detectado nas fontes.")
    return "\n".join(linhas)


def montar_resumo_atual() -> str:
    state = watcher.load_state()
    if not (state.get("resumo") or {}).get("desde"):
        return "📊 Ainda não há dados de resumo acumulados."
    return watcher.montar_resumo(state, datetime.now(timezone.utc))


COMANDOS = {
    "status": montar_status,
    "resumo": montar_resumo_atual,
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
