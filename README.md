# duna-imax-watcher

Monitora a página de sessões do IMAX Shopping Palladium (Curitiba) no
ingresso.com e avisa no Telegram assim que uma sessão de **Duna - Parte 3**
aparecer.

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
   perto da pré-venda antecipada (15-16/12/2026) ou da estreia (17/12/2026).

## Ajuste importante

O seletor CSS em `buscar_sessoes()` (`check_duna_imax.py`) é **provisório**,
porque a estrutura real do HTML de sessões só pode ser inspecionada quando o
ingresso.com efetivamente publicar sessões do filme (hoje a página mostra
"Ainda não temos sessões"). Quando qualquer outro filme estiver em cartaz no
IMAX Palladium, abra a página de sessões dele no navegador, use "Inspecionar
elemento" no card de uma sessão e ajuste o seletor em
`page.query_selector_all(...)` para bater com a classe/atributo real.
