import re
import itertools
import numpy as np
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


# ============ MOTOR DE SETUPS (usado pela Sentinela e pelo Historiador) ============
def quando(k):
    return "hoje" if k == 0 else ("há 1 pregão" if k == 1 else f"há {k} pregões")


def preparar_arrays(d, janela_pivo):
    """Converte a tabela em listas numéricas e marca os pivôs de uma vez só (rápido)."""
    tamanho = 2 * janela_pivo + 1
    maximas, minimas = d["High"], d["Low"]
    topo = (maximas == maximas.rolling(tamanho, center=True).max()).to_numpy()
    fundo = (minimas == minimas.rolling(tamanho, center=True).min()).to_numpy()
    return {
        "close": d["Close"].to_numpy(), "high": maximas.to_numpy(), "low": minimas.to_numpy(),
        "vol": d["Volume"].to_numpy(), "volmed": d["VolMedia20"].to_numpy(),
        "mme9": d["MME9"].to_numpy(), "mme21": d["MME21"].to_numpy(),
        "mma200": d["MMA200"].to_numpy(), "ifr": d["IFR"].to_numpy(),
        "piv_topo": np.where(topo, maximas.to_numpy(), np.nan),
        "piv_fundo": np.where(fundo, minimas.to_numpy(), np.nan),
    }


def niveis_no_dia(a, i, janela_pivo, tolerancia):
    """Suporte e resistência mais próximos, usando só pivôs já confirmados até o dia anterior."""
    ini, fim = max(0, i - 250), i - janela_pivo
    if fim <= ini:
        return None, None
    precos = np.concatenate([a["piv_topo"][ini:fim], a["piv_fundo"][ini:fim]])
    precos = precos[~np.isnan(precos)].tolist()
    niveis = [n[0] for n in agrupar_niveis(precos, tolerancia) if n[1] >= 2]
    referencia = a["close"][i - 1]
    suporte = max((n for n in niveis if n < referencia), default=None)
    resistencia = min((n for n in niveis if n > referencia), default=None)
    return suporte, resistencia


def setups_no_dia(a, i, janela_pivo, tolerancia, janela_cruzamento=1):
    """Procura os setups no dia i, olhando apenas dados até esse dia."""
    sinais = []
    if i < 30:
        return sinais
    fech, maxima, minima = a["close"][i], a["high"][i], a["low"][i]

    # 1) Cruzamento das MME 9 e 21
    for k in range(janela_cruzamento):
        atual = a["mme9"][i - k] - a["mme21"][i - k]
        anterior = a["mme9"][i - k - 1] - a["mme21"][i - k - 1]
        if anterior <= 0 < atual:
            sinais.append(("Cruzamento MME 9/21", "🟢 Alta",
                           f"MME 9 cruzou para cima da MME 21 {quando(k)}"))
            break
        if anterior >= 0 > atual:
            sinais.append(("Cruzamento MME 9/21", "🔴 Baixa",
                           f"MME 9 cruzou para baixo da MME 21 {quando(k)}"))
            break

    # 2) IFR extremo a favor da tendência de longo prazo
    ifr, mma200 = a["ifr"][i], a["mma200"][i]
    if not np.isnan(mma200):
        if ifr < 30 and fech > mma200:
            sinais.append(("IFR sobrevendido em tendência de alta", "🟢 Alta",
                           f"IFR em {br(ifr, 1)}, preço acima da MMA 200"))
        elif ifr > 70 and fech < mma200:
            sinais.append(("IFR sobrecomprado em tendência de baixa", "🔴 Baixa",
                           f"IFR em {br(ifr, 1)}, preço abaixo da MMA 200"))

    # 3 e 4) Suportes e resistências
    suporte, resistencia = niveis_no_dia(a, i, janela_pivo, tolerancia)
    vol_media = a["volmed"][i]
    forca = a["vol"][i] / vol_media if vol_media and vol_media > 0 else 0
    volume_forte = forca >= 1.5
    texto_volume = f"volume {br(forca, 1)}x a média"

    if resistencia is not None:
        if fech > resistencia and volume_forte:
            sinais.append(("Rompimento de resistência", "🟢 Alta",
                           f"Fechou acima de R$ {br(resistencia)} com {texto_volume}"))
        elif (maxima >= resistencia * 0.99 and fech <= resistencia
              and a["high"][i - 1] < resistencia * 0.99):  # só o primeiro toque
            sinais.append(("Testando resistência", "🔴 Baixa",
                           f"Encostou em R$ {br(resistencia)} e fechou abaixo"))
    if suporte is not None:
        if fech < suporte and volume_forte:
            sinais.append(("Perda de suporte", "🔴 Baixa",
                           f"Fechou abaixo de R$ {br(suporte)} com {texto_volume}"))
        elif (minima <= suporte * 1.01 and fech >= suporte
              and a["low"][i - 1] > suporte * 1.01):  # só o primeiro toque
            sinais.append(("Testando suporte", "🟢 Alta",
                           f"Encostou em R$ {br(suporte)} e fechou acima"))
    return sinais


def avaliar_setups(d, janela_pivo, tolerancia):
    """Sinais do pregão mais recente (cruzamentos dos últimos 3 pregões)."""
    a = preparar_arrays(d, janela_pivo)
    return setups_no_dia(a, len(d) - 1, janela_pivo, tolerancia, janela_cruzamento=3)


# ============ HISTORIADOR: BACKTEST ============
@st.cache_data(ttl=3600, show_spinner=False)
def backtest(universo, janela_pivo, tolerancia, horizonte, dias=252):
    """Aplica os setups a cada pregão do último ano e mede o que aconteceu depois."""
    todos = baixar_universo(universo)
    registros, base = [], []
    for ticker, d in todos.items():
        a = preparar_arrays(d, janela_pivo)
        fech = a["close"]
        n = len(d)
        for i in range(max(31, n - dias), n):
            futuro = i + horizonte
            retorno = (fech[futuro] / fech[i] - 1) * 100 if futuro < n else np.nan
            if not np.isnan(retorno):
                base.append(retorno)
            for setup, direcao, _ in setups_no_dia(a, i, janela_pivo, tolerancia):
                registros.append({"Ativo": ticker, "Data": d.index[i], "Pos": i,
                                  "Setup": setup, "Direção": direcao, "Retorno": retorno})
    return pd.DataFrame(registros), np.array(base)


def combinar(sinais, janela):
    """Encontra os dias em que dois setups diferentes, na mesma direção, apareceram
    no mesmo ativo com no máximo `janela` pregões de distância. O resultado é medido
    a partir do dia em que o segundo sinal completou a combinação."""
    eventos = []
    for (ativo, direcao), g in sinais.groupby(["Ativo", "Direção"]):
        por_setup = {s: sorted(gg["Pos"]) for s, gg in g.groupby("Setup")}
        retorno = dict(zip(g["Pos"], g["Retorno"]))
        data = dict(zip(g["Pos"], g["Data"]))
        for x, y in itertools.combinations(sorted(por_setup), 2):
            dias = set()
            for a_, b_ in ((por_setup[x], por_setup[y]), (por_setup[y], por_setup[x])):
                for dia in a_:
                    if any(dia - janela <= p <= dia for p in b_):
                        dias.add(dia)
            for dia in dias:
                eventos.append({"Setup": f"{x} + {y}", "Direção": direcao, "Ativo": ativo,
                                "Data": data[dia], "Retorno": retorno[dia]})
    return pd.DataFrame(eventos)


def resumir(sinais, base):
    """Calcula acertos, erros e médias por setup."""
    taxa_base_alta = (base > 0).mean() * 100
    taxa_base_baixa = (base < 0).mean() * 100
    linhas = []
    for (setup, direcao), g in sinais.groupby(["Setup", "Direção"]):
        encerrados = g.dropna(subset=["Retorno"])
        sinal = 1 if direcao == "🟢 Alta" else -1
        resultado = encerrados["Retorno"] * sinal   # positivo = o preço foi para o lado previsto
        acertos, erros = resultado[resultado > 0], resultado[resultado <= 0]
        taxa = len(acertos) / len(resultado) * 100 if len(resultado) else np.nan
        base_dir = taxa_base_alta if sinal == 1 else taxa_base_baixa
        linhas.append({
            "Setup": setup, "Direção": direcao,
            "Sinais": len(g), "Em aberto": len(g) - len(encerrados),
            "Acertos": len(acertos), "Erros": len(erros),
            "Taxa de acerto": taxa, "Acaso": base_dir, "Vantagem": taxa - base_dir,
            "Resultado médio": resultado.mean(),
            "Média nos acertos": acertos.mean(), "Média nos erros": erros.mean(),
        })
    return pd.DataFrame(linhas).sort_values("Vantagem", ascending=False)


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


def pagina_sentinela(universo, janela_pivo, tolerancia, max_niveis):
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
            "**Testando suporte / resistência:** o preço chegou ao nível (até 1% de distância) "
            "vindo de longe e fechou do lado de dentro. Mostra o nível sendo defendido, mas não garante que "
            "ele vai segurar.")

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

    tabela = pd.DataFrame(linhas)
    contagem = tabela.groupby(["Ativo", "Direção"])["Setup"].transform("count")
    tabela.insert(0, "Confluência", np.where(contagem >= 2, "⭐", ""))
    tabela = tabela.sort_values(["Confluência", "Direção", "Ativo"], ascending=[False, True, True])
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


def pct(v, casas=1, sinal=False):
    if pd.isna(v):
        return "—"
    prefixo = "+" if sinal and v > 0 else ""
    return f"{prefixo}{br(v, casas)}%"


def tabela_resumo(resumo):
    return pd.DataFrame({
        "Setup": resumo["Setup"], "Direção": resumo["Direção"],
        "Sinais": resumo["Sinais"], "Acertos": resumo["Acertos"], "Erros": resumo["Erros"],
        "Em aberto": resumo["Em aberto"],
        "Taxa de acerto": resumo["Taxa de acerto"].map(pct),
        "Acaso": resumo["Acaso"].map(pct),
        "Vantagem": resumo["Vantagem"].map(lambda v: pct(v, 1, True).replace("%", " p.p.")),
        "Resultado médio": resumo["Resultado médio"].map(lambda v: pct(v, 2, True)),
        "Média nos acertos": resumo["Média nos acertos"].map(lambda v: pct(v, 2, True)),
        "Média nos erros": resumo["Média nos erros"].map(lambda v: pct(v, 2, True)),
    })


def grafico_vantagem(resumo, titulo, chave):
    rotulos = [f"{r['Setup']} ({r['Direção'][2:]})" for _, r in resumo.iterrows()]
    cores = ["seagreen" if v > 0 else "indianred" for v in resumo["Vantagem"]]
    fig = go.Figure(go.Bar(
        x=resumo["Vantagem"], y=rotulos, orientation="h", marker_color=cores,
        text=[pct(v, 1, True).replace("%", " p.p.") for v in resumo["Vantagem"]],
        textposition="outside", hovertemplate="%{y}: %{x:.1f} p.p.<extra></extra>",
    ))
    fig.add_vline(x=0, line_color="gray")
    fig.update_layout(title=titulo, height=max(300, 45 * len(resumo) + 80),
                      margin=dict(l=10, r=50, t=50, b=20), dragmode=False)
    fig.update_yaxes(autorange="reversed", fixedrange=True)
    fig.update_xaxes(fixedrange=True)
    st.plotly_chart(fig, key=chave, config=config_grafico())


def lista_de_sinais(eventos, horizonte, chave):
    opcoes = sorted({f"{s} | {d}" for s, d in zip(eventos["Setup"], eventos["Direção"])})
    escolha = st.selectbox("Escolha um setup para ver os sinais", opcoes, key=chave)
    setup, direcao = escolha.split(" | ")
    detalhe = eventos[(eventos["Setup"] == setup) & (eventos["Direção"] == direcao)]
    detalhe = detalhe.sort_values("Data", ascending=False)
    sinal = 1 if direcao == "🟢 Alta" else -1

    def situacao(r):
        if pd.isna(r):
            return "⏳ Em aberto"
        return "✅ Acerto" if r * sinal > 0 else "❌ Erro"

    st.dataframe(pd.DataFrame({
        "Data": detalhe["Data"].dt.strftime("%d/%m/%Y"),
        "Ativo": detalhe["Ativo"],
        f"Variação em {horizonte} pregões": detalhe["Retorno"].map(lambda v: pct(v, 2, True)),
        "Resultado": detalhe["Retorno"].map(situacao),
    }), hide_index=True)


def pagina_historiador(universo, janela_pivo, tolerancia):
    st.title("🧪 Historiador")
    st.write("Aplica os setups da Sentinela a cada pregão do último ano e verifica o que "
             "aconteceu com o preço depois. Resultados passados não garantem resultados futuros.")

    horizonte = st.radio("Avaliar o resultado depois de", [5, 10, 20], index=1, horizontal=True,
                         format_func=lambda h: f"{h} pregões")

    with st.spinner("Voltando no tempo... a primeira análise leva alguns segundos"):
        sinais, base = backtest(universo, janela_pivo, tolerancia, horizonte)

    if sinais.empty or len(base) == 0:
        st.warning("Não encontrei sinais suficientes para analisar. Tente novamente em alguns minutos.")
        return

    resumo = resumir(sinais, base)

    with st.expander("Como ler esta página"):
        st.markdown(
            f"Para cada sinal, olhamos o fechamento **{horizonte} pregões depois**. "
            "Um sinal de alta **acertou** se o preço subiu nesse período; um de baixa, se caiu.\n\n"
            "**Acaso** é a taxa de acerto de quem apostasse na mesma direção em um dia qualquer, "
            "sem setup nenhum. Se o mercado subiu em 55% dos períodos, um setup de alta que acerta "
            "55% não trouxe informação nova. Por isso, a coluna mais importante é a **Vantagem**: "
            "quantos pontos percentuais o setup acertou acima do acaso.\n\n"
            "**Resultado médio** mostra quanto o preço andou a favor (+) ou contra (−) o sinal, "
            "em média. Um setup pode acertar pouco e ainda assim ser bom, se ganhar muito quando "
            "acerta e perder pouco quando erra.\n\n"
            "**Em aberto** são os sinais recentes que ainda não completaram o prazo.")

    aba_individual, aba_combinada = st.tabs(["Setups individuais", "Combinações de dois sinais"])

    with aba_individual:
        grafico_vantagem(resumo, "Vantagem sobre o acaso (pontos percentuais)", "vant_ind")
        st.dataframe(tabela_resumo(resumo), hide_index=True)
        st.caption(f"{len(sinais)} sinais entre {sinais['Data'].min().strftime('%d/%m/%Y')} e "
                   f"{sinais['Data'].max().strftime('%d/%m/%Y')}, "
                   f"em {sinais['Ativo'].nunique()} ativos.")
        lista_de_sinais(sinais, horizonte, "lista_ind")

    with aba_combinada:
        st.write("Aqui, um sinal só conta quando **dois setups diferentes, na mesma direção**, "
                 "aparecem no mesmo ativo em um intervalo curto. A pergunta é: a confirmação "
                 "de um segundo sinal melhora o resultado?")
        janela = st.radio("Os dois sinais devem aparecer em até", [0, 3, 5], index=1,
                          horizontal=True,
                          format_func=lambda j: "no mesmo dia" if j == 0 else f"{j} pregões")
        minimo = st.slider("Mostrar só combinações com pelo menos N sinais encerrados", 5, 50, 15)

        eventos = combinar(sinais, janela)
        if eventos.empty:
            st.info("Nenhuma combinação aconteceu nesse período. Tente aumentar o intervalo.")
        else:
            combos = resumir(eventos, base)
            combos = combos[(combos["Sinais"] - combos["Em aberto"]) >= minimo]

            if combos.empty:
                st.info("Nenhuma combinação atingiu o mínimo de sinais. Reduza o mínimo "
                        "ou aumente o intervalo entre os sinais.")
            else:
                # Compara com o melhor dos dois setups isolados
                vant_ind = {(r["Setup"], r["Direção"]): r["Vantagem"] for _, r in resumo.iterrows()}
                ganho = []
                for _, r in combos.iterrows():
                    partes = r["Setup"].split(" + ")
                    melhor = max(vant_ind.get((p, r["Direção"]), np.nan) for p in partes)
                    ganho.append(r["Vantagem"] - melhor)
                combos = combos.assign(Ganho=ganho)

                grafico_vantagem(combos, "Vantagem das combinações sobre o acaso", "vant_comb")
                tabela = tabela_resumo(combos)
                tabela.insert(9, "Ganho vs. melhor isolado", combos["Ganho"].map(
                    lambda v: pct(v, 1, True).replace("%", " p.p.")).values)
                st.dataframe(tabela, hide_index=True)
                st.caption("**Ganho vs. melhor isolado** compara a vantagem da combinação com a "
                           "do melhor dos dois setups sozinho. Positivo significa que esperar "
                           "a confirmação valeu a pena. Com poucos sinais, diferenças grandes "
                           "podem ser pura sorte.")
                lista_de_sinais(eventos[eventos["Setup"].isin(combos["Setup"])],
                                horizonte, "lista_comb")

    with st.expander("Limitações desta análise"):
        st.markdown(
            "**Um ano é pouco.** Um setup pode ter ido bem num ano de alta e mal num ano de "
            "baixa. Trate os números como pistas, não como leis. Nas combinações, a amostra "
            "é ainda menor, então o cuidado deve ser redobrado.\n\n"
            "**Viés de sobrevivência.** Testamos as empresas que estão no Ibovespa *hoje*. As que "
            "caíram tanto que saíram do índice não entram no teste, o que tende a deixar os "
            "resultados de alta mais bonitos do que foram de verdade.\n\n"
            "**Sem custos.** Corretagem, emolumentos, impostos e a diferença entre o preço do "
            "sinal e o preço que você conseguiria pagar não foram descontados.\n\n"
            "**Sinais repetidos.** Um mesmo ativo pode disparar o mesmo setup em dias próximos, "
            "e cada dia conta como um sinal.")


# ============ INTERFACE ============
with st.sidebar:
    pagina = st.radio("Página", ["📈 Gráficos", "🛰️ Sentinela", "🧪 Historiador"])
    st.divider()
    st.header("Configurações")

    if pagina == "📈 Gráficos":
        texto = st.text_input("Ativos (separados por vírgula)", "PETR4, VALE3, ITUB4, BOVA11")
        periodo_visivel = st.radio("Período visível", list(JANELAS_VISIVEIS), index=2)
    else:
        with st.expander("Universo de ativos"):
            texto_universo = st.text_area("Códigos analisados (edite à vontade)",
                                          " ".join(UNIVERSO_PADRAO), height=160)
        universo = tuple(limpar_lista(texto_universo))

    st.subheader("Suportes e resistências")
    janela_pivo = st.slider("Janela dos pivôs (dias)", 2, 15, 5)
    tolerancia = st.slider("Tolerância de agrupamento (%)", 0.5, 5.0, 2.0, 0.5) / 100
    max_niveis = st.slider("Níveis de cada lado", 1, 5, 3)

    st.caption("Dados do Yahoo Finance, com atraso. Uso educacional, "
               "não é recomendação de investimento.")

if pagina == "📈 Gráficos":
    pagina_graficos(texto, periodo_visivel, janela_pivo, tolerancia, max_niveis)
elif pagina == "🛰️ Sentinela":
    pagina_sentinela(universo, janela_pivo, tolerancia, max_niveis)
else:
    pagina_historiador(universo, janela_pivo, tolerancia)
