import re
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

st.set_page_config(page_title="Painel B3", page_icon="📈", layout="wide")

PERIODO = "2y"
JANELAS_VISIVEIS = {"1 mês": 21, "3 meses": 63, "6 meses": 126, "1 ano": 252, "Tudo": None}


# ============ UTILIDADES ============
def limpar_lista(texto):
    codigos = re.split(r"[,;\s]+", texto.upper())
    codigos = [c.removesuffix(".SA") for c in codigos if c]
    return list(dict.fromkeys(codigos))  # remove repetidos, mantendo a ordem


def br(valor, casas=2):
    """Formata número no padrão brasileiro: 1.234,56"""
    return f"{valor:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_variacao(v):
    if pd.isna(v):
