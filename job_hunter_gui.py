#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Job Hunter — CONFIGURADOR (app web local)
=========================================

Este app NÃO precisa ficar aberto. Ele só salva as suas preferências no
BACKEND, que roda a varredura de vagas sozinho, 24/7, e te avisa no Telegram
mesmo com o seu PC desligado.

Como funciona: `python job_hunter_gui.py` sobe um servidor local (somente
127.0.0.1), abre a interface no seu navegador e faz proxy das chamadas pro
backend (assim o navegador não esbarra em CORS). A config local (código de
acesso, chave da IA, cache das escolhas) fica em job_hunter_config.json,
como antes. As chaves de API de verdade ficam no backend.

Instalação:
  pip install requests
Rodar:
  python job_hunter_gui.py
"""

import json
import os
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# =========================================================================== #
# O DEV publica o backend.py e coloca a URL dele aqui (ou via JOBHUNTER_BACKEND)
# =========================================================================== #
BACKEND_URL = (os.getenv("JOBHUNTER_BACKEND", "").strip()
               or "https://jobhunter.147.15.26.90.sslip.io").rstrip("/")

CONFIG_FILE = "job_hunter_config.json"
HOST = "127.0.0.1"
PORT_PREFERIDA = 8787

# Espelho local das preferências (o servidor é a fonte da verdade).
DEFAULT = {
    "access_code": "",
    "llm_provider": "anthropic",
    "llm_base_url": "https://api.anthropic.com",
    "llm_api_key": "",
    "anthropic_api_key": "",   # legado; migrado para llm_api_key ao abrir
    "chat_id": "",
    "keywords": "desenvolvedor java",
    "location": "Curitiba, PR",
    "country": "br",
    "levels": ["junior"],
    "work_types": ["remoto", "presencial"],
    "sources": ["adzuna", "jooble", "google_jobs"],
    "match_threshold": 70,
    "use_llm": True,
    "llm_model": "claude-haiku-4-5-20251001",
    "resume": (
        "Desenvolvedor backend Java com 1+ ano em produção. Stack: Java, Spring Boot, "
        "APIs RESTful, microserviços, MySQL, Redis, Docker, Kafka, JWT. Otimizou gargalo "
        "N+1 reduzindo latência de pior caso em 77%. Projetos: MatchCV (LLMs via API, "
        "prompt engineering, LaTeX, Cassandra) e PokeTracker (Spring Boot + React + MySQL). "
        "Cursando Engenharia de Software (dez/2027). Busca vaga de nível júnior."
    ),
    "interval_minutes": 120,
    "enabled": False,
}

# Campos que compõem as PREFERÊNCIAS enviadas ao backend
PREF_KEYS = ["chat_id", "keywords", "location", "country", "levels", "work_types",
             "sources", "match_threshold", "use_llm", "llm_provider", "llm_base_url",
             "llm_model", "resume", "interval_minutes", "enabled", "llm_api_key"]

# Endpoints do backend que o front pode chamar via /api/<endpoint>
API_ALLOW = {"prefs/get", "prefs/set", "run_now", "telegram/detect", "telegram/test"}


def load_local():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = {**DEFAULT, **json.load(f)}
    except Exception:
        return dict(DEFAULT)
    # Compatibilidade: migra a chave antiga (só Anthropic) para a chave única.
    if not cfg.get("llm_api_key") and cfg.get("anthropic_api_key"):
        cfg["llm_api_key"] = cfg["anthropic_api_key"]
    return cfg


def save_local(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def api_post(path, payload, timeout=60):
    """POST para o backend. Retorna (data, erro_str)."""
    try:
        r = requests.post(f"{BACKEND_URL}{path}", json=payload, timeout=timeout)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return None, data.get("error", f"HTTP {r.status_code}")
        return data, None
    except requests.RequestException as e:
        return None, f"Falha de conexão com o backend: {e}"


# =========================================================================== #
# Interface — design "Job Hunter Interface Redesign" (Claude Design) com a
# lógica real no lugar da demo. Autossuficiente: sem framework, só vanilla JS.
# =========================================================================== #
GUI_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Hunter — configurador</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
html,body{margin:0;padding:0;background:#0A0A0C;}
*{box-sizing:border-box;}
:root{
  --bg:#0A0A0C; --surface:#141417; --field:#1C1C21; --hover:#232329;
  --border:#2A2A30; --border2:#1E1E23; --text:#F5F5F7; --muted:#9A9AA2;
  --faint:#5C5C66; --red:#DF1F2D; --red-h:#E93340; --red-txt:#FF4D55;
  --good:#4ADE80; --warn:#FACC15;
}
body{color:var(--text);font-family:Inter,sans-serif;}
input::placeholder,textarea::placeholder{color:var(--faint);}
input[type=number]::-webkit-inner-spin-button{opacity:0.4;}
::selection{background:rgba(223,31,45,0.35);}
.app{min-height:100vh;display:flex;flex-direction:column;}
header{display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:60px;border-bottom:1px solid var(--border2);flex:none;}
.logo{width:30px;height:30px;border-radius:8px;background:var(--red);display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(223,31,45,0.35);}
.pill{display:flex;align-items:center;gap:8px;padding:5px 12px;border:1px solid var(--border);border-radius:99px;background:var(--surface);}
.dot{width:7px;height:7px;border-radius:99px;flex:none;background:var(--faint);}
.dot.on{background:var(--good);box-shadow:0 0 6px rgba(74,222,128,0.6);}
nav{display:flex;gap:26px;padding:0 32px;border-bottom:1px solid var(--border2);flex:none;}
.tab{appearance:none;background:none;border:none;cursor:pointer;padding:13px 2px;font:500 13.5px Inter,sans-serif;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .12s;outline:none;color:var(--muted);display:flex;align-items:center;gap:7px;}
.tab.active{color:var(--text);border-bottom-color:var(--red);}
main{flex:1;overflow-y:auto;}
.wrap{max-width:820px;margin:0 auto;padding:32px 32px 56px;display:flex;flex-direction:column;gap:32px;}
h2{margin:0;font:600 11px Inter,sans-serif;letter-spacing:0.09em;text-transform:uppercase;color:var(--red-txt);display:flex;align-items:center;gap:8px;}
section{display:flex;flex-direction:column;gap:16px;}
label{font:500 12.5px Inter,sans-serif;color:var(--muted);}
.fld{display:flex;flex-direction:column;gap:6px;}
input[type=text],input[type=password],input[type=number],textarea{background:var(--field);border:1px solid var(--border);border-radius:8px;padding:10px 12px;color:var(--text);font:400 13.5px Inter,sans-serif;outline:none;width:100%;transition:border-color .12s, box-shadow .12s;}
.mono, textarea{font-family:'JetBrains Mono',monospace !important;font-size:13px !important;}
input:focus,textarea:focus{border-color:var(--red);box-shadow:0 0 0 3px rgba(223,31,45,0.25);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
.grid3{display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:16px;}
.gridBM{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:4px;}
.card .ttl{font:600 12px Inter,sans-serif;color:var(--text);margin-bottom:6px;}
.chk{display:flex;align-items:center;gap:10px;cursor:pointer;padding:5px 0;user-select:none;}
.box{width:17px;height:17px;border-radius:5px;display:flex;align-items:center;justify-content:center;flex:none;transition:all .12s;background:var(--field);border:1px solid #3A3A42;}
.box.on{background:var(--red);border-color:var(--red);}
.box svg{opacity:0;}
.box.on svg{opacity:1;}
.chk span{font:400 13px Inter,sans-serif;color:var(--muted);}
.chk .on-lbl{color:var(--text);}
.rowcard{display:flex;align-items:center;gap:28px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px;}
.vsep{width:1px;height:24px;background:var(--border);}
.switchrow{display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none;}
.track{width:36px;height:20px;border-radius:99px;background:var(--border);position:relative;flex:none;transition:background .15s;}
.track.on{background:var(--red);}
.knob{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:99px;background:#FFF;transition:left .15s;box-shadow:0 1px 3px rgba(0,0,0,0.4);}
.track.on .knob{left:18px;}
#serverCard{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;border-radius:10px;cursor:pointer;user-select:none;transition:border-color .15s, background .15s;background:var(--surface);border:1px solid var(--border);}
#serverCard.on{background:rgba(223,31,45,0.07);border-color:rgba(223,31,45,0.45);}
#serverLabel{font:600 10.5px 'JetBrains Mono',monospace;letter-spacing:0.1em;color:var(--faint);}
#serverCard.on #serverLabel{color:var(--red-txt);}
.btn{display:flex;align-items:center;gap:8px;background:var(--field);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 16px;font:500 13.5px Inter,sans-serif;cursor:pointer;transition:background .12s;}
.btn:hover{background:var(--hover);}
.btn:focus{border-color:var(--red);box-shadow:0 0 0 3px rgba(223,31,45,0.25);outline:none;}
.btn:disabled{opacity:0.5;cursor:default;}
.btn.primary{background:var(--red);color:#FFF;border:none;font-weight:600;padding:10px 18px;}
.btn.primary:hover{background:var(--red-h);}
.btn.primary:focus{box-shadow:0 0 0 3px rgba(223,31,45,0.35);outline:none;}
.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.chip{appearance:none;cursor:pointer;border-radius:8px;padding:7px 14px;font:500 12.5px Inter,sans-serif;transition:all .12s;outline:none;background:var(--field);border:1px solid var(--border);color:var(--muted);}
.chip.on{background:rgba(223,31,45,0.12);border-color:var(--red);color:var(--red-txt);}
#log{background:#0D0D10;border:1px solid var(--border);border-radius:10px;padding:6px 0;min-height:180px;max-height:320px;overflow-y:auto;}
.logline{display:flex;align-items:baseline;gap:12px;padding:6px 16px;font:400 12.5px 'JetBrains Mono',monospace;border-bottom:1px solid #17171B;}
.logline .t{color:var(--faint);flex:none;}
.logline .pct{font-weight:500;flex:none;min-width:36px;}
.logline .src{color:var(--faint);flex:none;margin-left:auto;}
.logline a{color:inherit;text-decoration:none;}
.logline a:hover{text-decoration:underline;}
.log-empty{padding:56px 20px;text-align:center;font:400 12.5px 'JetBrains Mono',monospace;color:var(--faint);}
.status{font:400 12.5px 'JetBrains Mono',monospace;color:var(--good);}
.status.err{color:var(--red-txt);}
.keywrap{position:relative;display:flex;align-items:center;}
.keywrap input{padding-right:44px;}
.eye{position:absolute;right:6px;background:none;border:none;color:var(--muted);cursor:pointer;padding:6px;display:flex;border-radius:6px;}
.eye:hover{color:var(--text);}
.hint{font:400 11.5px Inter,sans-serif;color:var(--faint);}
@media (max-width:720px){.grid2,.grid3,.gridBM{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="app">

  <header>
    <div style="display:flex;align-items:center;gap:12px;">
      <div class="logo">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#FFF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
      </div>
      <div style="display:flex;flex-direction:column;gap:1px;">
        <span style="font:600 14.5px Inter,sans-serif;letter-spacing:-0.01em;">Job Hunter</span>
        <span style="font:400 11px Inter,sans-serif;color:var(--muted);">Configurador de busca automática</span>
      </div>
    </div>
    <div class="pill">
      <div class="dot" id="statusDot"></div>
      <span style="font:500 12px Inter,sans-serif;color:var(--muted);" id="statusText">Servidor pausado</span>
    </div>
  </header>

  <nav>
    <button class="tab active" id="tabPrefs">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="21" y1="6" x2="10" y2="6"/><line x1="6" y1="6" x2="3" y2="6"/><line x1="21" y1="12" x2="16" y2="12"/><line x1="12" y1="12" x2="3" y2="12"/><line x1="21" y1="18" x2="14" y2="18"/><line x1="10" y1="18" x2="3" y2="18"/><circle cx="8" cy="6" r="2"/><circle cx="14" cy="12" r="2"/><circle cx="12" cy="18" r="2"/></svg>
      Preferências
    </button>
    <button class="tab" id="tabConta">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      Conta
    </button>
  </nav>

  <!-- ============ ABA 1 : PREFERÊNCIAS ============ -->
  <main id="pagePrefs">
  <div class="wrap">

    <section>
      <h2>Busca</h2>
      <div class="fld">
        <label for="jh-cargo">Cargo ou palavras-chave</label>
        <input id="jh-cargo" type="text">
      </div>
      <div class="grid2">
        <div class="fld"><label for="jh-local">Local</label><input id="jh-local" type="text"></div>
        <div class="fld"><label for="jh-pais">País (código)</label><input id="jh-pais" type="text" placeholder="br"></div>
      </div>
    </section>

    <section>
      <h2>Filtros</h2>
      <div class="grid3">
        <div class="card"><span class="ttl">Nível</span><div id="grpLevels"></div></div>
        <div class="card"><span class="ttl">Tipo de trabalho</span><div id="grpTypes"></div></div>
        <div class="card"><span class="ttl">Fontes</span><div id="grpSources"></div></div>
      </div>
    </section>

    <section>
      <h2>Match e frequência</h2>
      <div class="rowcard">
        <div style="display:flex;align-items:center;gap:10px;">
          <label for="jh-match" style="color:var(--text);font-weight:400;font-size:13px;">Notificar com match ≥</label>
          <input id="jh-match" type="number" min="0" max="100" style="width:64px;">
          <span style="font:400 13px Inter,sans-serif;color:var(--muted);">%</span>
        </div>
        <div class="vsep"></div>
        <div style="display:flex;align-items:center;gap:10px;">
          <label for="jh-interval" style="color:var(--text);font-weight:400;font-size:13px;">Rodar a cada</label>
          <input id="jh-interval" type="number" min="15" step="15" style="width:74px;">
          <span style="font:400 13px Inter,sans-serif;color:var(--muted);">min</span>
        </div>
        <div class="vsep"></div>
        <div class="switchrow" id="aiSwitch">
          <div class="track" id="aiTrack"><div class="knob"></div></div>
          <span style="font:400 13px Inter,sans-serif;">Usar IA no match</span>
        </div>
      </div>

      <div id="serverCard">
        <div style="display:flex;align-items:center;gap:14px;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#FF4D55" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
          <div style="display:flex;flex-direction:column;gap:2px;">
            <span style="font:600 13.5px Inter,sans-serif;">Ativar varredura no servidor</span>
            <span style="font:400 12px Inter,sans-serif;color:var(--muted);">Roda 24/7 mesmo com o app fechado — matches chegam pelo Telegram</span>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:14px;">
          <span id="serverLabel">INATIVO</span>
          <div class="track" id="serverTrack"><div class="knob"></div></div>
        </div>
      </div>
    </section>

    <section class="actions">
      <button class="btn primary" id="btnSavePrefs">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
        <span id="btnSavePrefsLbl">Salvar preferências</span>
      </button>
      <button class="btn" id="btnRun">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <span id="btnRunLbl">Buscar agora (testar)</span>
      </button>
      <button class="btn" id="btnReload">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><polyline points="21 3 21 8 16 8"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><polyline points="3 21 3 16 8 16"/></svg>
        Recarregar do servidor
      </button>
    </section>

    <section>
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <h2>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
          Log de resultados
        </h2>
        <span style="font:400 11.5px 'JetBrains Mono',monospace;color:var(--faint);" id="logCount"></span>
      </div>
      <div id="log"><div class="log-empty">— nenhum resultado ainda · rode uma busca —</div></div>
    </section>

  </div>
  </main>

  <!-- ============ ABA 2 : CONTA ============ -->
  <main id="pageConta" style="display:none;">
  <div class="wrap">

    <section>
      <h2>Acesso</h2>
      <div class="grid2">
        <div class="fld"><label for="jh-codigo">Código de acesso</label><input id="jh-codigo" type="password" class="mono"></div>
        <div class="fld"><label for="jh-chatid">Telegram Chat ID</label><input id="jh-chatid" type="text" class="mono" placeholder="ex.: 812394027"></div>
      </div>
      <div class="actions">
        <button class="btn" id="btnDetect">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
          Detectar meu Chat ID
        </button>
        <button class="btn" id="btnTestTg">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></svg>
          Testar Telegram
        </button>
        <span class="status" id="tgStatus"></span>
      </div>
      <span class="hint">(mande seu código de acesso como mensagem pro bot no Telegram, depois clique em detectar)</span>
    </section>

    <section>
      <h2>IA do match</h2>
      <div class="card" style="padding:20px;gap:16px;">
        <div style="display:flex;flex-direction:column;gap:8px;">
          <span style="font:500 12.5px Inter,sans-serif;color:var(--muted);">Provedor</span>
          <div style="display:flex;gap:8px;flex-wrap:wrap;" id="providers"></div>
        </div>
        <div class="gridBM">
          <div class="fld"><label for="jh-baseurl">Base URL</label><input id="jh-baseurl" type="text" class="mono" placeholder="https://api…"></div>
          <div class="fld"><label for="jh-modelo">Modelo</label><input id="jh-modelo" type="text" class="mono" placeholder="nome do modelo"></div>
        </div>
        <div class="fld">
          <label for="jh-apikey">Chave de API</label>
          <div class="keywrap">
            <input id="jh-apikey" type="password" class="mono" placeholder="opcional — sem chave usa o fallback do servidor ou o heurístico">
            <button class="eye" id="btnEye" aria-label="Mostrar chave">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>
          </div>
          <span class="hint">Ollama local não precisa de chave. A chave fica só no seu computador e no servidor — nunca volta pra interface.</span>
        </div>
      </div>
    </section>

    <section>
      <h2>Currículo</h2>
      <div class="fld">
        <label for="jh-cv">Texto do currículo <span style="color:var(--faint);">— usado pela IA para calcular o match</span></label>
        <textarea id="jh-cv" rows="9" spellcheck="false" style="line-height:1.65;resize:vertical;min-height:160px;"></textarea>
      </div>
    </section>

    <section class="actions" style="gap:14px;">
      <button class="btn primary" id="btnSaveConta">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m16 16-4-4-4 4"/></svg>
        <span id="btnSaveContaLbl">Salvar no servidor</span>
      </button>
      <span class="status" id="contaStatus"></span>
    </section>

  </div>
  </main>

</div>

<script>
"use strict";
/* ------------------------------------------------------------------ estado */
const LABELS = {
  levels:  { estagio:"Estágio", junior:"Júnior", pleno:"Pleno", senior:"Sênior" },
  types:   { remoto:"Remoto", presencial:"Presencial", hibrido:"Híbrido" },
  sources: { adzuna:"Adzuna", jooble:"Jooble", google_jobs:"LinkedIn + Indeed", internacional:"Internacional" },
};
/* provider -> [llm_provider do backend, base URL SEM /v1 (o backend completa), modelo] */
const PROVIDERS = {
  "Anthropic":    ["anthropic",     "https://api.anthropic.com",   "claude-haiku-4-5-20251001"],
  "OpenAI":       ["openai_compat", "https://api.openai.com",      "gpt-4o-mini"],
  "DeepSeek":     ["openai_compat", "https://api.deepseek.com",    "deepseek-chat"],
  "Groq":         ["openai_compat", "https://api.groq.com/openai", "llama-3.3-70b-versatile"],
  "Ollama local": ["openai_compat", "http://localhost:11434",      "llama3.1"],
  "Outro":        ["openai_compat", "",                            ""],
};
const PREF_KEYS = ["chat_id","keywords","location","country","levels","work_types",
                   "sources","match_threshold","use_llm","llm_provider","llm_base_url",
                   "llm_model","resume","interval_minutes","enabled","llm_api_key"];

let S = {};                 // config local espelhada (fonte: /local/config)
let running = false;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ------------------------------------------------------------------- fetch */
async function post(url, body) {
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                              body: JSON.stringify(body || {})});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

/* -------------------------------------------------------------- render UI */
function providerLabelFor(cfg) {
  const base = (cfg.llm_base_url || "").replace(/\/+$/, "");
  for (const [label, [prov, purl]] of Object.entries(PROVIDERS))
    if (prov === cfg.llm_provider && purl.replace(/\/+$/, "") === base) return label;
  return cfg.llm_provider === "anthropic" ? "Anthropic" : "Outro";
}

function renderChecks(elId, mapKey, stateKey) {
  const el = $(elId), labels = LABELS[mapKey], sel = new Set(S[stateKey] || []);
  el.innerHTML = Object.entries(labels).map(([k, lbl]) => `
    <div class="chk" data-k="${k}">
      <div class="box ${sel.has(k) ? "on" : ""}">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#FFF" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <span class="${sel.has(k) ? "on-lbl" : ""}">${lbl}</span>
    </div>`).join("");
  el.onclick = e => {
    const row = e.target.closest(".chk"); if (!row) return;
    const k = row.dataset.k, cur = new Set(S[stateKey] || []);
    cur.has(k) ? cur.delete(k) : cur.add(k);
    S[stateKey] = [...cur];
    renderChecks(elId, mapKey, stateKey);
  };
}

function renderProviders() {
  const active = providerLabelFor(S), el = $("providers");
  el.innerHTML = Object.keys(PROVIDERS).map(label =>
    `<button class="chip ${label === active ? "on" : ""}" data-p="${label}">${label}</button>`).join("");
  el.onclick = e => {
    const b = e.target.closest(".chip"); if (!b) return;
    const [prov, url, model] = PROVIDERS[b.dataset.p];
    S.llm_provider = prov; S.llm_base_url = url;
    $("jh-baseurl").value = url;
    if (model) { S.llm_model = model; $("jh-modelo").value = model; }
    renderProviders();
  };
}

function renderToggles() {
  $("aiTrack").classList.toggle("on", !!S.use_llm);
  $("serverTrack").classList.toggle("on", !!S.enabled);
  $("serverCard").classList.toggle("on", !!S.enabled);
  $("serverLabel").textContent = S.enabled ? "ATIVO" : "INATIVO";
  $("statusDot").classList.toggle("on", !!S.enabled);
  $("statusText").textContent = S.enabled ? "Servidor ativo · 24/7" : "Servidor pausado";
}

function fillInputs() {
  $("jh-cargo").value   = S.keywords || "";
  $("jh-local").value   = S.location || "";
  $("jh-pais").value    = S.country || "br";
  $("jh-match").value   = S.match_threshold ?? 70;
  $("jh-interval").value= S.interval_minutes ?? 120;
  $("jh-codigo").value  = S.access_code || "";
  $("jh-chatid").value  = S.chat_id || "";
  $("jh-baseurl").value = S.llm_base_url || "";
  $("jh-modelo").value  = S.llm_model || "";
  $("jh-apikey").value  = S.llm_api_key || "";
  $("jh-cv").value      = S.resume || "";
}

function renderAll() {
  fillInputs();
  renderChecks("grpLevels", "levels", "levels");
  renderChecks("grpTypes", "types", "work_types");
  renderChecks("grpSources", "sources", "sources");
  renderProviders();
  renderToggles();
}

/* ------------------------------------------------------------------- log */
const LOG = [];
function ts() { return new Date().toTimeString().slice(0, 8); }
function logLine(line) {
  LOG.push({t: ts(), ...line});
  const el = $("log");
  el.innerHTML = LOG.map(l => {
    const pctColor = l.pct >= 80 ? "var(--good)" : l.pct >= 70 ? "var(--warn)" : "var(--red-txt)";
    const msgColor = l.kind === "done" ? "var(--good)" : l.kind === "err" ? "var(--red-txt)"
                   : l.kind === "job" ? "var(--text)" : "var(--muted)";
    const msg = l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.msg)}</a>` : esc(l.msg);
    return `<div class="logline">
      <span class="t">${l.t}</span>
      ${l.pct != null ? `<span class="pct" style="color:${pctColor}">${l.pct}%</span>` : ""}
      <span style="color:${msgColor};min-width:0;overflow-wrap:anywhere;">${msg}</span>
      ${l.src ? `<span class="src">${esc(l.src)}</span>` : ""}
    </div>`;
  }).join("");
  el.scrollTop = el.scrollHeight;
  $("logCount").textContent = LOG.length + " linhas";
}

/* ----------------------------------------------------------------- ações */
function collect() {
  S.keywords = $("jh-cargo").value.trim();
  S.location = $("jh-local").value.trim();
  S.country = ($("jh-pais").value.trim() || "br").toLowerCase();
  S.match_threshold = parseInt($("jh-match").value, 10) || 70;
  S.interval_minutes = parseInt($("jh-interval").value, 10) || 120;
  S.access_code = $("jh-codigo").value.trim();
  S.chat_id = $("jh-chatid").value.trim();
  S.llm_base_url = $("jh-baseurl").value.trim();
  S.llm_model = $("jh-modelo").value.trim();
  S.llm_api_key = $("jh-apikey").value.trim();
  S.resume = $("jh-cv").value.trim();
  const prefs = {};
  for (const k of PREF_KEYS) prefs[k] = S[k];
  return prefs;
}

function needCode() {
  if (!S.access_code) {
    switchTab("conta");
    flash("contaStatus", "Preencha seu código de acesso primeiro.", true);
    $("jh-codigo").focus();
    return false;
  }
  return true;
}

function flash(id, msg, isErr) {
  const el = $(id);
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.textContent = ""; }, 4000);
}

async function saveLocal() { await post("/local/save", S).catch(() => {}); }

async function saveAll(btnLblId, statusId) {
  const prefs = collect();
  if (!needCode()) return;
  await saveLocal();
  const lbl = $(btnLblId), orig = "Salvar" + (btnLblId === "btnSavePrefsLbl" ? " preferências" : " no servidor");
  lbl.textContent = "Salvando…";
  try {
    await post("/api/prefs/set", {code: S.access_code, prefs});
    lbl.textContent = "Salvo ✓";
    if (statusId) flash(statusId, "✓ configurações salvas no servidor");
    renderToggles();
  } catch (e) {
    lbl.textContent = orig;
    if (statusId) flash(statusId, "✗ " + e.message, true);
    else logLine({kind: "err", msg: "não salvou: " + e.message});
    return;
  }
  setTimeout(() => { lbl.textContent = orig; }, 1600);
}

async function runNow() {
  if (running) return;
  const prefs = collect();
  if (!needCode()) return;
  running = true;
  $("btnRun").disabled = true;
  $("btnRunLbl").textContent = "Buscando…";
  await saveLocal();
  logLine({kind: "info", msg: `iniciando varredura · "${prefs.keywords}" · ${prefs.location}`});
  try {
    const data = await post("/api/run_now", {code: S.access_code, prefs});
    const jobs = data.jobs || [];
    for (const j of jobs) {
      const extra = [j.location, j.level, j.work_type].filter(x => x && x !== "indefinido").join(" · ");
      logLine({kind: "job", pct: j.match_score ?? 0,
               msg: `${j.title} — ${j.company}${extra ? " · " + extra : ""}`,
               src: j.source || "", url: j.url || ""});
      if (j.match_reason) logLine({kind: "info", msg: "   " + j.match_reason});
    }
    if (!jobs.length) logLine({kind: "info", msg: "nenhuma vaga nova bateu o corte agora (ou já foram enviadas antes)"});
    logLine({kind: "done", msg: `${data.sent || 0} match(es) enviados por Telegram ✓`});
  } catch (e) {
    logLine({kind: "err", msg: "erro: " + e.message});
  }
  running = false;
  $("btnRun").disabled = false;
  $("btnRunLbl").textContent = "Buscar agora (testar)";
}

async function reloadPrefs() {
  collect();
  if (!needCode()) return;
  try {
    const data = await post("/api/prefs/get", {code: S.access_code});
    const server = data.prefs || {};
    delete server.llm_api_key;        // a chave nunca volta do servidor;
    delete server.anthropic_api_key;  // preservamos a local
    Object.assign(S, server);
    renderAll();
    await saveLocal();
    logLine({kind: "info", msg: "preferências recarregadas do servidor"});
  } catch (e) {
    logLine({kind: "err", msg: "não recarregou: " + e.message});
  }
}

async function detectChat() {
  collect();
  if (!needCode()) return;
  try {
    const data = await post("/api/telegram/detect", {code: S.access_code});
    S.chat_id = String(data.chat_id || "");
    $("jh-chatid").value = S.chat_id;
    flash("tgStatus", "✓ Chat ID detectado: " + S.chat_id);
    await saveLocal();
  } catch (e) { flash("tgStatus", "✗ " + e.message, true); }
}

async function testTelegram() {
  collect();
  if (!needCode()) return;
  try {
    const data = await post("/api/telegram/test", {code: S.access_code, chat_id: S.chat_id});
    if (data.ok) flash("tgStatus", "✓ mensagem de teste enviada");
    else flash("tgStatus", "✗ não enviou — confira o Chat ID", true);
  } catch (e) { flash("tgStatus", "✗ " + e.message, true); }
}

/* ------------------------------------------------------------------- tabs */
function switchTab(t) {
  $("pagePrefs").style.display = t === "prefs" ? "" : "none";
  $("pageConta").style.display = t === "conta" ? "" : "none";
  $("tabPrefs").classList.toggle("active", t === "prefs");
  $("tabConta").classList.toggle("active", t === "conta");
}

/* ------------------------------------------------------------------- boot */
$("tabPrefs").onclick = () => switchTab("prefs");
$("tabConta").onclick = () => switchTab("conta");
$("aiSwitch").onclick = () => { S.use_llm = !S.use_llm; renderToggles(); };
$("serverCard").onclick = () => { S.enabled = !S.enabled; renderToggles(); };
$("btnSavePrefs").onclick = () => saveAll("btnSavePrefsLbl", null);
$("btnSaveConta").onclick = () => saveAll("btnSaveContaLbl", "contaStatus");
$("btnRun").onclick = runNow;
$("btnReload").onclick = reloadPrefs;
$("btnDetect").onclick = detectChat;
$("btnTestTg").onclick = testTelegram;
$("btnEye").onclick = () => {
  const k = $("jh-apikey");
  k.type = k.type === "password" ? "text" : "password";
};

(async () => {
  try { S = await (await fetch("/local/config")).json(); }
  catch { S = {}; }
  renderAll();
  if (S.access_code) reloadPrefs();
})();
</script>
</body>
</html>
"""


# =========================================================================== #
# Servidor local: serve a interface e faz proxy pro backend (evita CORS).
# Escuta SÓ em 127.0.0.1 — nada é exposto pra rede.
# =========================================================================== #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, GUI_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/local/config":
            self._send(200, load_local())
        else:
            self._send(404, {"error": "não encontrado"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "JSON inválido"})
            return

        if self.path == "/local/save":
            cfg = {**load_local(), **{k: v for k, v in body.items() if k in DEFAULT}}
            save_local(cfg)
            self._send(200, {"ok": True})
        elif self.path.startswith("/api/"):
            endpoint = self.path[len("/api/"):]
            if endpoint not in API_ALLOW:
                self._send(404, {"error": "endpoint não permitido"})
                return
            timeout = 180 if endpoint == "run_now" else 60
            data, err = api_post(f"/{endpoint}", body, timeout=timeout)
            if err:
                self._send(502, {"error": err})
            else:
                self._send(200, data)
        else:
            self._send(404, {"error": "não encontrado"})

    def log_message(self, *args):  # silencia o log de acesso no terminal
        pass


def pick_port():
    for port in range(PORT_PREFERIDA, PORT_PREFERIDA + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
    return 0  # deixa o SO escolher


def main():
    port = pick_port()
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"Job Hunter — interface aberta em {url}  (Ctrl+C para sair)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
