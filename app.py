import os
import time
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import dados

INTERVALO = 20   # segundos entre cada verificação de novos dados no Neon

st.set_page_config(page_title="Casa de Festas Ulabum — Painel Comercial do Buffet",
                   page_icon="🎉", layout="wide", initial_sidebar_state="collapsed")

# O Streamlit só serve de "moldura": tira margens/cabeçalho e deixa o fundo na cor do painel.
st.markdown("""
<style>
.stApp { background:#FAF8F2; }
#MainMenu, footer, header[data-testid="stHeader"], [data-testid="stToolbar"] { display:none !important; }
.block-container { padding:0 !important; max-width:100% !important; }
iframe { border:0; display:block; }
</style>
""", unsafe_allow_html=True)


def secret(chave):
    try:
        return st.secrets[chave]
    except Exception:
        return os.environ.get(chave)


# Senha opcional (defina APP_PASSWORD nos secrets para proteger o painel)
senha = secret("APP_PASSWORD")
if senha and not st.session_state.get("ok"):
    st.markdown("### 🎉 Painel Comercial do Buffet — Casa de Festas Ulabum")
    digitada = st.text_input("Senha de acesso", type="password")
    if digitada:
        if digitada == senha:
            st.session_state["ok"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    st.stop()


@st.cache_data(ttl=15, show_spinner=False)
def versao(url):
    """Impressão digital leve do banco. Se a consulta falhar, recarrega os dados 1x por minuto."""
    if not url:
        return "csv"
    try:
        return dados.assinatura(url)
    except Exception:
        return f"t{int(time.time() // 60)}"


@st.cache_data(ttl=3600, max_entries=3, show_spinner=False)
def carregar_payload(url, assinatura):
    """Leitura pesada: só roda quando a assinatura do banco muda (ou a cada 1h)."""
    df, origem = dados.carregar(url)
    return dados.payload_json(df), origem


@st.fragment(run_every=INTERVALO)
def painel():
    url = secret("DATABASE_URL")
    try:
        payload, origem = carregar_payload(url, versao(url))
        st.session_state["ultimo"] = (payload, origem)
    except Exception as e:
        if "ultimo" not in st.session_state:
            st.error(f"Não foi possível carregar os dados: {e}")
            return
        payload, origem = st.session_state["ultimo"]   # falha momentânea: mantém o último painel válido

    if origem == "csv":
        st.warning("Modo local: lendo os CSVs da pasta 'dados/'. Configure DATABASE_URL para usar o Neon.")

    modelo = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    # o painel ajusta a própria altura; 2800 é só o valor inicial.
    # Se os dados não mudaram, o HTML é idêntico e o navegador NÃO recarrega o painel.
    components.html(modelo.replace("__DADOS_JSON__", payload), height=2800, scrolling=True)


painel()
