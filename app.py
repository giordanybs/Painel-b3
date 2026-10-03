import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import json
import os

import motor
from motor import *  # funções de análise compartilhadas com o alerta

st.set_page_config(page_title="Dashboard B3", page_icon="📈", layout="wide")

# Período inicial do gráfico, em meses. 6 meses é o padrão; os maiores são para consultas eventuais.
JANELAS_VISIVEIS = {"1 mês": 1, "3 meses": 3, "6 meses": 6, "1 ano": 12, "2 anos": 24, "5 anos": 60}
TEMPOS_GRAFICOS = {"Diário": None, "Semanal": "W-FRI", "Mensal": "MS"}


# ============ DADOS (com cache de 1 hora) ============
@st.cache_data(ttl=3600, show_spinner=False)
def preparar_dados(ticker, periodo="2y"):
    return motor.baixar_dados(ticker, periodo)


def reamostrar(dados, regra):
    """Transforma candles diários em semanais ou mensais."""
    if regra is None:
        return dados
    d = dados[["Open", "High", "Low", "Close", "Volume"]].copy()
    d["Data"] = d.index
    agrupado = d.groupby(pd.Grouper(freq=regra)).agg(
        {"Data": "first", "Open": "first", "High": "max", "Low": "min",
         "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    return adicionar_indicadores(agrupado.set_index("Data"))


@st.cache_data(ttl=3600, show_spinner=False)
def baixar_universo(tickers):
    return motor.baixar_universo(tickers)


@st.cache_data(ttl=3600, show_spinner=False)
def backtest(universo, janela_pivo, tolerancia, horizonte):
    return motor.backtest(baixar_universo(universo), janela_pivo, tolerancia, horizonte)


# ============ GRÁFICO ============
FUNDO = "#0e1117"   # mesmo fundo do tema escuro do Streamlit
VERDE, VERMELHO = "#26a69a", "#ef5350"


PAINEIS = ["IFR", "MACD"]


def altura_grafico(paineis):
    return 560 + 170 * len(paineis)


def grafico(dados, ticker, janela_pivo, tolerancia, max_niveis, paineis=("IFR", "MACD")):
    x = dados.index.tz_localize(None) if dados.index.tz is not None else dados.index
    paineis = [p for p in PAINEIS if p in paineis]
    alturas = [0.62, 0.13] + [0.25] * len(paineis)
    fig = make_subplots(rows=2 + len(paineis), cols=1, shared_xaxes=True,
                        vertical_spacing=0.02, row_heights=alturas)
    linha_do = {p: 3 + n for n, p in enumerate(paineis)}

    # Painel 1: candles, médias e suportes/resistências
    fig.add_trace(go.Candlestick(
        x=x, name=ticker,
        open=dados["Open"], high=dados["High"], low=dados["Low"], close=dados["Close"],
        increasing_line_color=VERDE, decreasing_line_color=VERMELHO,
        increasing_fillcolor=VERDE, decreasing_fillcolor=VERMELHO,
        text=[formatar_variacao(v) for v in dados["Variacao"]],
    ), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=dados["MME9"], name="MME 9", hovertemplate="%{y:.2f}",
                             line=dict(color="#ffb74d", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=dados["MME21"], name="MME 21", hovertemplate="%{y:.2f}",
                             line=dict(color="#64b5f6", width=1.5)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=dados["MMA200"], name="MMA 200", hovertemplate="%{y:.2f}",
                             line=dict(color="#ce93d8", width=2)), row=1, col=1)

    suportes, resistencias = suportes_e_resistencias(dados, janela_pivo, tolerancia, max_niveis)
    for nivel, toques in suportes:
        fig.add_hline(y=nivel, line_dash="dash", line_color="#66bb6a", line_width=1, opacity=0.8,
                      annotation_text=f"S {br(nivel)} ({toques}x)",
                      annotation_position="bottom left", annotation_font_color="#66bb6a",
                      row=1, col=1)
    for nivel, toques in resistencias:
        fig.add_hline(y=nivel, line_dash="dash", line_color=VERMELHO, line_width=1, opacity=0.8,
                      annotation_text=f"R {br(nivel)} ({toques}x)",
                      annotation_position="top left", annotation_font_color=VERMELHO,
                      row=1, col=1)

    # Painel 2: volume, verde nos candles de alta e vermelho nos de baixa
    cores_volume = [VERDE if c >= o else VERMELHO for o, c in zip(dados["Open"], dados["Close"])]
    fig.add_trace(go.Bar(x=x, y=dados["Volume"], name="Volume", marker_color=cores_volume,
                         marker_line_width=0, opacity=0.7, hovertemplate="%{y:,.0f}"),
                  row=2, col=1)

    # Painel do IFR
    if "IFR" in linha_do:
        r = linha_do["IFR"]
        fig.add_trace(go.Scatter(x=x, y=dados["IFR"], name="IFR 14", hovertemplate="%{y:.1f}",
                                 line=dict(color="#4dd0e1", width=1.5)), row=r, col=1)
        fig.add_hrect(y0=30, y1=70, fillcolor="gray", opacity=0.12, line_width=0, row=r, col=1)
        fig.add_hline(y=70, line_dash="dot", line_color=VERMELHO, row=r, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="#66bb6a", row=r, col=1)
        fig.update_yaxes(range=[0, 100], row=r, col=1)

    # Painel do MACD: histograma (cores mais claras quando a barra está diminuindo)
    if "MACD" in linha_do:
        r = linha_do["MACD"]
        hist = dados["MACD_Hist"]
        anterior = hist.shift(1)
        cores = ["#26a69a" if h >= 0 and h >= p else "#b2dfdb" if h >= 0 else
                 "#ef5350" if h < p else "#ffcdd2"
                 for h, p in zip(hist, anterior.fillna(0))]
        fig.add_trace(go.Bar(x=x, y=hist, name="Histograma", marker_color=cores,
                             marker_line_width=0, hovertemplate="%{y:.3f}"), row=r, col=1)
        fig.add_trace(go.Scatter(x=x, y=dados["MACD"], name="MACD", hovertemplate="%{y:.3f}",
                                 line=dict(color="#42a5f5", width=1.5)), row=r, col=1)
        fig.add_trace(go.Scatter(x=x, y=dados["MACD_Sinal"], name="Sinal", hovertemplate="%{y:.3f}",
                                 line=dict(color="#ff7043", width=1.5)), row=r, col=1)
        fig.add_hline(y=0, line_color="#555c6b", line_width=1, row=r, col=1)

    fig.update_layout(template="plotly_dark", paper_bgcolor=FUNDO, plot_bgcolor=FUNDO,
                      height=altura_grafico(paineis), hovermode="x unified", dragmode="pan",
                      separators=",.", bargap=0.15,
                      xaxis_rangeslider_visible=False, margin=dict(t=20, b=20, l=10, r=10),
                      legend=dict(orientation="h", y=1.04, x=0))
    fig.update_yaxes(fixedrange=True, gridcolor="#262b36")  # os eixos verticais se ajustam sozinhos
    fig.update_yaxes(showticklabels=False, showgrid=False, row=2, col=1)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])], hoverformat="%d/%m/%Y",
                     gridcolor="#262b36")
    return fig


def inicio_da_janela(dados, meses):
    """Primeira data dentro dos últimos `meses` meses."""
    limite = dados.index[-1] - pd.DateOffset(months=meses)
    dentro = dados.index[dados.index >= limite]
    return (dentro[0] if len(dentro) else dados.index[0]).strftime("%Y-%m-%d")


def mostrar_grafico(fig, dados, meses_iniciais=6, paineis=("IFR", "MACD")):
    """Desenha o gráfico com zoom de verdade: roda do mouse, arrastar, botões e
    eixos de preço e volume que se reenquadram sozinhos a cada movimento."""
    precos = dados[["Low", "High", "MME9", "MME21", "MMA200"]]
    faixa = {"datas": [d.strftime("%Y-%m-%d") for d in dados.index],
             "min": precos.min(axis=1).round(2).tolist(),
             "max": precos.max(axis=1).round(2).tolist(),
             "vol": dados["Volume"].fillna(0).tolist()}
    paineis = [p for p in PAINEIS if p in paineis]
    eixo_macd = f"yaxis{3 + paineis.index('MACD')}" if "MACD" in paineis else ""
    macd = dados[["MACD", "MACD_Sinal", "MACD_Hist"]]
    faixa["macd_min"] = macd.min(axis=1).fillna(0).round(4).tolist()
    faixa["macd_max"] = macd.max(axis=1).fillna(0).round(4).tolist()
    altura = altura_grafico(paineis)
    inicio = inicio_da_janela(dados, meses_iniciais)

    html_fig = fig.to_html(full_html=False, include_plotlyjs="cdn", div_id="grafico",
                           config={"responsive": True, "scrollZoom": True,
                                   "displayModeBar": False})
    pagina = f"""
    <style>
      html, body {{ margin: 0; padding: 0; background: {FUNDO}; overflow: hidden;
                    font-family: sans-serif; }}
      .barra {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
                height: 40px; overflow: hidden; }}
      .barra button {{ background: #1c2230; color: #e6e6e6; border: 1px solid #333a48;
                       border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 14px; }}
      .barra button:hover {{ background: #283044; }}
      .dica {{ color: #8b93a3; font-size: 12px; }}
    </style>
    <div class="barra">
      <button onclick="zoom(0.66)" title="Aproximar">🔍 +</button>
      <button onclick="zoom(1.5)" title="Afastar">🔍 −</button>
      <button onclick="mover(-0.5)" title="Voltar no tempo">◀</button>
      <button onclick="mover(0.5)" title="Avançar no tempo">▶</button>
      <button onclick="inicial()" title="Voltar ao enquadramento inicial">↺ Início</button>
      <span class="dica">Roda do mouse: zoom. Arrastar: mover.</span>
    </div>
    {html_fig}
    <script>
      const F = {json.dumps(faixa)};
      const T = F.datas.map(ms);
      const DIA = 86400000;
      const PASSO = T.length > 1 ? Math.max(DIA, (T[T.length - 1] - T[0]) / (T.length - 1)) : DIA;
      const PRIMEIRO = T[0] - 0.6 * PASSO, ULTIMO = T[T.length - 1] + 0.6 * PASSO;
      const INICIO = ms("{inicio}") - 0.6 * PASSO;
      const gd = document.getElementById("grafico");
      const EIXO_MACD = "{eixo_macd}";
      let ajustando = false;

      function ms(v) {{
        if (typeof v === "number") return v;
        let s = String(v).replace(" ", "T").slice(0, 19);
        if (s.length === 10) s += "T00:00:00";
        return Date.parse(s + "Z");
      }}
      function texto(t) {{ return new Date(t).toISOString().slice(0, 19).replace("T", " "); }}
      function faixaAtual() {{
        const r = gd.layout.xaxis.range;
        return [ms(r[0]), ms(r[1])];
      }}
      function ajustarY() {{
        const [a, b] = faixaAtual();
        let lo = Infinity, hi = -Infinity, vmax = 0, mlo = 0, mhi = 0;
        for (let i = 0; i < T.length; i++) {{
          if (T[i] >= a && T[i] <= b) {{
            if (F.min[i] !== null && F.min[i] < lo) lo = F.min[i];
            if (F.max[i] !== null && F.max[i] > hi) hi = F.max[i];
            if (F.vol[i] > vmax) vmax = F.vol[i];
            if (F.macd_min[i] < mlo) mlo = F.macd_min[i];
            if (F.macd_max[i] > mhi) mhi = F.macd_max[i];
          }}
        }}
        if (lo === Infinity) return;
        const m = (hi - lo) * 0.05 || hi * 0.01;
        const novo = {{"yaxis.range": [lo - m, hi + m], "yaxis2.range": [0, (vmax || 1) * 1.15]}};
        if (EIXO_MACD) {{
          const mm = (mhi - mlo) * 0.08 || 1;
          novo[EIXO_MACD + ".range"] = [mlo - mm, mhi + mm];
        }}
        ajustando = true;
        Plotly.relayout(gd, novo).then(() => ajustando = false, () => ajustando = false);
      }}
      function aplicar(a, b) {{
        const largura = Math.min(b - a, ULTIMO - PRIMEIRO);
        a = b - largura;
        if (a < PRIMEIRO) {{ a = PRIMEIRO; b = a + largura; }}
        if (b > ULTIMO) {{ b = ULTIMO; a = b - largura; }}
        const r = [texto(a), texto(b)];
        const novo = {{}};
        Object.keys(gd.layout).filter(k => /^xaxis\\d*$/.test(k)).forEach(k => novo[k + ".range"] = r);
        Plotly.relayout(gd, novo);
      }}
      function zoom(f) {{
        const [a, b] = faixaAtual();
        aplicar(b - Math.max((b - a) * f, 8 * PASSO), b);
      }}
      function mover(f) {{
        const [a, b] = faixaAtual();
        const passo = (b - a) * f;
        aplicar(a + passo, b + passo);
      }}
      function inicial() {{ aplicar(INICIO, ULTIMO); }}

      function iniciar() {{
        if (!gd || !gd.on || !gd.layout || !gd.layout.xaxis) return setTimeout(iniciar, 50);
        gd.on("plotly_relayout", ev => {{
          if (ajustando) return;
          if (Object.keys(ev).some(k => k.startsWith("xaxis"))) ajustarY();
        }});
        inicial();
      }}
      if (document.readyState === "complete") iniciar();
      else window.addEventListener("load", iniciar);
    </script>
    """
    st.iframe(pagina, height=altura + 50)


# ============ PÁGINAS ============
def config_grafico():
    return {"displayModeBar": False, "scrollZoom": False}


def pagina_graficos(texto, periodo_visivel, tempo_grafico, paineis, janela_pivo, tolerancia,
                    max_niveis):
    st.title("📈 Dashboard B3")
    acoes = limpar_lista(texto)
    if not acoes:
        st.info("Digite ao menos um ativo na barra lateral para começar.")
        return

    for aba, acao in zip(st.tabs(acoes), acoes):
        with aba:
            try:
                with st.spinner(f"Carregando {acao}..."):
                    periodo = "5y" if JANELAS_VISIVEIS[periodo_visivel] > 24 else "2y"
                    dados = preparar_dados(acao, periodo)
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
            if acao in INDICES:
                c1.metric("Fechamento", f"{br(ultimo['Close'], 0)} pts", variacao)
            else:
                c1.metric("Fechamento", f"R$ {br(ultimo['Close'])}", variacao)
            c2.metric("IFR 14", br(ultimo["IFR"], 1))
            c3.metric("Preço x MMA 200", tendencia)
            c4.metric("Último pregão", ultimo.name.strftime("%d/%m/%Y"))

            velas = reamostrar(dados, TEMPOS_GRAFICOS[tempo_grafico])
            fig = grafico(velas, acao, janela_pivo, tolerancia, max_niveis, paineis)
            mostrar_grafico(fig, velas, JANELAS_VISIVEIS[periodo_visivel], paineis)


def pagina_sentinela(universo, janela_pivo, tolerancia, max_niveis):
    st.title("🛰️ Sentinela")
    st.write("Varre o Ibovespa e os principais ETFs em busca de setups técnicos "
             "no pregão mais recente. Os sinais são pontos de partida para a sua "
             "análise, não recomendações de compra ou venda.")

    with st.expander("Como ler os setups"):
        st.markdown(
            "**Cruzamento MME 9/21:** a média rápida cruzou a lenta nos últimos 3 pregões. "
            "Indica mudança de direção no curto prazo.\n\n"
            "**Cruzamento MACD:** o MACD cruzou sua linha de sinal nos últimos 3 pregões, vindo "
            "do lado oposto da linha zero (para cima abaixo de zero, ou para baixo acima de zero). "
            "Indica que o momento virou depois de um movimento esticado.\n\n"
            "**Divergência no MACD:** o preço fez um fundo mais baixo que o anterior, mas o MACD "
            "fez um fundo mais alto (ou o espelho, com topos). O movimento continua, porém "
            "perdendo força. O sinal aparece quando o segundo fundo/topo é confirmado, alguns "
            "pregões depois de acontecer.\n\n"
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

    # Sinais de hoje + avaliação do Historiador
    crit = motor.CRITERIOS
    with st.spinner("Consultando o Historiador..."):
        sinais_hist, base = backtest(universo, janela_pivo, tolerancia, crit["horizonte"])
        resumo_ind = resumir(sinais_hist, base)
        resumo_comb = resumir(combinar(sinais_hist, crit["janela_combinacao"]), base)
    hoje = classificar_sinais_de_hoje(todos, resumo_ind, resumo_comb,
                                      janela_pivo, tolerancia, crit)

    if hoje.empty:
        st.info("Nenhum setup apareceu no último pregão. Isso acontece: "
                "às vezes o melhor sinal é não ter sinal.")
        return

    aprovados = hoje["Aprovado"].sum()
    if aprovados:
        st.success(f"🏅 {aprovados} sinais bem avaliados pelo Historiador hoje. Com os ajustes "
                   "padrão da barra lateral, são esses que vão para o seu e-mail.")
    else:
        st.caption("Nenhum sinal de hoje atingiu os critérios do Historiador.")

    filtro = st.radio("Mostrar", ["Todos", "🏅 Só bem avaliados", "🟢 Alta", "🔴 Baixa"],
                      horizontal=True)
    if filtro == "🏅 Só bem avaliados":
        hoje = hoje[hoje["Aprovado"]]
    elif filtro != "Todos":
        hoje = hoje[hoje["Direção"] == filtro]

    hoje = hoje.sort_values(["Aprovado", "Vantagem"], ascending=[False, False])
    tabela = pd.DataFrame({
        "🏅": np.where(hoje["Aprovado"], "🏅", ""),
        "Ativo": hoje["Ativo"], "Direção": hoje["Direção"], "Tipo": hoje["Tipo"],
        "Setup": hoje["Setup"], "Detalhe": hoje["Detalhe"],
        "Fechamento": hoje["Fechamento"].map(lambda v: f"R$ {br(v)}"),
        "Vantagem histórica": hoje["Vantagem"].map(
            lambda v: "—" if pd.isna(v) else f"{'+' if v > 0 else ''}{br(v, 1)} p.p."),
        "Taxa de acerto": hoje["Taxa de acerto"].map(
            lambda v: "—" if pd.isna(v) else f"{br(v, 1)}%"),
        "Amostra": hoje["Amostra"].map(lambda v: "—" if pd.isna(v) else int(v)),
        "Avaliação do Historiador": hoje["Motivo"],
    })
    st.write(f"**{len(tabela)} sinais encontrados**")
    st.dataframe(tabela, hide_index=True)
    st.caption(f"Histórico medido em {crit['horizonte']} pregões no último ano. Bem avaliado = "
               f"vantagem de pelo menos {br(crit['vantagem_minima'], 0)} p.p. sobre o acaso, "
               f"resultado médio positivo e amostra mínima de {crit['amostra_individual']} sinais "
               f"(setup isolado) ou {crit['amostra_combinacao']} (combinação).")

    if tabela.empty:
        return
    escolha = st.selectbox("Ver os motivos e o gráfico de", sorted(hoje["Ativo"].unique()))
    d = todos[escolha]
    for _, linha in hoje[hoje["Ativo"] == escolha].iterrows():
        marca = "🏅 " if linha["Aprovado"] else ""
        with st.expander(f"{marca}{linha['Direção']} | {linha['Setup']}", expanded=bool(linha["Aprovado"])):
            st.markdown(explicacao_markdown(explicar_sinal(d, linha, janela_pivo, tolerancia, crit)))
    mostrar_grafico(grafico(d, escolha, janela_pivo, tolerancia, max_niveis), d, 6)


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


# ============ CARTEIRA SIMULADA ============
PASTA_CARTEIRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carteira")


def reais(v, sinal=False):
    if pd.isna(v):
        return "—"
    if v < 0:
        return f"-R$ {br(abs(v))}"
    prefixo = "+" if sinal and v > 0 else ""
    return f"{prefixo}R$ {br(v)}"


def pagina_carteira():
    st.title("💼 Carteira Simulada")
    st.write("Todas as compras em confluências de alta, com R$ 1.000 fictícios cada. "
             "Atualizada automaticamente todo dia útil, após o fechamento.")

    with st.expander("Regras do simulado"):
        st.markdown(
            "- **Entra:** toda confluência de alta (2 ou mais setups de alta diferentes no "
            "mesmo ativo, em até 3 pregões), com ou sem a 🏅 do Historiador.\n"
            "- **Compra:** R$ 1.000 fictícios na **abertura do pregão seguinte** ao sinal.\n"
            "- **Stop loss:** o suporte mais próximo abaixo do preço de compra.\n"
            "- **Stop gain:** a primeira resistência que esteja a pelo menos **2x o risco** acima "
            "do preço de compra (risco/retorno mínimo de 2 para 1). Resistências mais próximas "
            "são ignoradas. Se nenhuma atender, o alvo é exatamente 2x o risco.\n"
            "- **Sem prazo:** a operação só termina no stop ou no alvo.\n"
            "- **Ativo já em carteira:** novos sinais dele são ignorados.\n"
            "- **Gap:** se abrir além do stop ou do alvo, a saída é no preço de abertura.\n"
            "- **Stop e alvo no mesmo dia:** considera o stop (hipótese conservadora).\n"
            "- **Custos:** 0,10% por operação. Proventos não são considerados.")

    arq_ops = os.path.join(PASTA_CARTEIRA, "operacoes.csv")
    arq_estado = os.path.join(PASTA_CARTEIRA, "estado.json")
    estado = {}
    if os.path.exists(arq_estado):
        with open(arq_estado, encoding="utf-8") as f:
            estado = json.load(f)
    ops = pd.read_csv(arq_ops) if os.path.exists(arq_ops) else pd.DataFrame()
    inicio = pd.Timestamp(estado.get("inicio", "2026-10-05"))

    if ops.empty:
        st.info(f"O simulado começa no pregão de {inicio.strftime('%d/%m/%Y')}. As primeiras "
                "ordens aparecem aqui depois do fechamento desse dia, e as compras são "
                "executadas na abertura do pregão seguinte.")
        return
    st.caption(f"Última atualização: {estado.get('atualizado_em', '—')}")

    for c in ("preco_entrada", "stop", "alvo", "preco_saida", "resultado_pct", "resultado_rs"):
        ops[c] = pd.to_numeric(ops[c], errors="coerce")
    ops["bem_avaliado"] = ops["bem_avaliado"].astype(str).str.lower() == "true"

    # Preço atual das operações abertas
    abertas = ops[ops["status"] == "Aberta"].copy()
    if not abertas.empty:
        with st.spinner("Atualizando preços..."):
            todos = baixar_universo(tuple(sorted(abertas["ativo"].unique())))
        abertas["atual"] = abertas["ativo"].map(
            lambda t: todos[t]["Close"].iloc[-1] if t in todos else np.nan)
        abertas["resultado_pct"] = (abertas["atual"] / abertas["preco_entrada"] - 1) * 100 - 0.10
        abertas["resultado_rs"] = 1000 * abertas["resultado_pct"] / 100
    encerradas = ops[ops["status"] == "Encerrada"].copy()

    realizado = encerradas["resultado_rs"].sum()
    em_aberto = abertas["resultado_rs"].sum() if not abertas.empty else 0.0
    acertos = (encerradas["resultado_rs"] > 0).sum()

    try:
        ibov = preparar_dados("IBOV")["Close"]
        antes = ibov[ibov.index.tz_localize(None) < inicio] if ibov.index.tz else ibov[ibov.index < inicio]
        ibov_pct = (ibov.iloc[-1] / antes.iloc[-1] - 1) * 100 if len(antes) else np.nan
    except Exception:
        ibov_pct = np.nan

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Resultado total", reais(realizado + em_aberto, True),
              f"realizado {reais(realizado, True)}", delta_color="off")
    c2.metric("Operações encerradas", len(encerradas),
              f"{br(acertos / len(encerradas) * 100, 0)}% de acerto" if len(encerradas) else None,
              delta_color="off")
    c3.metric("Abertas agora", len(abertas), f"em aberto {reais(em_aberto, True)}", delta_color="off")
    c4.metric("IBOV no mesmo período", "—" if pd.isna(ibov_pct) else
              f"{'+' if ibov_pct > 0 else ''}{br(ibov_pct)}%")

    if len(encerradas):
        st.caption(f"Resultado médio por operação encerrada: "
                   f"{'+' if encerradas['resultado_pct'].mean() > 0 else ''}"
                   f"{br(encerradas['resultado_pct'].mean())}%. "
                   f"Para comparar com o IBOV, some o resultado total ao capital máximo usado.")

        # Curva do resultado realizado
        curva = encerradas.sort_values("data_saida").groupby("data_saida")["resultado_rs"].sum().cumsum()
        fig = go.Figure(go.Scatter(x=pd.to_datetime(curva.index), y=curva.values, mode="lines+markers",
                                   line=dict(color="#26a69a"), hovertemplate="R$ %{y:,.2f}<extra></extra>"))
        fig.add_hline(y=0, line_color="gray")
        fig.update_layout(title="Resultado realizado acumulado (R$)", height=320, separators=",.",
                          margin=dict(t=50, b=20, l=10, r=10), dragmode=False)
        fig.update_xaxes(fixedrange=True, hoverformat="%d/%m/%Y")
        fig.update_yaxes(fixedrange=True)
        st.plotly_chart(fig, key="curva_carteira", config=config_grafico())

        # Com ou sem a 🏅 do Historiador
        grupos = []
        for nome, g in (("🏅 Bem avaliadas", encerradas[encerradas["bem_avaliado"]]),
                        ("Demais", encerradas[~encerradas["bem_avaliado"]])):
            if len(g):
                grupos.append({"Grupo": nome, "Operações": len(g),
                               "Acerto": f"{br((g['resultado_rs'] > 0).mean() * 100, 0)}%",
                               "Resultado médio": f"{br(g['resultado_pct'].mean())}%",
                               "Resultado total": reais(g["resultado_rs"].sum(), True)})
        st.write("**As bem avaliadas pelo Historiador se saíram melhor?**")
        st.dataframe(pd.DataFrame(grupos), hide_index=True)

    def data_br(s):
        return pd.to_datetime(s, errors="coerce").dt.strftime("%d/%m/%Y")

    if not abertas.empty:
        st.subheader("Operações abertas")
        st.dataframe(pd.DataFrame({
            "Ativo": abertas["ativo"], "🏅": np.where(abertas["bem_avaliado"], "🏅", ""),
            "Setups": abertas["setups"], "Entrada": data_br(abertas["data_entrada"]),
            "Preço de compra": abertas["preco_entrada"].map(reais),
            "Preço atual": abertas["atual"].map(reais),
            "Stop": abertas["stop"].map(reais), "Alvo": abertas["alvo"].map(reais),
            "Tipo de alvo": abertas["tipo_alvo"],
            "Resultado": abertas["resultado_rs"].map(lambda v: reais(v, True)),
        }), hide_index=True)

    pendentes = ops[ops["status"] == "Pendente"]
    if not pendentes.empty:
        st.subheader("Ordens para a abertura do próximo pregão")
        st.dataframe(pd.DataFrame({
            "Ativo": pendentes["ativo"], "🏅": np.where(pendentes["bem_avaliado"], "🏅", ""),
            "Setups": pendentes["setups"], "Sinal em": data_br(pendentes["data_sinal"]),
        }), hide_index=True)

    if not encerradas.empty:
        st.subheader("Operações encerradas")
        e = encerradas.sort_values("data_saida", ascending=False)
        st.dataframe(pd.DataFrame({
            "Ativo": e["ativo"], "🏅": np.where(e["bem_avaliado"], "🏅", ""),
            "Setups": e["setups"], "Entrada": data_br(e["data_entrada"]),
            "Saída": data_br(e["data_saida"]), "Compra": e["preco_entrada"].map(reais),
            "Venda": e["preco_saida"].map(reais), "Motivo": e["motivo_saida"],
            "Resultado": e["resultado_pct"].map(lambda v: f"{'+' if v > 0 else ''}{br(v)}%"),
            "R$": e["resultado_rs"].map(lambda v: reais(v, True)),
        }), hide_index=True)

    canceladas = ops[ops["status"] == "Cancelada"]
    if not canceladas.empty:
        with st.expander(f"Ordens canceladas ({len(canceladas)})"):
            st.dataframe(pd.DataFrame({
                "Ativo": canceladas["ativo"], "Sinal em": data_br(canceladas["data_sinal"]),
                "Setups": canceladas["setups"], "Motivo": canceladas["motivo_saida"],
            }), hide_index=True)


# ============ INTERFACE ============
with st.sidebar:
    pagina = st.radio("Página", ["📈 Gráficos", "🛰️ Sentinela", "🧪 Historiador",
                                 "💼 Carteira Simulada"])
    st.divider()
    st.header("Configurações")

    if pagina == "📈 Gráficos":
        texto = st.text_input("Ativos (separados por vírgula)", "IBOV",
                              help="Ex.: IBOV, PETR4, VALE3, BOVA11. O IBOV é o índice Bovespa.")
        tempo_grafico = st.radio("Tempo gráfico", list(TEMPOS_GRAFICOS), horizontal=True,
                                 help="Cada candle representa um dia, uma semana ou um mês.")
        paineis = st.multiselect("Indicadores abaixo do gráfico", PAINEIS, default=PAINEIS,
                                 help="Desmarque para deixar o gráfico mais limpo, "
                                      "principalmente no celular.")
        periodo_visivel = st.selectbox("Período inicial do gráfico", list(JANELAS_VISIVEIS), index=2,
                                       help="6 meses é o padrão. Os períodos longos baixam mais "
                                            "dados e podem demorar um pouco mais.")
    elif pagina != "💼 Carteira Simulada":
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
    pagina_graficos(texto, periodo_visivel, tempo_grafico, paineis, janela_pivo, tolerancia,
                    max_niveis)
elif pagina == "🛰️ Sentinela":
    pagina_sentinela(universo, janela_pivo, tolerancia, max_niveis)
elif pagina == "🧪 Historiador":
    pagina_historiador(universo, janela_pivo, tolerancia)
else:
    pagina_carteira()
