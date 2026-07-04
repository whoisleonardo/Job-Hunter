# Job Hunter 🎯

Caçador de vagas que roda **24/7 num servidor** e avisa cada usuário no **Telegram** — mesmo com o PC desligado. Busca em Adzuna, Jooble, LinkedIn/Indeed (via Google Jobs/SerpApi) e vagas internacionais remotas, pontua o encaixe com o currículo usando **IA (provedor à escolha do usuário)** e só envia as vagas novas acima do corte de match.

```
App configurador (navegador)          Servidor (24/7)                    Usuário
┌──────────────────────────┐   HTTPS  ┌──────────────────────────┐      ┌──────────┐
│ job_hunter_gui.py         │ ───────▶ │ backend.py               │ ───▶ │ Telegram │
│ • preferências e filtros  │          │ • agendador por usuário  │      │ 🟢 92%   │
│ • provedor de IA + chave  │          │ • busca multi-fonte      │      │  match!  │
│ • currículo               │          │ • match por IA + dedupe  │      └──────────┘
└──────────────────────────┘          └──────────────────────────┘
```

## Componentes

| Arquivo | Papel |
|---|---|
| `backend.py` | API Flask + agendador. Guarda chaves e preferências de cada usuário (por código de acesso), roda a varredura no intervalo configurado e envia ao Telegram. |
| `job_hunter_gui.py` | App configurador. Sobe um servidor local, abre a interface no navegador e faz proxy pro backend. **Não precisa ficar aberto** — só salva as preferências. |

## Para quem vai usar (amigos)

1. Rode o app (`python job_hunter_gui.py` ou o executável recebido)
2. Aba **Conta**: cole seu código de acesso
3. No Telegram: procure o bot, dê `/start` e mande seu código como mensagem
4. No app: **Detectar meu Chat ID** → **Testar Telegram**
5. (Opcional) Escolha seu provedor de IA — Anthropic, OpenAI, DeepSeek, Groq, Ollama local ou qualquer API compatível — e cole sua chave. Sem chave, o match usa heurística por palavra-chave
6. Aba **Preferências**: cargo, filtros, corte de match → **Salvar** e ative a varredura

Pronto. Pode fechar o app: as vagas chegam sozinhas no Telegram.

## Para quem vai hospedar (dev)

### Instalar e configurar

```bash
pip install -r requirements.txt
cp .env.example .env    # preencha com suas chaves (todas têm plano grátis)
```

- Adzuna → https://developer.adzuna.com
- Jooble → https://jooble.org/api/about
- SerpApi (LinkedIn/Indeed) → https://serpapi.com
- Telegram Bot Token → @BotFather
- `ACCESS_CODES` → um código por amigo, ex.: `ANA-7F3K,JOAO-92LM`
- `LLM_API_KEY` → opcional; fallback global de IA pra quem não informar a própria

### Rodar local (teste)

```bash
python backend.py                                       # API em :8000
JOBHUNTER_BACKEND=http://localhost:8000 python job_hunter_gui.py
```

### Deploy 24/7 (VPS)

O backend é um processo só — serve qualquer VPS/host sempre ligado:

```bash
# como serviço systemd (exemplo)
gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 backend:app
```

> ⚠️ **`-w 1` é obrigatório**: mais de 1 worker duplica o agendador (vagas enviadas 2×). Concorrência vem das threads.

Recomendado: HTTPS via reverse proxy (Caddy/nginx). Com Caddy, um bloco resolve — certificado automático:

```caddyfile
jobhunter.SEU-IP.sslip.io {
    reverse_proxy host.docker.internal:8000
}
```

`prefs.json`/`seen.json` são gravados em `DATA_DIR` (padrão: pasta do app) — em disco persistente, sobrevivem a restart.

### Gerar executável pros amigos

Troque `BACKEND_URL` no `job_hunter_gui.py` pela URL pública e:

```bash
pip install pyinstaller
pyinstaller --onefile job_hunter_gui.py
```

Entregue o executável + o código de acesso de cada um. Código vazou? Remova de `ACCESS_CODES` e reinicie o backend.

## Como foi feito

- **Backend**: Flask + `requests`, sem banco — persistência em JSON com escrita atômica. Um agendador em thread verifica a cada minuto quem precisa rodar, respeitando o intervalo de cada usuário. Dedupe por hash de título+empresa+local; rate limit por código de acesso.
- **Match por IA multi-provedor**: cada usuário escolhe o provedor nas preferências. `llm_match` despacha entre a API da Anthropic (`/v1/messages`) e o formato OpenAI (`/v1/chat/completions` — cobre OpenAI, DeepSeek, Groq, Ollama e afins). Mesmo prompt, mesmo contrato de saída (`{score, reason, company_desc}`). Timeout, HTTP ≥ 400 ou JSON inválido **nunca derrubam a rodada**: a vaga cai no ranking heurístico por palavras-chave.
- **Interface**: gerada no [Claude Design](https://claude.ai) (dark, preto + vermelho) e transformada em app real — o `job_hunter_gui.py` embute o HTML/CSS/JS e sobe um servidor `http.server` local (só `127.0.0.1`) que serve a página e faz proxy das chamadas pro backend, evitando CORS sem dependência nova. A config local fica em `job_hunter_config.json`.
- **Segurança**: chaves de fonte de vagas e do bot ficam só no servidor; a chave de IA do usuário é gravada no backend mas **nunca retorna** nas respostas da API. Todas as rotas exigem código de acesso válido, com rate limit por hora.
- **Ferramentas**: desenvolvido em par com o [Claude Code](https://claude.com/claude-code) — do refactor multi-provedor ao deploy com systemd + Caddy.

## Segurança

- O executável dos amigos não contém nenhuma chave — só a URL do backend
- Cada amigo tem um código próprio, revogável, com rate limit
- Sem chave de IA, o match degrada graciosamente pra heurística
- Se alguma credencial já foi commitada antes, **gere novas** — o histórico do Git guarda a antiga
