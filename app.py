import re
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

st.set_page_config(page_title="Painel B3", page_icon="📈", layout="wide")

PERIODO = "2y"
JANELAS_VISIVEIS = {"1 mês": 21, "3 meses": 63, "6 meses": 126, "1 ano": 252, "Tudo": None}

# Universo da Sentinela: ações do Ibovespa (aproximação da carteira set-dez/2026) + ETFs.
# A carteira muda a cada 4 meses (jan, mai, set): confira em b3.com.br e edite quando precisar.
IBOVESPA = """
ALOS3 ABEV3 ASAI3 AURE3 AXIA3 AXIA6 AZZA3 B3SA3 BBAS3 BBDC3 BBDC4 BBSE3 BEEF3
BPAC11 BRAP4 BRAV3 CMIG4 CMIN3 COGN3 CPFE3 CPLE3 CPLE6 CSAN3 CSMG3 CSNA3 CURY3
CXSE3 CYRE3 DIRR3 EGIE3 EMBJ3 ENEV3 ENGI11 EQTL3 FLRY3 GGBR4 GOAU4 HAPV3 HYPE3
IGTI11 IRBR3 ISAE4 ITSA4 ITUB4 KLBN11 LREN3 MBRF3 MGLU3 MOTV3 MRVE3 MULT3 NATU3
PCAR3 PETR3 PETR4 POMO4 PRIO3 PSSA3 RADL3 RAIL3 RAIZ4 RDOR3 RENT3 SANB11 SBSP3
SMFT3 SUZB3 TAEE11 TEND3 TIMS3 TOTS3 UGPA3 USIM5 VALE3 VAMO3 VBBR3 VIVA3 VIVT3
WEGE3 YDUQ3
"""
ETFS = "BOVA11 SMAL11 IVVB11 DIVO11 NASD11 GOLD11 HASH11 FIND11"
UNIVERSO_PADRAO = IBOVESPA.split() + ETFS.split()


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


def adicionar_indicadores(dados):
    dados = dados.dropna(subset=["Close"]).copy()
    dados[["Open", "High", "Low", "Close"]] = dados[["Open", "High", "Low", "Close"]].round(2)
    dados["Variacao"] = dados["Close"].pct_change() * 100
    dados["MME9"] = dados["Close"].ewm(span=9, adjust=False).mean()
    dados["MME21"] = dados["Close"].ewm(span=21, adjust=False).mean()
    dados["MMA200"] = dados["Close"].rolling(200).mean()
    dados["IFR"] = calcular_ifr(dados["Close"])
    dados["VolMedia20"] = dados["Volume"].rolling(20).mean()
    return dados


@st.cache_data(ttl=3600, show_spinner=False)
def preparar_dados(ticker):
    dados = yf.Ticker(ticker + ".SA").history(period=PERIODO)
    dados = adicionar_indicadores(dados)
    if dados.empty:
        raise ValueError("não encontrei dados. Confira o código do ativo.")
    return dados


@st.cache_data(ttl=3600, show_spinner=False)
def baixar_universo(tickers):
    """Baixa todos os ativos de uma vez só (bem mais rápido que um por um)."""
    simbolos = [t + ".SA" for t in tickers]
    bruto = yf.download(simbolos, period=PERIODO, group_by="ticker",
                        auto_adjust=True, threads=True, progress=False)
    resultado = {}
    for t, sim in zip(tickers, simbolos):
        try:
            df = bruto[sim] if isinstance(bruto.columns, pd.MultiIndex) else bruto
            df = adicionar_indicadores(df)
        except Exception:
            continue
        if len(df) >= 30:
            resultado[t] = df

    # Descarta ativos sem pregões recentes (código antigo ou fora de negociação)
    if resultado:
        ultima = max(df.index[-1] for df in resultado.values())
        resultado = {t: df for t, df in resultado.items()
                     if df.index[-1] >= ultima - pd.Timedelta(days=10)}
    return resultado


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
                      dragmode=False,
                      xaxis_rangeslider_visible=False,
                      margin=dict(t=30, b=20))
    fig.update_yaxes(range=[lo - margem, hi + margem], fixedrange=True, row=1, col=1)
    fig.update_yaxes(range=[0, 100], fixedrange=True, row=2, col=1)
    fig.update_xaxes(range=[inicio, final], fixedrange=True,
                     rangebreaks=[dict(bounds=["sat", "mon"])],
                     hoverformat="%d/%m/%Y")
    return fig


# ============ SENTINELA: SETUPS ============
def quando(k):
    return "hoje" if k == 0 else ("há 1 pregão" if k == 1 else f"há {k} pregões")


def avaliar_setups(d, janela_pivo, tolerancia):
    """Procura os setups no pregão mais recente. Devolve uma lista de sinais."""
    sinais = []
    if len(d) < 30:
        return sinais
    hoje = d.iloc[-1]

    # 1) Cruzamento das MME 9 e 21 (nos últimos 3 pregões)
    diferenca = d["MME9"] - d["MME21"]
    for k in range(3):
        atual, anterior = diferenca.iloc[-1 - k], diferenca.iloc[-2 - k]
        if anterior <= 0 < atual:
            sinais.append(("Cruzamento MME 9/21", "🟢 Alta",
                           f"MME 9 cruzou para cima da MME 21 {quando(k)}"))
            break
        if anterior >= 0 > atual:
            sinais.append(("Cruzamento MME 9/21", "🔴 Baixa",
                           f"MME 9 cruzou para baixo da MME 21 {quando(k)}"))
            break

    # 2) IFR extremo a favor da tendência de longo prazo
    if not pd.isna(hoje["MMA200"]):
        if hoje["IFR"] < 30 and hoje["Close"] > hoje["MMA200"]:
            sinais.append(("IFR sobrevendido em tendência de alta", "🟢 Alta",
                           f"IFR em {br(hoje['IFR'], 1)}, preço acima da MMA 200"))
        elif hoje["IFR"] > 70 and hoje["Close"] < hoje["MMA200"]:
            sinais.append(("IFR sobrecomprado em tendência de baixa", "🔴 Baixa",
                           f"IFR em {br(hoje['IFR'], 1)}, preço abaixo da MMA 200"))

    # 3 e 4) Suportes e resistências calculados até ontem, testados com o pregão de hoje
    suportes, resistencias = suportes_e_resistencias(d.iloc[:-1], janela_pivo, tolerancia, 1)
    vol_media = hoje["VolMedia20"]
    forca_volume = hoje["Volume"] / vol_media if vol_media and vol_media > 0 else 0
    volume_forte = forca_volume >= 1.5
    texto_volume = f"volume {br(forca_volume, 1)}x a média"

    if resistencias:
        r = resistencias[0][0]
        if hoje["Close"] > r and volume_forte:
            sinais.append(("Rompimento de resistência", "🟢 Alta",
                           f"Fechou acima de R$ {br(r)} com {texto_volume}"))
        elif hoje["High"] >= r * 0.99 and hoje["Close"] <= r:
            sinais.append(("Testando resistência", "🔴 Baixa",
                           f"Encostou em R$ {br(r)} e fechou abaixo"))
    if suportes:
        s_ = suportes[0][0]
        if hoje["Close"] < s_ and volume_forte:
            sinais.append(("Perda de suporte", "🔴 Baixa",
                           f"Fechou abaixo de R$ {br(s_)} com {texto_volume}"))
        elif hoje["Low"] <= s_ * 1.01 and hoje["Close"] >= s_:
            sinais.append(("Testando suporte", "🟢 Alta",
                           f"Encostou em R$ {br(s_)} e fechou acima"))
    return sinais


# ============ PÁGINAS ============
def config_grafico():
    return {"displayModeBar": False, "scrollZoom": False}


def pagina_graficos(texto, periodo_visivel, janela_pivo, tolerancia, max_niveis):
    st.title("📈 Painel B3")
    acoes = limpar_lista(texto)
    if not acoes:
        st.info("Digite ao menos um ativo na barra lateral para começar.")
        return

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
            st.plotly_chart(fig, key=f"grafico_{acao}", config=config_grafico())


def pagina_sentinela(janela_pivo, tolerancia, max_niveis):
    st.title("🛰️ Sentinela")
    st.write("Varre o Ibovespa e os principais ETFs em busca de setups técnicos "
             "no pregão mais recente. Os sinais são pontos de partida para a sua "
             "análise, não recomendações de compra ou venda.")

    with st.expander("Como ler os setups"):
        st.markdown(
            "**Cruzamento MME 9/21:** a média rápida cruzou a lenta nos últimos 3 pregões. "
            "Indica mudança de direção no curto prazo.\n\n"
            "**IFR sobrevendido em tendência de alta:** IFR abaixo de 30 com o preço acima da "
            "MMA 200, ou seja, uma queda forte dentro de uma tendência de alta. O espelho "
            "(IFR acima de 70 abaixo da MMA 200) aparece como sinal de baixa.\n\n"
            "**Rompimento de resistência / perda de suporte:** o fechamento atravessou o nível "
            "mais próximo com volume pelo menos 1,5x a média de 20 dias. Volume alto indica "
            "convicção no movimento.\n\n"
            "**Testando suporte / resistência:** o preço encostou no nível (até 1% de distância) "
            "e fechou do lado de dentro. Mostra o nível sendo defendido, mas não garante que "
            "ele vai segurar.")

    with st.expander("Universo de ativos"):
        texto_universo = st.text_area("Códigos analisados (edite à vontade)",
                                      " ".join(UNIVERSO_PADRAO), height=140)
    universo = tuple(limpar_lista(texto_universo))

    with st.spinner(f"Varrendo {len(universo)} ativos... na primeira vez leva alguns segundos"):
        todos = baixar_universo(universo)

    if not todos:
        st.error("Não consegui baixar os dados agora. O Yahoo pode estar limitando "
                 "os pedidos: aguarde alguns minutos e recarregue a página.")
        return

    ultima = max(df.index[-1] for df in todos.values())
    st.caption(f"{len(todos)} ativos analisados. Pregão de referência: "
               f"{ultima.strftime('%d/%m/%Y')}.")
    ignorados = [t for t in universo if t not in todos]
    if ignorados:
        st.caption("Sem dados recentes (confira os códigos): " + ", ".join(ignorados))

    linhas = []
    for ticker, d in todos.items():
        hoje = d.iloc[-1]
        for setup, direcao, detalhe in avaliar_setups(d, janela_pivo, tolerancia):
            linhas.append({
                "Ativo": ticker,
                "Direção": direcao,
                "Setup": setup,
                "Detalhe": detalhe,
                "Fechamento": f"R$ {br(hoje['Close'])}",
                "Var. dia": "—" if pd.isna(hoje["Variacao"]) else
                            f"{'+' if hoje['Variacao'] >= 0 else ''}{br(hoje['Variacao'])}%",
                "IFR": br(hoje["IFR"], 1),
            })

    if not linhas:
        st.info("Nenhum setup apareceu no último pregão. Isso acontece: "
                "às vezes o melhor sinal é não ter sinal.")
        return

    tabela = pd.DataFrame(linhas).sort_values(["Direção", "Setup", "Ativo"])
    filtro = st.radio("Mostrar", ["Todos", "🟢 Alta", "🔴 Baixa"], horizontal=True)
    if filtro != "Todos":
        tabela = tabela[tabela["Direção"] == filtro]

    st.write(f"**{len(tabela)} sinais encontrados**")
    st.dataframe(tabela, hide_index=True)

    if tabela.empty:
        return
    escolha = st.selectbox("Ver o gráfico de", sorted(tabela["Ativo"].unique()))
    d = todos[escolha]
    fig = grafico(d, escolha, 126, d.index[-1], janela_pivo, tolerancia, max_niveis)
    st.plotly_chart(fig, key="grafico_sentinela", config=config_grafico())


# ============ INTERFACE ============
with st.sidebar:
    pagina = st.radio("Página", ["📈 Gráficos", "🛰️ Sentinela"])
    st.divider()
    st.header("Configurações")

    if pagina == "📈 Gráficos":
        texto = st.text_input("Ativos (separados por vírgula)", "PETR4, VALE3, ITUB4, BOVA11")
        periodo_visivel = st.radio("Período visível", list(JANELAS_VISIVEIS), index=2)

    st.subheader("Suportes e resistências")
    janela_pivo = st.slider("Janela dos pivôs (dias)", 2, 15, 5)
    tolerancia = st.slider("Tolerância de agrupamento (%)", 0.5, 5.0, 2.0, 0.5) / 100
    max_niveis = st.slider("Níveis de cada lado", 1, 5, 3)

    st.caption("Dados do Yahoo Finance, com atraso. Uso educacional, "
               "não é recomendação de investimento.")

if pagina == "📈 Gráficos":
    pagina_graficos(texto, periodo_visivel, janela_pivo, tolerancia, max_niveis)
else:
    pagina_sentinela(janela_pivo, tolerancia, max_niveis)
