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
        return "Variação: —"
    cor = "green" if v >= 0 else "red"
    sinal = "+" if v >= 0 else ""
    return f'Variação: <span style="color:{cor}">{sinal}{br(v)}%</span>'


# ============ INDICADORES ============
def calcular_ifr(fechamento, periodo=14):
    variacao = fechamento.diff()
    ganhos = variacao.clip(lower=0)
    perdas = -variacao.clip(upper=0)
    media_ganhos = ganhos.ewm(alpha=1/periodo, adjust=False).mean()
    media_perdas = perdas.ewm(alpha=1/periodo, adjust=False).mean()
    return 100 - 100 / (1 + media_ganhos / media_perdas)


@st.cache_data(ttl=3600, show_spinner=False)
def preparar_dados(ticker):
    dados = yf.Ticker(ticker + ".SA").history(period=PERIODO)
    dados = dados.dropna(subset=["Close"])
    if dados.empty:
        raise ValueError("não encontrei dados. Confira o código do ativo.")

    dados[["Open", "High", "Low", "Close"]] = dados[["Open", "High", "Low", "Close"]].round(2)
    dados["Variacao"] = dados["Close"].pct_change() * 100
    dados["MME9"] = dados["Close"].ewm(span=9, adjust=False).mean()
    dados["MME21"] = dados["Close"].ewm(span=21, adjust=False).mean()
    dados["MMA200"] = dados["Close"].rolling(200).mean()
    dados["IFR"] = calcular_ifr(dados["Close"])
    return dados


# ============ SUPORTES E RESISTÊNCIAS ============
def encontrar_pivos(dados, janela):
    tamanho = 2 * janela + 1
    maximas = dados["High"]
    minimas = dados["Low"]
    topos = maximas[maximas == maximas.rolling(tamanho, center=True).max()]
    fundos = minimas[minimas == minimas.rolling(tamanho, center=True).min()]
    return topos.tolist() + fundos.tolist()


def agrupar_niveis(precos, tolerancia):
    if not precos:
        return []
    precos = sorted(precos)
    grupos = [[precos[0]]]
    for p in precos[1:]:
        media = sum(grupos[-1]) / len(grupos[-1])
        if p <= media * (1 + tolerancia):
            grupos[-1].append(p)
        else:
            grupos.append([p])
    return [(sum(g) / len(g), len(g)) for g in grupos]


def suportes_e_resistencias(dados, janela, tolerancia, max_niveis):
    recentes = dados.tail(250)
    niveis = [n for n in agrupar_niveis(encontrar_pivos(recentes, janela), tolerancia) if n[1] >= 2]
    preco = dados["Close"].iloc[-1]
    suportes = sorted([n for n in niveis if n[0] < preco], key=lambda n: preco - n[0])
    resistencias = sorted([n for n in niveis if n[0] > preco], key=lambda n: n[0] - preco)
    return suportes[:max_niveis], resistencias[:max_niveis]


# ============ GRÁFICO ============
def grafico(dados, ticker, janela_visivel, fim, janela_pivo, tolerancia, max_niveis):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=[0.75, 0.25])

    fig.add_trace(go.Candlestick(
        x=dados.index, name=ticker,
        open=dados["Open"], high=dados["High"],
        low=dados["Low"], close=dados["Close"],
        text=[formatar_variacao(v) for v in dados["Variacao"]],
    ), row=1, col=1)
    fig.add_trace(go.Scatter(x=dados.index, y=dados["MME9"], name="MME 9",
                             hovertemplate="%{y:.2f}",
                             line=dict(color="orange", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=dados.index, y=dados["MME21"], name="MME 21",
                             hovertemplate="%{y:.2f}",
                             line=dict(color="royalblue", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=dados.index, y=dados["MMA200"], name="MMA 200",
                             hovertemplate="%{y:.2f}",
                             line=dict(color="purple", width=2)), row=1, col=1)

    suportes, resistencias = suportes_e_resistencias(dados, janela_pivo, tolerancia, max_niveis)
    for nivel, toques in suportes:
        fig.add_hline(y=nivel, line_dash="dash", line_color="green", line_width=1, opacity=0.7,
                      annotation_text=f"S {br(nivel)} ({toques}x)",
                      annotation_position="bottom left", annotation_font_color="green",
                      row=1, col=1)
    for nivel, toques in resistencias:
        fig.add_hline(y=nivel, line_dash="dash", line_color="red", line_width=1, opacity=0.7,
                      annotation_text=f"R {br(nivel)} ({toques}x)",
                      annotation_position="top left", annotation_font_color="red",
                      row=1, col=1)

    fig.add_trace(go.Scatter(x=dados.index, y=dados["IFR"], name="IFR 14",
                             hovertemplate="%{y:.1f}",
                             line=dict(color="teal", width=1.5)), row=2, col=1)
    fig.add_hrect(y0=30, y1=70, fillcolor="gray", opacity=0.1, line_width=0, row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="red", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="green", row=2, col=1)

    # Enquadramento: recorta a janela que termina em "fim" e ajusta o eixo de preços
    recorte = dados.loc[:fim].tail(janela_visivel or len(dados))
    precos = recorte[["Low", "High", "MME9", "MME21", "MMA200"]]
    lo, hi = precos.min().min(), precos.max().max()
    margem = (hi - lo) * 0.05
    inicio = recorte.index[0] - pd.Timedelta(days=1)
    final = recorte.index[-1] + pd.Timedelta(hours=23)

    fig.update_layout(height=700, hovermode="x unified",
                      dragmode="pan",
                      xaxis_rangeslider_visible=False,
                      margin=dict(t=30, b=20))
    fig.update_yaxes(range=[lo - margem, hi + margem], row=1, col=1)
    fig.update_yaxes(range=[0, 100], fixedrange=True, row=2, col=1)
    fig.update_xaxes(range=[inicio, final],
                     rangebreaks=[dict(bounds=["sat", "mon"])],
                     hoverformat="%d/%m/%Y")
    return fig


# ============ INTERFACE ============
st.title("📈 Painel B3")

with st.sidebar:
    st.header("Configurações")
    texto = st.text_input("Ativos (separados por vírgula)", "PETR4, VALE3, ITUB4, BOVA11")
    periodo_visivel = st.radio("Período visível", list(JANELAS_VISIVEIS), index=2)

    st.subheader("Suportes e resistências")
    janela_pivo = st.slider("Janela dos pivôs (dias)", 2, 15, 5)
    tolerancia = st.slider("Tolerância de agrupamento (%)", 0.5, 5.0, 2.0, 0.5) / 100
    max_niveis = st.slider("Níveis de cada lado", 1, 5, 3)

    st.caption("Dados do Yahoo Finance, com atraso. Uso educacional, "
               "não é recomendação de investimento.")

acoes = limpar_lista(texto)
if not acoes:
    st.info("Digite ao menos um ativo na barra lateral para começar.")
    st.stop()

for aba, acao in zip(st.tabs(acoes), acoes):
    with aba:
        try:
            with st.spinner(f"Carregando {acao}..."):
                dados = preparar_dados(acao)
        except Exception as erro:
            st.warning(f"{acao}: {erro}")
            continue

        ultimo = dados.iloc[-1]
        variacao = None
        if not pd.isna(ultimo["Variacao"]):
            variacao = f"{'+' if ultimo['Variacao'] >= 0 else ''}{br(ultimo['Variacao'])}%"

        if pd.isna(ultimo["MMA200"]):
            tendencia = "—"
        elif ultimo["Close"] > ultimo["MMA200"]:
            tendencia = "Acima 📈"
        else:
            tendencia = "Abaixo 📉"

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Fechamento", f"R$ {br(ultimo['Close'])}", variacao)
        c2.metric("IFR 14", br(ultimo["IFR"], 1))
        c3.metric("Preço x MMA 200", tendencia)
        c4.metric("Último pregão", ultimo.name.strftime("%d/%m/%Y"))

        # Navegação no tempo: escolhe o último dia da janela visível
        janela = JANELAS_VISIVEIS[periodo_visivel]
        fim = dados.index[-1]
        if janela and len(dados) > janela:
            opcoes = list(dados.index[janela - 1:])
            chave = f"nav_{acao}"
            if chave not in st.session_state or st.session_state[chave] not in opcoes:
                st.session_state[chave] = opcoes[-1]

            col_slider, col_botao = st.columns([6, 1])
            if col_botao.button("⏭ Hoje", key=f"hoje_{acao}"):
                st.session_state[chave] = opcoes[-1]
            fim = col_slider.select_slider(
                "Navegar no histórico (arraste para voltar no tempo)",
                options=opcoes,
                format_func=lambda d: d.strftime("%d/%m/%Y"),
                key=chave,
            )

        fig = grafico(dados, acao, janela, fim, janela_pivo, tolerancia, max_niveis)
        st.plotly_chart(fig, key=f"grafico_{acao}")
