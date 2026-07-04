#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Job Hunter — BACKEND com agendador (roda 24/7, PC do usuário desligado)
=======================================================================

O servidor faz TODO o trabalho:
  - guarda as PREFERÊNCIAS de cada código de acesso (cargo, filtros, chat_id...);
  - um AGENDADOR interno roda a busca de cada usuário no intervalo dele;
  - agrega as fontes, pontua (IA ou heurístico), deduplica e envia as vagas
    NOVAS pro Telegram de cada um.
O app (.exe) vira só um configurador: salva preferências aqui e pode disparar
uma busca de teste. Ele não precisa ficar aberto.

Chaves ficam AQUI (variáveis de ambiente), nunca no .exe.

Instalação:
  pip install flask requests

Variáveis de ambiente (defina no host — Render/Railway/Fly/VPS):
  ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_API_KEY, SERPAPI_KEY, TELEGRAM_BOT_TOKEN
  ACCESS_CODES     -> "ANA-7F3K,JOAO-92LM"   (lista de códigos válidos)
  LLM_API_KEY      -> (opcional) chave de IA GLOBAL do servidor, usada como
                       fallback quando o usuário não informa a própria. Cada
                       usuário escolhe o provedor (Anthropic, DeepSeek, OpenAI,
                       Groq, Ollama, ...) nas suas preferências. Sem chave
                       nenhuma, o match cai no modo heurístico.
                       (ANTHROPIC_API_KEY ainda é aceita por compatibilidade.)
  RATE_PER_HOUR    -> (opcional) limite de requisições por código/hora (120)
  DATA_DIR         -> (opcional) pasta para persistir prefs/seen (padrão: .)

Rodar:
  python backend.py        (inicia o agendador + a API)
"""

import os
import re
import json
import time
import threading
import hashlib

import requests
from flask import Flask, request, jsonify

# Carrega variáveis do arquivo .env, se existir (não obrigatório em produção,
# onde as variáveis costumam vir do painel do host).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # sem python-dotenv, seguimos só com as variáveis de ambiente do sistema

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Configuração vinda do ambiente
# --------------------------------------------------------------------------- #
KEYS = {
    "adzuna_app_id": os.getenv("ADZUNA_APP_ID", ""),
    "adzuna_app_key": os.getenv("ADZUNA_APP_KEY", ""),
    "jooble_api_key": os.getenv("JOOBLE_API_KEY", ""),
    "serpapi_key": os.getenv("SERPAPI_KEY", ""),
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
}
# Chave de IA do servidor: fallback GLOBAL opcional, usado só quando o usuário
# não informa a própria chave. Aceita a nova LLM_API_KEY ou a antiga
# ANTHROPIC_API_KEY (compatibilidade). Sem nenhuma, o match cai no heurístico.
SERVER_LLM_KEY = (os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")).strip()
ACCESS_CODES = {c.strip() for c in os.getenv("ACCESS_CODES", "").split(",") if c.strip()}
RATE_PER_HOUR = int(os.getenv("RATE_PER_HOUR", "120"))
DATA_DIR = os.getenv("DATA_DIR", ".")
TICK_SECONDS = 60  # de quanto em quanto o agendador verifica quem precisa rodar

PREFS_FILE = os.path.join(DATA_DIR, "prefs.json")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")

_lock = threading.Lock()
_hits = {}          # rate limit: code -> [timestamps]
_last_run = {}      # code -> epoch da última varredura
_sched_started = False

DEFAULT_PREFS = {
    "enabled": False,
    "chat_id": "",
    "keywords": "desenvolvedor java",
    "location": "Curitiba, PR",
    "country": "br",
    "levels": ["junior"],
    "work_types": ["remoto", "presencial"],
    "sources": ["adzuna", "jooble", "google_jobs"],
    "match_threshold": 70,
    "use_llm": True,
    # Provedor de IA escolhido por usuário (sem padrão imposto pelo servidor):
    #   "anthropic"     -> API da Anthropic (/v1/messages, x-api-key)
    #   "openai_compat" -> DeepSeek, OpenAI, Groq, Ollama, etc. (/v1/chat/completions)
    "llm_provider": "anthropic",
    "llm_base_url": "",        # vazio => usa o padrão do provedor (DEFAULT_BASE_URLS)
    "llm_model": "claude-haiku-4-5-20251001",
    "describe_company": True,
    "resume": "",
    "interval_minutes": 120,
    "max_results": 25,
    # Chave única do provedor escolhido; se vazia, usa a do servidor (ou heurístico).
    "llm_api_key": "",
    # Legado: mantido só por compatibilidade com prefs já salvas (migra p/ llm_api_key).
    "anthropic_api_key": "",
}
ALLOWED_PREF_KEYS = set(DEFAULT_PREFS)

# Padrões sensatos por provedor (usados quando o usuário não define base_url/modelo).
DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai_compat": "https://api.openai.com",
}
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai_compat": "gpt-4o-mini",
}


# --------------------------------------------------------------------------- #
# Persistência simples em JSON (troque por um banco em produção séria)
# --------------------------------------------------------------------------- #
def _load(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


prefs = _load(PREFS_FILE, {})   # code -> prefs
seen = _load(SEEN_FILE, {})     # code -> { uid: {...} }


def save_prefs():
    with _lock:
        _save(PREFS_FILE, prefs)


def save_seen():
    with _lock:
        _save(SEEN_FILE, seen)


# --------------------------------------------------------------------------- #
# Guardas de acesso / rate limit
# --------------------------------------------------------------------------- #
def code_ok(code):
    return bool(code) and code in ACCESS_CODES


def rate_ok(code):
    now = time.time()
    with _lock:
        window = [t for t in _hits.get(code, []) if now - t < 3600]
        if len(window) >= RATE_PER_HOUR:
            _hits[code] = window
            return False
        window.append(now)
        _hits[code] = window
        return True


def guard():
    d = request.get_json(silent=True) or {}
    code = (d.get("code") or "").strip()
    if not code_ok(code):
        return None, (jsonify({"error": "Código de acesso inválido."}), 403)
    if not rate_ok(code):
        return None, (jsonify({"error": "Limite de requisições atingido."}), 429)
    return code, None


# --------------------------------------------------------------------------- #
# HTTP helper + salário
# --------------------------------------------------------------------------- #
def http_json(method, url, *, params=None, json_body=None, headers=None, timeout=25):
    s = requests.Session()
    s.headers.update({"User-Agent": "job-hunter-backend/2.0", "Accept": "application/json"})
    if headers:
        s.headers.update(headers)
    resp = s.request(method, url, params=params, json=json_body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


CURRENCY = {"br": "R$", "gb": "£", "us": "$", "ca": "C$", "au": "A$", "de": "€", "fr": "€"}


def fmt_salary(mn, mx, country="br"):
    sym = CURRENCY.get(country, "")
    def n(x):
        try:
            return f"{int(float(x)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return None
    lo, hi = n(mn), n(mx)
    if lo and hi and lo != hi:
        return f"{sym} {lo}–{hi}".strip()
    if lo or hi:
        return f"{sym} {lo or hi}".strip()
    return "Não informado"


# --------------------------------------------------------------------------- #
# Detecção de nível e tipo
# --------------------------------------------------------------------------- #
LEVEL_PATTERNS = {
    "estagio": r"\b(est[aá]gi|intern|trainee)\w*",
    "junior": r"\b(j[uú]nior|junior|\bjr\b)\w*",
    "pleno": r"\b(pleno|\bpl\b|mid[- ]?level)\w*",
    "senior": r"\b(s[eê]nior|senior|\bsr\b|especialista|staff|lead|principal|tech[ -]?lead)\w*",
}
WT_PATTERNS = {
    "remoto": r"\b(remoto|remote|home[ -]?office|anywhere)\w*",
    "hibrido": r"\b(h[ií]brid|hybrid)\w*",
    "presencial": r"\b(presencial|on[- ]?site|no local|escrit[oó]rio)\w*",
}


def detect_level(text):
    t = text.lower()
    for lvl in ("senior", "pleno", "junior", "estagio"):
        if re.search(LEVEL_PATTERNS[lvl], t):
            return lvl
    return "indefinido"


def detect_wt(text, wfh=False):
    if wfh:
        return "remoto"
    t = text.lower()
    for wt in ("remoto", "hibrido", "presencial"):
        if re.search(WT_PATTERNS[wt], t):
            return wt
    return "indefinido"


def uid_of(j):
    base = f"{j.get('title','')}|{j.get('company','')}|{j.get('location','')}".lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Fontes de vagas
# --------------------------------------------------------------------------- #
def src_adzuna(q, loc, country, limit):
    if not (KEYS["adzuna_app_id"] and KEYS["adzuna_app_key"]):
        return []
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {"app_id": KEYS["adzuna_app_id"], "app_key": KEYS["adzuna_app_key"],
              "what": q, "where": loc, "results_per_page": limit,
              "content-type": "application/json", "sort_by": "date"}
    out = []
    for j in http_json("GET", url, params=params).get("results", []):
        locd = j.get("location") or {}
        area = ", ".join(locd.get("area", [])) if locd.get("area") else locd.get("display_name", loc)
        out.append({"title": j.get("title", ""), "company": (j.get("company") or {}).get("display_name", "—"),
                    "location": locd.get("display_name", loc), "url": j.get("redirect_url", ""),
                    "source": "adzuna", "description": j.get("description", "") or "",
                    "posted": (j.get("created", "") or "")[:10],
                    "salary": fmt_salary(j.get("salary_min"), j.get("salary_max"), country),
                    "address": area, "work_from_home": False})
    return out


def src_jooble(q, loc, limit):
    if not KEYS["jooble_api_key"]:
        return []
    data = http_json("POST", f"https://jooble.org/api/{KEYS['jooble_api_key']}",
                     json_body={"keywords": q, "location": loc})
    out = []
    for j in data.get("jobs", [])[:limit]:
        out.append({"title": j.get("title", ""), "company": j.get("company", "—") or "—",
                    "location": j.get("location", loc) or loc, "url": j.get("link", ""),
                    "source": "jooble", "description": j.get("snippet", "") or "",
                    "posted": (j.get("updated", "") or "")[:10],
                    "salary": (j.get("salary") or "").strip() or "Não informado",
                    "address": j.get("location", "") or "", "work_from_home": False})
    return out


def src_google_jobs(q, loc, country, limit):
    if not KEYS["serpapi_key"]:
        return []
    params = {"engine": "google_jobs", "q": f"{q} {loc}", "hl": "pt-br",
              "gl": country, "api_key": KEYS["serpapi_key"]}
    out = []
    for j in http_json("GET", "https://serpapi.com/search.json", params=params).get("jobs_results", [])[:limit]:
        via = (j.get("via", "") or "").replace("via ", "").strip().lower()
        ext = j.get("detected_extensions", {}) or {}
        link = (j.get("apply_options") or [{}])[0].get("link", "") or j.get("share_link", "")
        source = via if via in ("linkedin", "indeed") else "google"
        out.append({"title": j.get("title", ""), "company": j.get("company_name", "—") or "—",
                    "location": j.get("location", loc) or loc, "url": link, "source": source,
                    "description": j.get("description", "") or "", "posted": ext.get("posted_at", "") or "",
                    "salary": str(ext.get("salary")) if ext.get("salary") else "Não informado",
                    "address": j.get("location", "") or "", "work_from_home": bool(ext.get("work_from_home", False))})
    return out


def src_internacional(q, limit):
    term = q.split()[-1] if q else "java"
    out = []
    try:
        data = http_json("GET", "https://remotive.com/api/remote-jobs", params={"search": term, "limit": limit})
        for j in data.get("jobs", [])[:limit]:
            desc = re.sub(r"<[^>]+>", " ", j.get("description", "") or "")[:2000]
            out.append({"title": j.get("title", ""), "company": j.get("company_name", "—") or "—",
                        "location": j.get("candidate_required_location", "Remoto (global)") or "Remoto (global)",
                        "url": j.get("url", ""), "source": "remotive", "description": desc,
                        "posted": (j.get("publication_date", "") or "")[:10],
                        "salary": (j.get("salary") or "").strip() or "Não informado",
                        "address": "Remoto (internacional)", "work_from_home": True})
    except requests.RequestException:
        pass
    try:
        rows = http_json("GET", "https://remoteok.com/api")
        got = 0
        for j in rows if isinstance(rows, list) else []:
            if not isinstance(j, dict) or "position" not in j:
                continue
            title = j.get("position", "") or j.get("title", "")
            if term.lower() not in (title + " " + " ".join(j.get("tags", []))).lower():
                continue
            desc = re.sub(r"<[^>]+>", " ", j.get("description", "") or "")[:2000]
            out.append({"title": title, "company": j.get("company", "—") or "—",
                        "location": j.get("location", "Remoto (global)") or "Remoto (global)",
                        "url": j.get("url", ""), "source": "remoteok", "description": desc,
                        "posted": (j.get("date", "") or "")[:10],
                        "salary": (f"${j.get('salary_min')}–${j.get('salary_max')}" if j.get("salary_min") else "Não informado"),
                        "address": "Remoto (internacional)", "work_from_home": True})
            got += 1
            if got >= limit:
                break
    except requests.RequestException:
        pass
    return out


def aggregate(p):
    q, loc, country = p["keywords"], p["location"], p.get("country", "br")
    limit = int(p.get("max_results", 25))
    jobs = []
    srcs = p.get("sources", [])
    try:
        if "adzuna" in srcs:
            jobs += src_adzuna(q, loc, country, limit)
        if "jooble" in srcs:
            jobs += src_jooble(q, loc, limit)
        if "google_jobs" in srcs:
            jobs += src_google_jobs(q, loc, country, limit)
        if "internacional" in srcs:
            jobs += src_internacional(q, limit)
    except requests.RequestException:
        pass
    return jobs


# --------------------------------------------------------------------------- #
# Pontuação + match por IA
# --------------------------------------------------------------------------- #
def score_and_filter(jobs, p):
    keywords = [k for k in re.split(r"[ ,]+", p["keywords"].lower()) if k]
    wl, wt = set(p.get("levels", [])), set(p.get("work_types", []))
    result = []
    for j in jobs:
        text = (j["title"] + " " + j.get("description", "")).lower()
        hits = [k for k in keywords if k in text]
        if not hits:
            continue
        j["level"] = detect_level(text)
        j["work_type"] = detect_wt(text, j.get("work_from_home", False))
        if j["level"] != "indefinido" and j["level"] not in wl:
            continue
        if j["work_type"] != "indefinido" and j["work_type"] not in wt:
            continue
        j["score"] = len(hits) + (2 if j["level"] in wl else 0) + (1 if j["work_type"] in wt else 0)
        j["uid"] = uid_of(j)
        result.append(j)
    return result


def _llm_key(p):
    """Chave a usar: a do usuário (nova ou legada) ou a global do servidor."""
    return (p.get("llm_api_key") or p.get("anthropic_api_key") or SERVER_LLM_KEY or "").strip()


def _llm_provider(p):
    """(provider, base_url, model) já resolvidos, com padrões por provedor."""
    provider = (p.get("llm_provider") or "anthropic").strip().lower()
    if provider not in DEFAULT_BASE_URLS:
        provider = "anthropic"
    base = (p.get("llm_base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URLS[provider]
    if base.endswith("/v1"):          # usuário colou a URL já com /v1: evita /v1/v1
        base = base[:-3].rstrip("/")
    model = (p.get("llm_model") or "").strip() or DEFAULT_MODELS[provider]
    return provider, base, model


def _heuristic_match(j, reason, cap):
    """Fallback por palavras-chave. Nunca derruba a rodada; não usa a rede."""
    j["match_score"] = min(60 + j.get("score", 0) * 8, cap)
    j["match_reason"] = reason
    j.setdefault("company_desc", j.get("company_desc", ""))


def _call_anthropic(base, key, model, prompt):
    data = http_json("POST", f"{base}/v1/messages",
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                     json_body={"model": model, "max_tokens": 400,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=30)
    return "".join(b.get("text", "") for b in data.get("content", []))


def _call_openai_compat(base, key, model, prompt):
    url = f"{base}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": model, "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    try:
        data = http_json("POST", url, headers=headers, json_body=body, timeout=30)
    except requests.RequestException:
        # Alguns provedores/modelos (ex.: Ollama antigo) não aceitam response_format:
        # tenta de novo sem ele antes de desistir para o heurístico.
        body.pop("response_format", None)
        data = http_json("POST", url, headers=headers, json_body=body, timeout=30)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def llm_match(j, p):
    key = _llm_key(p)
    if not (p.get("use_llm") and key):
        _heuristic_match(j, "Match estimado por palavras-chave.", 95)
        return
    provider, base, model = _llm_provider(p)
    prompt = (
        "Você avalia o encaixe entre um candidato e uma vaga.\n\n"
        f"CURRÍCULO:\n{p.get('resume','')}\n\n"
        f"VAGA:\nTítulo: {j['title']}\nEmpresa: {j['company']}\n"
        f"Local/Tipo: {j['location']} ({j.get('work_type','')})\n"
        f"Descrição: {j.get('description','')[:1500]}\n\n"
        "Responda SOMENTE JSON válido, sem markdown:\n"
        '{"score": <0-100>, "reason": "<1-2 frases PT; ⚠️ se houver lacuna>", '
        '"company_desc": "<1 frase factual da empresa; vazio se não conhecer>"}'
    )
    try:
        if provider == "openai_compat":
            text = _call_openai_compat(base, key, model, prompt)
        else:
            text = _call_anthropic(base, key, model, prompt)
        text = (text or "").strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        parsed = json.loads(text)
        j["match_score"] = int(parsed.get("score", 0))
        j["match_reason"] = str(parsed.get("reason", "")).strip()
        if p.get("describe_company") and not j.get("company_desc"):
            j["company_desc"] = str(parsed.get("company_desc", "")).strip()
    except Exception:
        # Timeout, HTTP >= 400 ou JSON inválido: cai no heurístico só desta vaga.
        _heuristic_match(j, "Match estimado por palavras-chave (IA indisponível).", 90)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_send(chat_id, text):
    token = KEYS["telegram_bot_token"]
    if not (token and chat_id):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False


def format_html(j):
    tag = {"linkedin": "🔗 LinkedIn", "indeed": "🟦 Indeed",
           "remotive": "🌎 Remotive", "remoteok": "🌎 RemoteOK"}.get(j["source"], j["source"].title())
    lvl = j.get("level", "?"); lvl = lvl if lvl != "indefinido" else "?"
    wt = j.get("work_type", "?"); wt = wt if wt != "indefinido" else "?"
    ms = j.get("match_score", 0)
    dot = "🟢" if ms >= 80 else ("🟡" if ms >= 70 else "🔴")
    link = f'<a href="{j["url"]}">{j["title"]}</a>' if j.get("url") else j["title"]
    msg = (f"{dot} <b>{ms}% match · {lvl.capitalize()}</b>\n<b>{link}</b>\n"
           f"🏢 {j['company']} · 📍 {j['location']} · 🏠 {wt}\n🔎 via {tag}\n💰 {j.get('salary','Não informado')}")
    if j.get("address"):
        msg += f"\n🗺️ {j['address']}"
    if j.get("company_desc"):
        msg += f"\nℹ️ {j['company_desc']}"
    if j.get("match_reason"):
        msg += f"\n\n💡 <i>{j['match_reason']}</i>"
    msg += "\n\n👉 <i>Toque no título para se candidatar</i>"
    return msg


# --------------------------------------------------------------------------- #
# Pipeline por código (usado pelo agendador e pelo /run_now)
# --------------------------------------------------------------------------- #
def run_for_code(code):
    p = prefs.get(code)
    if not p:
        return {"jobs": [], "sent": 0, "error": "sem preferências"}
    chat_id = p.get("chat_id")
    jobs = aggregate(p)
    filtered = score_and_filter(jobs, p)

    uniq = {}
    for j in filtered:
        uniq.setdefault(j["uid"], j)
    filtered = list(uniq.values())

    with _lock:
        code_seen = seen.setdefault(code, {})
    novas = [j for j in filtered if j["uid"] not in code_seen]

    threshold = int(p.get("match_threshold", 70))
    approved, sent = [], 0
    now = time.time()
    for j in novas:
        llm_match(j, p)
        code_seen[j["uid"]] = {"title": j["title"], "match": j.get("match_score", 0), "when": now}
        if j.get("match_score", 0) >= threshold:
            approved.append(j)
            if chat_id and tg_send(chat_id, format_html(j)):
                sent += 1
    save_seen()
    approved.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    _last_run[code] = now
    return {"jobs": approved, "sent": sent}


# --------------------------------------------------------------------------- #
# Agendador: verifica a cada TICK quem precisa rodar
# --------------------------------------------------------------------------- #
def scheduler_loop():
    while True:
        try:
            now = time.time()
            for code, p in list(prefs.items()):
                if not p.get("enabled") or not p.get("chat_id"):
                    continue
                interval = int(p.get("interval_minutes", 120)) * 60
                if now - _last_run.get(code, 0) >= interval:
                    run_for_code(code)
        except Exception:
            pass
        time.sleep(TICK_SECONDS)


def start_scheduler():
    global _sched_started
    if _sched_started or os.getenv("DISABLE_SCHEDULER") == "1":
        return
    _sched_started = True
    threading.Thread(target=scheduler_loop, daemon=True).start()


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return jsonify({"ok": True, "codes": len(ACCESS_CODES), "registered": len(prefs)})


@app.post("/prefs/get")
def prefs_get():
    code, err = guard()
    if err:
        return err
    p = {**DEFAULT_PREFS, **prefs.get(code, {})}
    p.pop("llm_api_key", None)        # nunca devolve a chave da IA
    p.pop("anthropic_api_key", None)  # idem para a chave legada
    return jsonify({"prefs": p})


@app.post("/prefs/set")
def prefs_set():
    code, err = guard()
    if err:
        return err
    incoming = (request.get_json(silent=True) or {}).get("prefs", {})
    clean = {k: v for k, v in incoming.items() if k in ALLOWED_PREF_KEYS}
    with _lock:
        prefs[code] = {**DEFAULT_PREFS, **prefs.get(code, {}), **clean}
    save_prefs()
    return jsonify({"ok": True})


@app.post("/run_now")
def run_now():
    code, err = guard()
    if err:
        return err
    # aplica prefs enviadas na hora (ex.: teste antes de salvar), se vierem
    incoming = (request.get_json(silent=True) or {}).get("prefs")
    if incoming:
        clean = {k: v for k, v in incoming.items() if k in ALLOWED_PREF_KEYS}
        with _lock:
            prefs[code] = {**DEFAULT_PREFS, **prefs.get(code, {}), **clean}
        save_prefs()
    result = run_for_code(code)
    return jsonify(result)


@app.post("/telegram/test")
def telegram_test():
    code, err = guard()
    if err:
        return err
    chat_id = (request.get_json(silent=True) or {}).get("chat_id") or prefs.get(code, {}).get("chat_id")
    ok = tg_send(chat_id, "✅ <b>Job Hunter</b> conectado! As vagas vão chegar por aqui.")
    return jsonify({"ok": ok})


@app.post("/telegram/detect")
def telegram_detect():
    code, err = guard()
    if err:
        return err
    token = KEYS["telegram_bot_token"]
    if not token:
        return jsonify({"error": "Bot não configurado no servidor."}), 500
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        r.raise_for_status()
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("edited_message") or {}
            if code.lower() in (msg.get("text", "") or "").lower():
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    return jsonify({"chat_id": chat["id"]})
        return jsonify({"error": f"Não achei mensagem com o código '{code}'. "
                                 f"Mande exatamente esse código pro bot e tente de novo."}), 404
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


start_scheduler()  # inicia junto com o processo (gunicorn/python)

if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
