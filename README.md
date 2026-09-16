# duna-imax-watcher

Monitora as sessões de **Duna - Parte 3** no IMAX Shopping Palladium
(Curitiba) e avisa no Telegram assim que uma sessão nova aparecer. Duas
fontes são checadas de forma independente, para não depender de uma só:
a página de sessões do cinema no ingresso.com e a página do próprio IMAX
Palladium.

100% grátis: roda em GitHub Actions (repositório público = minutos
ilimitados), sem servidor, sem banco de dados pago.

## Setup

1. **Criar o bot no Telegram**
   - Fale com [@BotFather](https://t.me/BotFather), use `/newbot`, guarde o
     token.
   - Mande qualquer mensagem para o seu bot recém-criado.
   - Acesse `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador e
     copie o valor de `chat.id`.

2. **Criar este repositório no GitHub** (público, para Actions grátis
   ilimitado) e subir estes arquivos.

3. **Adicionar os secrets** em `Settings > Secrets and variables > Actions`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

4. **Rodar manualmente uma vez** pela aba *Actions* → *Checar sessões IMAX
   Duna Parte 3* → *Run workflow*, para confirmar que a mensagem chega.

5. O cron já está configurado para rodar a cada 30 minutos
   (`.github/workflows/check-duna.yml`). Diminua o intervalo (ex: `*/10`)
   perto da estreia (15/12/2026, segundo a página do filme no ingresso.com).

## Como a detecção funciona

A pré-venda geral do filme começou em 10/09/2026 (habilita a página do
filme e o "lembre-me"), mas as sessões específicas (dia/hora/sala) só
aparecem quando o cinema efetivamente abre a venda para cada data. Sessões
para 15/12 e 16/12/2026 já estão abertas na página do IMAX Palladium — é
esse tipo de publicação que o watcher detecta.

`buscar_sessoes()` (`check_duna_imax.py`) combina duas fontes:

- **ingresso.com** (`buscar_sessoes_ingresso`): a página de sessões do
  cinema usa Tailwind puro, sem nenhuma classe ou `data-testid` com
  "session"/"sessao" — um seletor CSS nunca encontraria nada ali. Em vez
  disso, lemos o bloco `<script type="application/ld+json">` que a página
  já embute (dados schema.org `ScreeningEvent`, usados para SEO/Google) e
  filtramos pelo título do filme (`TITULO_FILME = "duna"`), já que essa
  página lista todos os filmes em cartaz, não só Duna. Como essa página
  precisa de JavaScript pra renderizar, usamos Playwright (Chromium
  headless) aqui.
- **imaxpalladium.com.br** (`buscar_sessoes_imax_palladium`): página do
  próprio cinema dedicada a este filme. É HTML estático (renderizado no
  servidor), então basta `requests` + BeautifulSoup, sem navegador — mais
  rápido e mais robusto que a fonte acima.

As duas fontes linkam pro mesmo `checkout.ingresso.com/?sessionId=...`, e é
esse `sessionId` (extraído em `extrair_session_id`) que usamos como chave
de "sessão já vista" — assim, se as duas fontes acharem a mesma sessão, só
a primeira notifica, sem duplicar aviso. Se uma fonte falhar (site fora do
ar, mudança de estrutura), a outra continua funcionando normalmente.

## Deploy adicional num servidor próprio (opcional)

O `schedule` do GitHub Actions é "melhor esforço": pode atrasar horas em
período de alta carga, especialmente porque o cron deste repo dispara em
`:00`/`:30` de cada hora, quando todo mundo dispara junto. Se você tem uma
máquina ligada 24h, dá pra rodar a fonte `imax_palladium` nela com muito
mais frequência (ela é só `requests` + BeautifulSoup, sem navegador — leve
o suficiente pra qualquer hardware) e deixar o GitHub Actions só como
backup redundante (com as duas fontes, incluindo a pesada via Playwright).

Env vars que controlam isso:

- `FONTES_ATIVAS` (padrão `ingresso,imax_palladium`): lista separada por
  vírgula de quais fontes rodar. Um deploy leve usa só `imax_palladium` —
  o `import` do Playwright só acontece se `ingresso` estiver na lista, então
  essa máquina nem precisa ter o pacote instalado.
- `HEARTBEAT_ATIVO` (padrão `1`): `0` desliga o aviso periódico de "continuo
  monitorando" nesta máquina, pra não duplicar esse aviso com o do GitHub
  Actions.
- `ORIGEM` (padrão `GitHub Actions`): identifica, na mensagem do Telegram,
  qual deploy mandou o aviso — importante porque os dois rodam em paralelo
  e cada sessão notificada já diz de qual site veio ([Ingresso.com] ou
  [IMAX Palladium]), mas não de qual máquina. No servidor próprio, use algo
  como `ORIGEM=Servidor` pra diferenciar.
- `RESUMO_ATIVO` (padrão `0`): `1` liga um resumo periódico ("X checagens,
  Y sessões novas, Z erros") — pensado pro deploy que roda com mais
  frequência, já que ele tem uma amostra maior e mais confiável que o
  heartbeat do GitHub Actions (que já sofre com o atraso do `schedule`).
  Fica desligado por padrão nos dois deploys pra não duplicar.
- `RESUMO_INTERVALO_DIAS` (padrão `3`): de quantos em quantos dias manda
  esse resumo.

Independente de `HEARTBEAT_ATIVO`/`RESUMO_ATIVO`, todo deploy tem um
"canário": se uma fonte que antes achava sessão passar a achar 0 de
repente (ex: o site mudou de estrutura e o parser quebrou, em vez de
simplesmente "ainda não abriu venda"), sai um alerta imediato — só uma vez
por transição, sem virar spam enquanto continuar zerado.

Passos num servidor Ubuntu, por exemplo:

```bash
mkdir -p ~/duna-watcher && cd ~/duna-watcher
# copie check_duna_imax.py e requirements-light.txt deste repo pra cá
python3 -m venv venv
./venv/bin/pip install -r requirements-light.txt
```

Crie `/etc/duna-watcher.env` (permissão restrita, só o dono lê):

```
TELEGRAM_BOT_TOKEN=xxxx
TELEGRAM_CHAT_ID=xxxx
FONTES_ATIVAS=imax_palladium
HEARTBEAT_ATIVO=0
ORIGEM=Servidor
RESUMO_ATIVO=1
```

```bash
sudo chmod 600 /etc/duna-watcher.env
```

Crie `/etc/systemd/system/duna-watcher.service`:

```ini
[Unit]
Description=Duna IMAX Watcher (fonte leve)

[Service]
Type=oneshot
EnvironmentFile=/etc/duna-watcher.env
WorkingDirectory=/home/SEU_USUARIO/duna-watcher
ExecStart=/home/SEU_USUARIO/duna-watcher/venv/bin/python check_duna_imax.py
```

E `/etc/systemd/system/duna-watcher.timer`:

```ini
[Unit]
Description=Roda o Duna IMAX Watcher a cada 1 minuto

[Timer]
OnBootSec=30
OnUnitActiveSec=60
Unit=duna-watcher.service

[Install]
WantedBy=timers.target
```

Ative com:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now duna-watcher.timer
```

`state.json` fica só nessa pasta local, sem git — não precisa (e não deve)
ser commitado de volta pro repositório.

## Bot de comandos (opcional, só no deploy com servidor próprio)

Além dos avisos automáticos, dá pra mandar uma mensagem pro bot e ele
responder na hora, com dois comandos (sem diferenciar maiúsc./minúsc., com
ou sem `/` na frente):

- `status` — sessões já vistas, quando foi a última execução, e se alguma
  fonte está com o canário disparado (parou de achar sessão).
- `resumo` — o mesmo texto do resumo periódico, mas sob demanda, sem
  esperar `RESUMO_INTERVALO_DIAS` nem resetar o contador.

Isso é feito com `duna_watcher_bot.py`, via long polling
(`getUpdates`) — o processo fica de olho por mensagens novas sem precisar
expor nenhuma porta nem endereço público (só chamadas de saída pro
Telegram, iguais às de mandar mensagem). Só responde a mensagens vindas do
`TELEGRAM_CHAT_ID` configurado; qualquer outro remetente é ignorado.

Diferente do `check_duna_imax.py` (chamado sob demanda pelo timer), esse
script é um **processo contínuo** — precisa do seu próprio serviço
systemd, `Type=simple` (não `oneshot`+timer). Copie `duna_watcher_bot.py`
pra mesma pasta (`~/duna-watcher`) e crie
`/etc/systemd/system/duna-watcher-bot.service`:

```ini
[Unit]
Description=Duna IMAX Watcher - bot de comandos (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/duna-watcher.env
WorkingDirectory=/home/SEU_USUARIO/duna-watcher
ExecStart=/home/SEU_USUARIO/duna-watcher/venv/bin/python -u duna_watcher_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

(o `-u` evita que a saída fique presa em buffer e não apareça no
`journalctl` em tempo real.)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now duna-watcher-bot.service
```
