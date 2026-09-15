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
