import datetime
import html
import math
import random
import re
import threading
import time
from collections import deque
from string import Template
from zoneinfo import ZoneInfo

import altair as alt
import numpy as np
import pandas as pd
import requests
import serial
import streamlit as st
from flask import Flask, jsonify, request

st.set_page_config(
    page_title="Monitor Ambiental | ESP32 + DHT22",
    page_icon="🌡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =====================================================
# CONFIGURAÇÕES
# =====================================================
# ⚠️ IMPORTANTE: altere 'COM5' para a porta real do seu ESP32
PORTA = "COM5"
BAUD_RATE = 115200

# Gera dados sintéticos (sem ESP32) para demonstração e testes do painel.
MODO_SIMULACAO = False

# API HTTP (http://localhost:5000/api/dados) disponível junto com o painel.
HABILITAR_API = True
PORTA_API = 5000

MAX_HISTORICO = 20000          # leituras mantidas em memória
LIMITE_SEM_DADOS = 10          # segundos sem leitura para considerar o sensor "sem dados"
VERSAO = "2.0"

# Horário oficial de Brasília (APIs públicas, sem chave)
FUSO_BR = ZoneInfo("America/Sao_Paulo")
APIS_HORARIO = [
    ("https://worldtimeapi.org/api/timezone/America/Sao_Paulo", "datetime"),
    ("https://timeapi.io/api/time/current/zone?timeZone=America/Sao_Paulo", "dateTime"),
]
INTERVALO_SINCRONIZACAO = 600  # segundos entre consultas às APIs de horário

LIMITES_PADRAO = {"temp_min": 15.0, "temp_max": 35.0, "umid_min": 30.0, "umid_max": 70.0}
DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
               "sexta-feira", "sábado", "domingo"]


def num(valor, casas=1, sinal=False):
    """Formata número no padrão brasileiro (vírgula decimal)."""
    texto = f"{valor:+.{casas}f}" if sinal else f"{valor:.{casas}f}"
    return texto.replace(".", ",")


def log(mensagem):
    try:
        print(mensagem, flush=True)
    except Exception:
        pass


# =====================================================
# NÚCLEO: ESTADO COMPARTILHADO + SERVIÇOS EM SEGUNDO PLANO
# O Streamlit reexecuta o script a cada interação (criando um novo espaço
# de variáveis). Por isso todo o estado vive em um único objeto guardado
# com st.cache_resource, compartilhado entre threads e sessões.
# =====================================================
class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.t0 = time.monotonic()
        self.dados = {
            "temperatura": 0.0,
            "umidade": 0.0,
            "status": "Aguardando dados...",
            "atualizado_em": None,
        }
        self.historico = deque(maxlen=MAX_HISTORICO)
        self.eventos = deque(maxlen=300)
        self.limites = dict(LIMITES_PADRAO)
        self.estado_alerta = {"temp": None, "umid": None}
        self.total_leituras = 0
        self.conectado = False
        self.api_status = "Desativada"
        self.sync = {"deslocamento": None, "ultima": None, "fonte": None}

    # ---------- Horário de Brasília ----------
    def _consultar_hora_api(self):
        for url, campo in APIS_HORARIO:
            try:
                antes = datetime.datetime.now(datetime.timezone.utc)
                resp = requests.get(url, timeout=3)
                depois = datetime.datetime.now(datetime.timezone.utc)
                resp.raise_for_status()
                texto = resp.json()[campo]
                # Normaliza a fração de segundos para 6 dígitos (compatível com fromisoformat)
                texto = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), texto)
                hora = datetime.datetime.fromisoformat(texto)
                if hora.tzinfo is None:
                    hora = hora.replace(tzinfo=FUSO_BR)
                referencia = antes + (depois - antes) / 2  # compensa a latência da requisição
                return hora - referencia, url.split("/")[2]
            except (requests.RequestException, KeyError, ValueError):
                continue
        return None, None

    def agora_brasil(self, sincronizar=True):
        """Data/hora atual de Brasília (sem fuso), ajustada por API pública."""
        agora_utc = datetime.datetime.now(datetime.timezone.utc)

        if sincronizar:
            ultima = self.sync["ultima"]
            if ultima is None or time.monotonic() - ultima > INTERVALO_SINCRONIZACAO:
                deslocamento, fonte = self._consultar_hora_api()
                if deslocamento is not None:
                    self.sync.update(deslocamento=deslocamento, ultima=time.monotonic(), fonte=fonte)
                else:
                    # Falhou: tenta novamente em ~60 s, mantendo o último ajuste conhecido
                    self.sync["ultima"] = time.monotonic() - INTERVALO_SINCRONIZACAO + 60

        deslocamento = self.sync["deslocamento"] or datetime.timedelta(0)
        return (agora_utc + deslocamento).astimezone(FUSO_BR).replace(tzinfo=None)

    # ---------- Eventos e alertas ----------
    def _registrar_evento(self, nivel, texto):
        momento = self.agora_brasil(False)
        with self.lock:
            self.eventos.append({"Horário": momento, "Nível": nivel, "Evento": texto})

    def _avaliar_alertas(self, temperatura, umidade):
        lim = self.limites
        verificacoes = [
            ("temp", "Temperatura", temperatura, lim["temp_min"], lim["temp_max"], "°C"),
            ("umid", "Umidade", umidade, lim["umid_min"], lim["umid_max"], "%"),
        ]
        for chave, nome, valor, minimo, maximo, un in verificacoes:
            if valor > maximo:
                novo = "alta"
            elif valor < minimo:
                novo = "baixa"
            else:
                novo = None

            if novo != self.estado_alerta[chave]:
                if novo == "alta":
                    self._registrar_evento(
                        "Crítico", f"{nome} acima do limite: {num(valor)} {un} (máx. {num(maximo)} {un})")
                elif novo == "baixa":
                    self._registrar_evento(
                        "Crítico", f"{nome} abaixo do limite: {num(valor)} {un} (mín. {num(minimo)} {un})")
                else:
                    self._registrar_evento("Normal", f"{nome} normalizada: {num(valor)} {un}")
                self.estado_alerta[chave] = novo

    def atualizar_limites(self, novos):
        with self.lock:
            self.limites.update(novos)

    def limpar(self):
        with self.lock:
            self.historico.clear()
            self.eventos.clear()

    # ---------- Leituras ----------
    def _registrar_leitura(self, temperatura, umidade):
        agora = self.agora_brasil()  # pode consultar a API de horário (fora do lock)
        with self.lock:
            self.dados.update(
                temperatura=temperatura, umidade=umidade, status="OK", atualizado_em=agora)
            self.historico.append({"Horário": agora, "Temperatura": temperatura, "Umidade": umidade})
            self.total_leituras += 1
            self._avaliar_alertas(temperatura, umidade)

    def _loop_leitura(self, esp32):
        while True:
            if esp32.in_waiting > 0:
                linha = esp32.readline().decode("utf-8", errors="ignore").strip()

                if not linha:
                    continue

                if "erro" in linha:
                    with self.lock:
                        self.dados["status"] = "Erro na leitura do sensor DHT"
                    continue

                if "," in linha:
                    try:
                        partes = linha.split(",")
                        temperatura = float(partes[0])
                        umidade = float(partes[1])
                    except (ValueError, IndexError):
                        continue
                    self._registrar_leitura(temperatura, umidade)
                    log(f"[Cabo USB] Temp: {temperatura}C | Umid: {umidade}%")

            time.sleep(0.1)

    def _simular(self):
        with self.lock:
            self.conectado = True
        self._registrar_evento("Info", "Modo de simulação ativo (dados sintéticos)")
        inicio = time.time()
        while True:
            s = time.time() - inicio
            temperatura = 28 + 8 * math.sin(s / 45) + random.uniform(-0.2, 0.2)
            umidade = 55 + 20 * math.sin(s / 70 + 1) + random.uniform(-0.5, 0.5)
            self._registrar_leitura(round(temperatura, 1), round(umidade, 1))
            time.sleep(1)

    def ler_serial(self):
        """Thread em segundo plano: lê o cabo USB e reconecta automaticamente."""
        if MODO_SIMULACAO:
            self._simular()
            return

        falha_registrada = False
        while True:
            esp32 = None
            try:
                esp32 = serial.Serial(PORTA, BAUD_RATE, timeout=1)
                time.sleep(2)  # Aguarda a placa estabilizar
                with self.lock:
                    self.conectado = True
                    self.dados["status"] = "Conectado, aguardando dados..."
                falha_registrada = False
                self._registrar_evento("Info", f"Conexão estabelecida com o ESP32 ({PORTA})")
                log(f"Conectado com sucesso ao ESP32 na porta {PORTA}!")
                self._loop_leitura(esp32)
            except Exception:
                with self.lock:
                    self.conectado = False
                    if esp32 is None:
                        self.dados["status"] = f"Erro: Não foi possível abrir a porta {PORTA}"
                    else:
                        self.dados["status"] = f"Conexão perdida com a porta {PORTA}"
                if not falha_registrada:
                    falha_registrada = True
                    self._registrar_evento(
                        "Crítico",
                        f"Falha de comunicação serial em {PORTA} (nova tentativa a cada 5 s)")
                    log(f"ERRO: nao consegui abrir/ler a porta {PORTA}.")
                    log("-> Verifique se o Monitor Serial da Arduino IDE esta FECHADO.")
                    log("-> Verifique se o numero da porta COM esta correto no codigo.")
            finally:
                if esp32 is not None:
                    try:
                        esp32.close()
                    except Exception:
                        pass
            time.sleep(5)

    # ---------- API HTTP ----------
    def executar_api(self):
        app = Flask("monitor_api")

        @app.route("/api/dados", methods=["GET"])
        def api_dados():
            with self.lock:
                d = dict(self.dados)
            if d["status"] != "OK" and d["temperatura"] == 0.0:
                return jsonify({"erro": d["status"]}), 500
            return jsonify({"temperatura": d["temperatura"], "umidade": d["umidade"]})

        @app.route("/api/historico", methods=["GET"])
        def api_historico():
            limite = request.args.get("limite", default=100, type=int)
            limite = max(1, min(limite, MAX_HISTORICO))
            with self.lock:
                itens = list(self.historico)[-limite:]
            return jsonify([
                {"horario": i["Horário"].isoformat(),
                 "temperatura": i["Temperatura"], "umidade": i["Umidade"]}
                for i in itens
            ])

        @app.route("/api/status", methods=["GET"])
        def api_status():
            with self.lock:
                return jsonify({
                    "conectado": self.conectado,
                    "status": self.dados["status"],
                    "total_leituras": self.total_leituras,
                    "limites": self.limites,
                })

        try:
            with self.lock:
                self.api_status = f"Ativa na porta {PORTA_API}"
            log(f"API disponivel em http://localhost:{PORTA_API}/api/dados")
            app.run(host="0.0.0.0", port=PORTA_API, debug=False, use_reloader=False)
        except OSError as erro:
            with self.lock:
                self.api_status = "Indisponível (porta em uso)"
            self._registrar_evento("Crítico", f"API HTTP não iniciada: {erro}")

    # ---------- Leitura segura para a interface ----------
    def snapshot(self):
        agora = self.agora_brasil(False)
        with self.lock:
            dados = dict(self.dados)
            instantaneo = {
                "dados": dados,
                "historico": list(self.historico),
                "eventos": list(self.eventos),
                "limites": dict(self.limites),
                "estado_alerta": dict(self.estado_alerta),
                "conectado": self.conectado,
                "total": self.total_leituras,
                "sync": dict(self.sync),
                "api_status": self.api_status,
                "uptime": time.monotonic() - self.t0,
                "agora": agora,
            }
        atualizado = dados["atualizado_em"]
        instantaneo["idade"] = (agora - atualizado).total_seconds() if atualizado else None
        return instantaneo


@st.cache_resource(show_spinner=False)
def obter_monitor():
    m = Monitor()
    threading.Thread(target=m.ler_serial, daemon=True, name="serial").start()
    if HABILITAR_API:
        threading.Thread(target=m.executar_api, daemon=True, name="api").start()
    return m


monitor = obter_monitor()

# =====================================================
# TEMAS E PALETAS
# =====================================================
MODOS = {
    "Claro": dict(bg="#F4F7FB", surface="#FFFFFF", border="#DCE5F0", text="#0F172A",
                  muted="#5B6B82", grid="#E5EAF2", shadow="0 1px 3px rgba(15,23,42,0.07)"),
    "Escuro": dict(bg="#0B1220", surface="#111A2E", border="#22304D", text="#E6EDF7",
                   muted="#93A3BD", grid="#1E2A44", shadow="0 1px 3px rgba(0,0,0,0.45)"),
}

# a/s = cores principal/secundária (modo claro); a_d/s_d = versões para o modo escuro
PALETAS = {
    "Azul Corporativo": dict(a="#1D4ED8", a_d="#60A5FA", s="#0EA5E9", s_d="#38BDF8",
                             g=("#0B2545", "#13315C", "#1D4ED8")),
    "Turquesa": dict(a="#0E7490", a_d="#22D3EE", s="#14B8A6", s_d="#5EEAD4",
                     g=("#083344", "#0E4F63", "#0E7490")),
    "Esmeralda": dict(a="#047857", a_d="#34D399", s="#65A30D", s_d="#A3E635",
                      g=("#052E26", "#065F46", "#059669")),
    "Índigo": dict(a="#4338CA", a_d="#818CF8", s="#9333EA", s_d="#C084FC",
                   g=("#1E1B4B", "#312E81", "#4F46E5")),
    "Âmbar": dict(a="#B45309", a_d="#FBBF24", s="#EA580C", s_d="#FB923C",
                  g=("#451A03", "#78350F", "#D97706")),
    "Grafite": dict(a="#334155", a_d="#CBD5E1", s="#94A3B8", s_d="#64748B",
                    g=("#0F172A", "#1E293B", "#475569")),
}


def montar_tema(modo, paleta):
    base = MODOS[modo]
    p = PALETAS[paleta]
    escuro = modo == "Escuro"
    return {
        **base,
        "accent": p["a_d"] if escuro else p["a"],
        "accent2": p["s_d"] if escuro else p["s"],
        "g1": p["g"][0], "g2": p["g"][1], "g3": p["g"][2],
        "on_accent": "#0B1220" if escuro else "#FFFFFF",
        "esquema": "dark" if escuro else "light",
        "danger": "#EF4444", "ok": "#10B981", "warn": "#F59E0B",
    }


tema_nome = st.session_state.get("tema", "Claro")
paleta_nome = st.session_state.get("paleta", "Azul Corporativo")
T = montar_tema(tema_nome, paleta_nome)

CSS = Template("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root { color-scheme: $esquema; }

/* ---------- Base ---------- */
.stApp { background: $bg; color: $text; }
.stApp, [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] *,
.stApp h1, .stApp h2, .stApp h3, .stApp p, .stApp label, .stTabs [data-baseweb="tab"] p {
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
}
.block-container { padding: 1.5rem 2rem 3rem; max-width: 1320px; }
header[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"],
#MainMenu, footer { display: none !important; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp p, .stApp li { color: $text; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: $muted; }
[data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label {
    color: $muted; font-size: 0.8rem; font-weight: 600;
}

/* ---------- Barra lateral ---------- */
[data-testid="stSidebar"] { background: $surface; border-right: 1px solid $border; }
.sb-marca { display: flex; align-items: center; gap: 0.7rem; margin: 0.25rem 0 0.75rem; }
.sb-logo {
    width: 38px; height: 38px; border-radius: 10px; display: flex; align-items: center;
    justify-content: center; background: linear-gradient(135deg, $g1, $g3);
}
.sb-nome { font-weight: 700; font-size: 1rem; color: $text; line-height: 1.15; }
.sb-sub { font-size: 0.72rem; color: $muted; }
.sb-titulo {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
    color: $accent; margin: 1.1rem 0 0.4rem; padding-top: 0.9rem; border-top: 1px solid $border;
}
.sb-amostras { display: flex; gap: 0.4rem; margin: 0.15rem 0 0.25rem; }
.sb-amostras span { width: 26px; height: 8px; border-radius: 4px; display: inline-block; }
.sb-nota { font-size: 0.74rem; color: $muted; line-height: 1.5; }

/* ---------- Campos de formulário ---------- */
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="base-input"] {
    background: $surface !important; border-color: $border !important;
    color: $text !important; border-radius: 8px !important;
}
[data-baseweb="select"] * { color: $text; }
[data-baseweb="select"] svg { fill: $muted; }
[data-baseweb="input"] input, [data-baseweb="base-input"] input {
    color: $text !important; -webkit-text-fill-color: $text !important; background: transparent !important;
}
.stNumberInput button { background: $surface !important; color: $text !important; border-color: $border !important; }
[data-baseweb="popover"] > div, [data-baseweb="popover"] ul, [data-baseweb="menu"] {
    background: $surface !important; border-color: $border !important;
}
[data-baseweb="popover"] li { background: $surface !important; color: $text !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] {
    background: ${accent}22 !important;
}

/* ---------- Abas ---------- */
.stTabs [data-baseweb="tab-list"] { gap: 0.25rem; border-bottom: 1px solid $border; }
.stTabs [data-baseweb="tab"] { background: transparent; padding: 0.6rem 1.1rem; }
.stTabs [data-baseweb="tab"] p { color: $muted; font-weight: 600; font-size: 0.9rem; }
.stTabs [aria-selected="true"] p { color: $accent; }
.stTabs [data-baseweb="tab-highlight"] { background-color: $accent; }
.stTabs [data-baseweb="tab-border"] { background-color: $border; }

/* ---------- Botões ---------- */
.stButton > button, .stDownloadButton > button {
    background: $accent; color: $on_accent; border: none; border-radius: 8px;
    font-weight: 600; padding: 0.45rem 1rem;
}
.stButton > button p, .stDownloadButton > button p { color: $on_accent !important; }
.stButton > button:hover, .stDownloadButton > button:hover { background: $accent; filter: brightness(1.12); }

/* ---------- Cabeçalho ---------- */
.banner {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    background: linear-gradient(135deg, $g1 0%, $g2 55%, $g3 100%);
    border-radius: 14px; padding: 1.5rem 2rem; margin-bottom: 1.25rem;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18);
}
.banner-eyebrow { color: rgba(255,255,255,0.65); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.14em; }
.banner-titulo { color: #FFFFFF; font-size: 1.65rem; font-weight: 700; letter-spacing: -0.01em; margin: 0.15rem 0; }
.banner-sub { color: rgba(255,255,255,0.78); font-size: 0.88rem; }
.banner-dir { text-align: right; }
.banner-relogio { color: #FFFFFF; font-size: 1.9rem; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.1; margin-top: 0.5rem; }
.banner-data { color: rgba(255,255,255,0.75); font-size: 0.8rem; }
.badge { display: inline-block; padding: 0.35rem 0.85rem; border-radius: 999px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em; white-space: nowrap; }
.badge-ok { background: #D1FAE5; color: #047857; }
.badge-aviso { background: #FEF3C7; color: #B45309; }
.badge-erro { background: #FEE2E2; color: #B91C1C; }

/* ---------- Cartões ---------- */
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem; margin-bottom: 1rem; }
.card {
    background: $surface; border: 1px solid $border; border-left: 4px solid $accent;
    border-radius: 12px; padding: 0.95rem 1.2rem; box-shadow: $shadow;
}
.card-rotulo { font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: $muted; }
.card-valor { font-size: 1.9rem; font-weight: 700; color: $text; line-height: 1.25; font-variant-numeric: tabular-nums; }
.card-detalhe { font-size: 0.8rem; color: $muted; }
.card-nota { font-size: 0.72rem; color: $muted; margin-top: 0.25rem; opacity: 0.85; }

/* ---------- Seções, avisos e tabelas ---------- */
.secao { font-size: 0.78rem; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: $accent; margin: 1.25rem 0 0.6rem; }
.alerta {
    background: ${danger}16; border: 1px solid ${danger}55; border-left: 4px solid $danger;
    color: $text; border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 1rem; font-size: 0.9rem;
}
.alerta ul { margin: 0.35rem 0 0 1.1rem; padding: 0; }
.alerta-ok { background: ${ok}14; border-color: ${ok}55; border-left-color: $ok; }
.alerta-info { background: ${accent}12; border-color: ${accent}44; border-left-color: $accent; }
.tabela-wrap { background: $surface; border: 1px solid $border; border-radius: 12px; overflow: auto; max-height: 460px; box-shadow: $shadow; }
.tabela { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.tabela th {
    position: sticky; top: 0; background: $bg; color: $muted; text-align: left; font-size: 0.7rem;
    text-transform: uppercase; letter-spacing: 0.06em; padding: 0.65rem 1rem; border-bottom: 1px solid $border;
}
.tabela td { padding: 0.55rem 1rem; color: $text; border-bottom: 1px solid $border; font-variant-numeric: tabular-nums; }
.tabela tbody tr:last-child td { border-bottom: none; }
.tabela tbody tr:hover td { background: ${accent}12; }
.tag { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.7rem; font-weight: 700; }
.tag-critico { background: ${danger}22; color: $danger; }
.tag-normal { background: ${ok}22; color: $ok; }
.tag-info { background: ${accent}22; color: $accent; }
.kv { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 2rem; background: $surface;
      border: 1px solid $border; border-radius: 12px; padding: 0.5rem 1.25rem; box-shadow: $shadow; }
.kv-item { display: flex; justify-content: space-between; gap: 1rem; padding: 0.7rem 0; border-bottom: 1px solid $border; font-size: 0.88rem; }
.kv-item span:first-child { color: $muted; }
.kv-item span:last-child { color: $text; font-weight: 600; text-align: right; }
.rodape { text-align: center; color: $muted; font-size: 0.74rem; margin-top: 2.5rem; }

/* ---------- Gráficos ---------- */
[data-testid="stVegaLiteChart"], [data-testid="stArrowVegaLiteChart"] {
    background: $surface; border: 1px solid $border; border-radius: 12px; padding: 0.75rem; box-shadow: $shadow;
}

@media (max-width: 900px) {
    .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .kv { grid-template-columns: 1fr; }
    .banner { flex-direction: column; align-items: flex-start; }
    .banner-dir { text-align: left; }
}
</style>
""")
st.markdown(CSS.safe_substitute(T), unsafe_allow_html=True)


# =====================================================
# FUNÇÕES AUXILIARES
# =====================================================
def compactar(texto):
    """Remove quebras de linha/indentação (evita que o Markdown interprete o HTML como código)."""
    return "".join(linha.strip() for linha in texto.splitlines())


def secao(texto):
    st.markdown(f'<div class="secao">{texto}</div>', unsafe_allow_html=True)


def aviso(texto, tipo="info"):
    st.markdown(f'<div class="alerta alerta-{tipo}">{texto}</div>', unsafe_allow_html=True)


def card(rotulo, valor, detalhe="", nota="", cor=None):
    estilo = f' style="border-left-color:{cor}"' if cor else ""
    nota_html = f'<div class="card-nota">{nota}</div>' if nota else ""
    return (f'<div class="card"{estilo}><div class="card-rotulo">{rotulo}</div>'
            f'<div class="card-valor">{valor}</div><div class="card-detalhe">{detalhe}</div>'
            f"{nota_html}</div>")


def grade_cards(*cartoes):
    st.markdown(f'<div class="cards">{"".join(cartoes)}</div>', unsafe_allow_html=True)


def variacao(atual, anterior, unidade):
    delta = atual - anterior
    seta = "▲" if delta > 0.049 else ("▼" if delta < -0.049 else "●")
    return f"{seta} {num(delta, 1, True)} {unidade} vs. leitura anterior"


def duracao(segundos):
    s = int(segundos)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}min"
    if h:
        return f"{h}h {m}min"
    if m:
        return f"{m}min {s}s"
    return f"{s}s"


def formatar_dt(momento):
    return momento.strftime("%d/%m/%Y %H:%M:%S")


def ponto_orvalho(temperatura, umidade):
    """Ponto de orvalho (fórmula de Magnus)."""
    umidade = max(umidade, 0.1)
    gama = math.log(umidade / 100) + 17.62 * temperatura / (243.12 + temperatura)
    return 243.12 * gama / (17.62 - gama)


def sensacao_termica(temperatura, umidade):
    """Índice de calor (NWS). Abaixo de ~27 °C a sensação é considerada igual à temperatura."""
    tf = temperatura * 9 / 5 + 32
    if tf < 80:
        return temperatura
    hi = (-42.379 + 2.04901523 * tf + 10.14333127 * umidade - 0.22475541 * tf * umidade
          - 0.00683783 * tf ** 2 - 0.05481717 * umidade ** 2 + 0.00122874 * tf ** 2 * umidade
          + 0.00085282 * tf * umidade ** 2 - 0.00000199 * tf ** 2 * umidade ** 2)
    if umidade < 13 and tf <= 112:
        hi -= ((13 - umidade) / 4) * math.sqrt((17 - abs(tf - 95)) / 17)
    elif umidade > 85 and tf <= 87:
        hi += ((umidade - 85) / 10) * ((87 - tf) / 5)
    return (hi - 32) * 5 / 9


def para_df(historico):
    if not historico:
        return pd.DataFrame({
            "Horário": pd.Series(dtype="datetime64[ns]"),
            "Temperatura": pd.Series(dtype=float),
            "Umidade": pd.Series(dtype=float),
        })
    return pd.DataFrame(historico)


def reduzir(df, maximo=1500):
    if len(df) <= maximo:
        return df
    return df.iloc[::math.ceil(len(df) / maximo)]


def tendencia(df, coluna, minutos=5):
    """Inclinação (unidade por minuto) por regressão linear nos últimos minutos."""
    recente = df[df["Horário"] >= df["Horário"].iloc[-1] - pd.Timedelta(minutes=minutos)]
    if len(recente) < 5:
        return None
    x = (recente["Horário"] - recente["Horário"].iloc[0]).dt.total_seconds().to_numpy()
    if x[-1] <= 0:
        return None
    return float(np.polyfit(x, recente[coluna].to_numpy(), 1)[0] * 60)


def texto_tendencia(valor, unidade):
    if valor is None:
        return "—"
    if abs(valor) < 0.05:
        return "● estável"
    seta = "▲" if valor > 0 else "▼"
    return f"{seta} {num(valor, 2, True)} {unidade}/min"


def estado_sensor(s):
    idade = s["idade"]
    if not s["conectado"]:
        return "Offline", s["dados"]["status"], "erro"
    if idade is None:
        return "Aguardando", "Sem leituras ainda", "aviso"
    if idade > LIMITE_SEM_DADOS:
        return "Sem dados", f"Última leitura há {duracao(idade)}", "aviso"
    if s["dados"]["status"] != "OK":
        return "Instável", s["dados"]["status"], "aviso"
    return "Online", "Recebendo leituras", "ok"


def tabela_html(df, colunas_html=()):
    cabecalho_html = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    linhas = []
    for _, linha in df.iterrows():
        celulas = []
        for coluna, valor in linha.items():
            conteudo = valor if coluna in colunas_html else html.escape(str(valor))
            celulas.append(f"<td>{conteudo}</td>")
        linhas.append("<tr>" + "".join(celulas) + "</tr>")
    corpo = "".join(linhas)
    return (f'<div class="tabela-wrap"><table class="tabela"><thead><tr>{cabecalho_html}</tr></thead>'
            f"<tbody>{corpo}</tbody></table></div>")


# ---------- Gráficos (Altair, estilizados pelo tema ativo) ----------
def estilizar(grafico):
    return (grafico
            .configure_view(strokeWidth=0)
            .configure_axis(labelColor=T["muted"], titleColor=T["muted"], gridColor=T["grid"],
                            domainColor=T["border"], tickColor=T["border"],
                            labelFontSize=11, titleFontSize=11, titleFontWeight=600)
            .configure_title(color=T["text"], fontSize=13, anchor="start", fontWeight=600)
            .configure(background=T["surface"]))


def mostrar_grafico(grafico):
    try:
        st.altair_chart(grafico, width="stretch", theme=None)
    except TypeError:  # versões antigas do Streamlit (sem o parâmetro width)
        st.altair_chart(grafico, use_container_width=True, theme=None)


def grafico_serie(df, coluna, titulo, unidade, cor, minimo=None, maximo=None, altura=300):
    d = reduzir(df)[["Horário", coluna]]
    baixo, alto = d[coluna].min(), d[coluna].max()
    if minimo is not None:
        baixo = min(baixo, minimo)
    if maximo is not None:
        alto = max(alto, maximo)
    folga = max((alto - baixo) * 0.12, 0.5)
    escala = alt.Scale(domain=[float(baixo - folga), float(alto + folga)], nice=False)

    x = alt.X("Horário:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False, labelOverlap=True))
    y = alt.Y(f"{coluna}:Q", title=unidade, scale=escala)
    dica = [alt.Tooltip("Horário:T", title="Horário", format="%d/%m/%Y %H:%M:%S"),
            alt.Tooltip(f"{coluna}:Q", title=titulo, format=".1f")]

    degrade = alt.Gradient(
        gradient="linear",
        stops=[alt.GradientStop(color=T["surface"], offset=0), alt.GradientStop(color=cor, offset=1)],
        x1=1, x2=1, y1=1, y2=0)
    base = alt.Chart(d).encode(x=x, y=y)
    camadas = [
        base.mark_area(line={"color": cor, "strokeWidth": 2}, color=degrade, opacity=0.35),
        base.mark_point(size=60, opacity=0, filled=True).encode(tooltip=dica),
    ]
    for limite in (minimo, maximo):
        if limite is not None:
            camadas.append(
                alt.Chart(pd.DataFrame({"v": [limite]}))
                .mark_rule(color=T["danger"], strokeDash=[6, 4], strokeWidth=1.2)
                .encode(y=alt.Y("v:Q", scale=escala, axis=None)))
    titulo_grafico = f"{titulo} ({unidade})"
    return estilizar(alt.layer(*camadas).properties(height=altura, title=titulo_grafico))


def grafico_combinado(df):
    d = reduzir(df)
    x = alt.X("Horário:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False, labelOverlap=True))
    dica_t = [alt.Tooltip("Horário:T", format="%d/%m/%Y %H:%M:%S"), alt.Tooltip("Temperatura:Q", format=".1f")]
    dica_u = [alt.Tooltip("Horário:T", format="%d/%m/%Y %H:%M:%S"), alt.Tooltip("Umidade:Q", format=".1f")]
    linha_t = alt.Chart(d).mark_line(color=T["accent"], strokeWidth=2).encode(
        x=x, tooltip=dica_t,
        y=alt.Y("Temperatura:Q", title="Temperatura (°C)", scale=alt.Scale(zero=False),
                axis=alt.Axis(titleColor=T["accent"])))
    linha_u = alt.Chart(d).mark_line(color=T["accent2"], strokeWidth=2).encode(
        x=x, tooltip=dica_u,
        y=alt.Y("Umidade:Q", title="Umidade (%)", scale=alt.Scale(zero=False),
                axis=alt.Axis(orient="right", grid=False, titleColor=T["accent2"])))
    combinado = alt.layer(linha_t, linha_u).resolve_scale(y="independent")
    return estilizar(combinado.properties(height=320, title="Temperatura × Umidade"))


def grafico_histograma(df, coluna, titulo, cor):
    d = reduzir(df, 3000)[[coluna]]
    grafico = alt.Chart(d).mark_bar(color=cor, cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        x=alt.X(f"{coluna}:Q", bin=alt.Bin(maxbins=20), title=None),
        y=alt.Y("count()", title="Frequência"),
        tooltip=[alt.Tooltip("count()", title="Leituras")])
    return estilizar(grafico.properties(height=240, title=titulo))


def grafico_dispersao(df):
    d = reduzir(df)
    grafico = alt.Chart(d).mark_circle(size=40, opacity=0.55, color=T["accent"]).encode(
        x=alt.X("Temperatura:Q", scale=alt.Scale(zero=False), title="Temperatura (°C)"),
        y=alt.Y("Umidade:Q", scale=alt.Scale(zero=False), title="Umidade (%)"),
        tooltip=[alt.Tooltip("Horário:T", format="%d/%m/%Y %H:%M:%S"),
                 alt.Tooltip("Temperatura:Q", format=".1f"), alt.Tooltip("Umidade:Q", format=".1f")])
    return estilizar(grafico.properties(height=240, title="Temperatura × Umidade (dispersão)"))


# =====================================================
# BARRA LATERAL
# =====================================================
PERIODOS = {
    "Últimos 5 minutos": 5,
    "Últimos 15 minutos": 15,
    "Última hora": 60,
    "Últimas 6 horas": 360,
    "Todo o histórico": None,
}

ICONE = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#FFFFFF" stroke-width="2" '
         'stroke-linecap="round" stroke-linejoin="round"><path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/></svg>')

with st.sidebar:
    st.markdown(compactar(f"""
        <div class="sb-marca">
            <div class="sb-logo">{ICONE}</div>
            <div><div class="sb-nome">Monitor Ambiental</div><div class="sb-sub">ESP32 · DHT22</div></div>
        </div>"""), unsafe_allow_html=True)

    st.markdown('<div class="sb-titulo" style="border-top:none;padding-top:0">Aparência</div>',
                unsafe_allow_html=True)
    st.selectbox("Tema", list(MODOS), key="tema")
    st.selectbox("Paleta de cores", list(PALETAS), key="paleta")
    st.markdown(compactar(f"""
        <div class="sb-amostras">
            <span style="background:{T['g1']}"></span><span style="background:{T['accent']}"></span>
            <span style="background:{T['accent2']}"></span>
        </div>"""), unsafe_allow_html=True)

    st.markdown('<div class="sb-titulo">Monitoramento</div>', unsafe_allow_html=True)
    modo_atualizacao = st.selectbox("Atualização", ["Automática", "Pausada"], key="modo")
    intervalo = st.selectbox("Intervalo", [1, 2, 3, 5, 10, 30], index=1, key="intervalo",
                             format_func=lambda x: f"{x} s")
    periodo_nome = st.selectbox("Período exibido", list(PERIODOS), index=1, key="periodo")

    st.markdown('<div class="sb-titulo">Ações</div>', unsafe_allow_html=True)
    if st.button("Limpar histórico"):
        monitor.limpar()

    st.markdown('<div class="sb-titulo">Informações</div>', unsafe_allow_html=True)
    api_texto = (f"API: http://localhost:{PORTA_API}/api/dados" if HABILITAR_API else "API HTTP desativada")
    origem = "Simulação de dados" if MODO_SIMULACAO else f"Porta serial {PORTA}"
    st.markdown(compactar(f"""
        <div class="sb-nota">{origem}<br>{api_texto}<br>Horário de Brasília</div>"""),
                unsafe_allow_html=True)

pausado = modo_atualizacao == "Pausada"
auto = st.fragment(run_every=None if pausado else intervalo)
auto_lento = st.fragment(run_every=None if pausado else max(intervalo, 5))


def filtrar_periodo(df, agora):
    minutos = PERIODOS[periodo_nome]
    if minutos is None or df.empty:
        return df
    return df[df["Horário"] >= agora - pd.Timedelta(minutes=minutos)]


# =====================================================
# CABEÇALHO
# =====================================================
@auto
def cabecalho():
    s = monitor.snapshot()
    rotulo, _, classe = estado_sensor(s)
    agora = s["agora"]
    origem_txt = "Modo de simulação" if MODO_SIMULACAO else f"Porta {PORTA}"
    st.markdown(compactar(f"""
        <div class="banner">
            <div>
                <div class="banner-eyebrow">MONITORAMENTO EM TEMPO REAL</div>
                <div class="banner-titulo">Temperatura e Umidade</div>
                <div class="banner-sub">ESP32 + DHT22 &nbsp;·&nbsp; {origem_txt} &nbsp;·&nbsp; Horário de Brasília</div>
            </div>
            <div class="banner-dir">
                <span class="badge badge-{classe}">● SENSOR {rotulo.upper()}</span>
                <div class="banner-relogio">{agora:%H:%M:%S}</div>
                <div class="banner-data">{DIAS_SEMANA[agora.weekday()]}, {agora:%d/%m/%Y}</div>
            </div>
        </div>"""), unsafe_allow_html=True)


# =====================================================
# PÁGINA: VISÃO GERAL
# =====================================================
def bloco_alertas(s):
    d = s["dados"]
    if d["atualizado_em"] is None:
        return ""
    lim = s["limites"]
    ativos = []
    for chave, nome, valor, un in (("temp", "Temperatura", d["temperatura"], "°C"),
                                   ("umid", "Umidade", d["umidade"], "%")):
        estado = s["estado_alerta"][chave]
        if estado == "alta":
            ativos.append(f"{nome} acima do limite máximo ({num(lim[chave + '_max'])} {un}) — "
                          f"leitura atual: {num(valor)} {un}")
        elif estado == "baixa":
            ativos.append(f"{nome} abaixo do limite mínimo ({num(lim[chave + '_min'])} {un}) — "
                          f"leitura atual: {num(valor)} {un}")
    if ativos:
        itens = "".join(f"<li>{a}</li>" for a in ativos)
        return f'<div class="alerta"><strong>Alertas ativos</strong><ul>{itens}</ul></div>'
    return '<div class="alerta alerta-ok">Todos os parâmetros estão dentro dos limites configurados.</div>'


@auto
def pagina_visao_geral():
    s = monitor.snapshot()
    df_total = para_df(s["historico"])

    if df_total.empty:
        aviso("Aguardando as primeiras leituras do ESP32...")
        return

    st.markdown(bloco_alertas(s), unsafe_allow_html=True)

    d = s["dados"]
    t, u = d["temperatura"], d["umidade"]
    anterior = df_total.iloc[-2] if len(df_total) > 1 else df_total.iloc[-1]
    lim = s["limites"]
    estado, detalhe_estado, classe = estado_sensor(s)
    cor_estado = {"ok": T["ok"], "aviso": T["warn"], "erro": T["danger"]}[classe]
    ultimo_minuto = df_total[df_total["Horário"] >= s["agora"] - pd.Timedelta(seconds=60)]
    idade = s["idade"]

    grade_cards(
        card("Temperatura", f"{num(t)} °C", variacao(t, anterior["Temperatura"], "°C"),
             f"Limites: {num(lim['temp_min'])} a {num(lim['temp_max'])} °C", T["accent"]),
        card("Umidade relativa", f"{num(u)} %", variacao(u, anterior["Umidade"], "%"),
             f"Limites: {num(lim['umid_min'])} a {num(lim['umid_max'])} %", T["accent2"]),
        card("Ponto de orvalho", f"{num(ponto_orvalho(t, u))} °C", "Temperatura de condensação",
             "Fórmula de Magnus", T["g3"]),
        card("Sensação térmica", f"{num(sensacao_termica(t, u))} °C", "Índice de calor",
             "Aplicável a partir de ~27 °C", T["muted"]),
    )
    grade_cards(
        card("Estado do sensor", estado, detalhe_estado, cor=cor_estado),
        card("Taxa de amostragem", f"{len(ultimo_minuto)}/min", "Leituras no último minuto", cor=T["accent"]),
        card("Última leitura", f"há {duracao(idade)}" if idade is not None else "—",
             formatar_dt(d["atualizado_em"]) if d["atualizado_em"] else "", cor=T["accent2"]),
        card("Tempo de operação", duracao(s["uptime"]), f"{s['total']} leituras recebidas", cor=T["muted"]),
    )

    df = filtrar_periodo(df_total, s["agora"])
    if df.empty:
        aviso(f"Sem leituras no período selecionado ({periodo_nome.lower()}).", "info")
        return

    secao(f"Evolução · {periodo_nome.lower()}")
    c1, c2 = st.columns(2)
    with c1:
        mostrar_grafico(grafico_serie(df, "Temperatura", "Temperatura", "°C", T["accent"],
                                      lim["temp_min"], lim["temp_max"]))
    with c2:
        mostrar_grafico(grafico_serie(df, "Umidade", "Umidade", "%", T["accent2"],
                                      lim["umid_min"], lim["umid_max"]))
    st.caption("Linhas tracejadas indicam os limites de alerta configurados.")


# =====================================================
# PÁGINA: ANÁLISES
# =====================================================
def tabela_estatisticas(df):
    linhas = []
    for nome, coluna, un in (("Temperatura", "Temperatura", "°C"), ("Umidade", "Umidade", "%")):
        x = df[coluna]
        linhas.append({
            "Variável": nome,
            "Mínimo": f"{num(x.min())} {un}",
            "Média": f"{num(x.mean())} {un}",
            "Mediana": f"{num(x.median())} {un}",
            "Máximo": f"{num(x.max())} {un}",
            "Desvio padrão": f"{num(x.std(ddof=0), 2)} {un}",
            "Amplitude": f"{num(x.max() - x.min())} {un}",
            "Tendência (5 min)": texto_tendencia(tendencia(df, coluna), un),
        })
    return pd.DataFrame(linhas)


@auto
def pagina_analises():
    s = monitor.snapshot()
    df = filtrar_periodo(para_df(s["historico"]), s["agora"])

    if len(df) < 2:
        aviso("Dados insuficientes no período selecionado para gerar as análises.")
        return

    correlacao = df["Temperatura"].corr(df["Umidade"])
    correlacao_txt = "n/d" if pd.isna(correlacao) else num(correlacao, 2)
    cobertura = (df["Horário"].iloc[-1] - df["Horário"].iloc[0]).total_seconds()

    grade_cards(
        card("Amostras no período", f"{len(df)}", periodo_nome, cor=T["accent"]),
        card("Janela coberta", duracao(cobertura), f"{df['Horário'].iloc[0]:%H:%M:%S} a {df['Horário'].iloc[-1]:%H:%M:%S}",
             cor=T["accent2"]),
        card("Correlação T × U", correlacao_txt, "Coeficiente de Pearson (−1 a 1)", cor=T["g3"]),
        card("Amplitude térmica", f"{num(df['Temperatura'].max() - df['Temperatura'].min())} °C",
             "Máximo − mínimo no período", cor=T["muted"]),
    )

    secao("Resumo estatístico")
    st.markdown(tabela_html(tabela_estatisticas(df)), unsafe_allow_html=True)

    secao("Evolução conjunta")
    mostrar_grafico(grafico_combinado(df))

    secao("Distribuição e correlação")
    c1, c2, c3 = st.columns(3)
    with c1:
        mostrar_grafico(grafico_histograma(df, "Temperatura", "Distribuição da temperatura", T["accent"]))
    with c2:
        mostrar_grafico(grafico_histograma(df, "Umidade", "Distribuição da umidade", T["accent2"]))
    with c3:
        mostrar_grafico(grafico_dispersao(df))


# =====================================================
# PÁGINA: ALERTAS
# =====================================================
def restaurar_limites():
    for chave, valor in LIMITES_PADRAO.items():
        st.session_state[f"lim_{chave}"] = valor


def configurar_limites():
    secao("Limites de operação")
    atuais = monitor.snapshot()["limites"]
    for chave, valor in atuais.items():
        st.session_state.setdefault(f"lim_{chave}", float(valor))

    c1, c2 = st.columns(2)
    with c1:
        tmin = st.number_input("Temperatura mínima (°C)", step=0.5, key="lim_temp_min")
        tmax = st.number_input("Temperatura máxima (°C)", step=0.5, key="lim_temp_max")
    with c2:
        umin = st.number_input("Umidade mínima (%)", step=1.0, key="lim_umid_min")
        umax = st.number_input("Umidade máxima (%)", step=1.0, key="lim_umid_max")

    if tmin < tmax and umin < umax:
        monitor.atualizar_limites({"temp_min": tmin, "temp_max": tmax, "umid_min": umin, "umid_max": umax})
    else:
        st.warning("O valor mínimo deve ser menor que o máximo. Os limites anteriores continuam em vigor.")
    st.button("Restaurar limites padrão", on_click=restaurar_limites)


@auto
def painel_eventos():
    s = monitor.snapshot()
    eventos = s["eventos"]
    ativos = sum(1 for v in s["estado_alerta"].values() if v)
    criticos = sum(1 for e in eventos if e["Nível"] == "Crítico")

    grade_cards(
        card("Alertas ativos", f"{ativos}", "Parâmetros fora dos limites agora",
             cor=T["danger"] if ativos else T["ok"]),
        card("Eventos críticos", f"{criticos}", "No registro atual", cor=T["accent"]),
        card("Eventos registrados", f"{len(eventos)}", "Máximo de 300 eventos", cor=T["accent2"]),
        card("Último evento", eventos[-1]["Horário"].strftime("%H:%M:%S") if eventos else "—",
             eventos[-1]["Nível"] if eventos else "Nenhum evento", cor=T["muted"]),
    )

    secao("Registro de eventos")
    if not eventos:
        aviso("Nenhum evento registrado até o momento.")
        return

    classes = {"Crítico": "critico", "Normal": "normal", "Info": "info"}
    linhas = [{
        "Horário": formatar_dt(e["Horário"]),
        "Nível": f'<span class="tag tag-{classes.get(e["Nível"], "info")}">{e["Nível"]}</span>',
        "Evento": e["Evento"],
    } for e in reversed(eventos[-150:])]
    st.markdown(tabela_html(pd.DataFrame(linhas), colunas_html=("Nível",)), unsafe_allow_html=True)


# =====================================================
# PÁGINA: DADOS
# =====================================================
@auto_lento
def pagina_dados():
    s = monitor.snapshot()
    df = para_df(s["historico"])

    if df.empty:
        aviso("Ainda não há leituras registradas.")
        return

    exportacao = df.rename(columns={"Temperatura": "Temperatura (°C)", "Umidade": "Umidade (%)"})
    carimbo = s["agora"].strftime("%Y%m%d_%H%M%S")

    secao("Exportação")
    c1, c2, _ = st.columns([1, 1, 3])
    with c1:
        st.download_button("Exportar CSV", data=exportacao.to_csv(index=False).encode("utf-8"),
                           file_name=f"leituras_{carimbo}.csv", mime="text/csv", key="dl_csv")
    with c2:
        st.download_button("Exportar JSON",
                           data=exportacao.to_json(orient="records", date_format="iso", force_ascii=False),
                           file_name=f"leituras_{carimbo}.json", mime="application/json", key="dl_json")

    secao(f"Últimas {min(len(df), 200)} leituras (de {len(df)} em memória)")
    recentes = df.tail(200).iloc[::-1]
    tabela = pd.DataFrame({
        "Horário": recentes["Horário"].dt.strftime("%d/%m/%Y %H:%M:%S"),
        "Temperatura": recentes["Temperatura"].map(lambda v: f"{num(v)} °C"),
        "Umidade": recentes["Umidade"].map(lambda v: f"{num(v)} %"),
    })
    st.markdown(tabela_html(tabela), unsafe_allow_html=True)


# =====================================================
# PÁGINA: SISTEMA
# =====================================================
@auto_lento
def pagina_sistema():
    import sys

    s = monitor.snapshot()
    sync = s["sync"]
    if sync["deslocamento"] is not None:
        fonte_hora = f"API pública ({sync['fonte']})"
        ultima = f"há {duracao(time.monotonic() - sync['ultima'])}" if sync["ultima"] else "—"
        desvio = f"{sync['deslocamento'].total_seconds() * 1000:+.0f} ms".replace(".", ",")
    else:
        fonte_hora = "Relógio do sistema (America/Sao_Paulo)"
        ultima, desvio = "sem sincronização", "—"

    origem = "Simulação de dados" if MODO_SIMULACAO else f"{PORTA} @ {BAUD_RATE} baud"
    itens = [
        ("Origem dos dados", origem),
        ("Conexão com o sensor", "Conectado" if s["conectado"] else "Desconectado"),
        ("Último status", s["dados"]["status"]),
        ("Leituras recebidas", str(s["total"])),
        ("Leituras em memória", f"{len(s['historico'])} / {MAX_HISTORICO}"),
        ("API HTTP", s["api_status"]),
        ("Endpoints", "/api/dados · /api/historico · /api/status"),
        ("Fonte do horário", fonte_hora),
        ("Última sincronização", ultima),
        ("Desvio do relógio local", desvio),
        ("Tempo de operação", duracao(s["uptime"])),
        ("Versão do painel", VERSAO),
        ("Streamlit", st.__version__),
        ("Python", sys.version.split()[0]),
    ]
    secao("Diagnóstico do sistema")
    corpo = "".join(
        f'<div class="kv-item"><span>{html.escape(r)}</span><span>{html.escape(v)}</span></div>'
        for r, v in itens)
    st.markdown(f'<div class="kv">{corpo}</div>', unsafe_allow_html=True)


# =====================================================
# MONTAGEM DA PÁGINA
# =====================================================
cabecalho()

aba_geral, aba_analises, aba_alertas, aba_dados, aba_sistema = st.tabs(
    ["Visão geral", "Análises", "Alertas", "Dados", "Sistema"])

with aba_geral:
    pagina_visao_geral()
with aba_analises:
    pagina_analises()
with aba_alertas:
    configurar_limites()
    painel_eventos()
with aba_dados:
    pagina_dados()
with aba_sistema:
    pagina_sistema()

st.markdown(
    f'<div class="rodape">Monitor Ambiental v{VERSAO} · ESP32 + DHT22 · Horário de Brasília (America/Sao_Paulo)</div>',
    unsafe_allow_html=True)