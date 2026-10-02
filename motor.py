"""Motor de análise: dados, indicadores, setups e backtest.
Não depende do Streamlit, por isso é usado tanto pelo painel (app.py)
quanto pelo alerta diário por e-mail (alerta.py)."""
import re
import itertools
import numpy as np
import pandas as pd
import yfinance as yf

PERIODO = "2y"

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
    # MACD (12, 26, 9): diferença entre duas médias exponenciais e a média dessa diferença
    dados["MACD"] = (dados["Close"].ewm(span=12, adjust=False).mean()
                     - dados["Close"].ewm(span=26, adjust=False).mean())
    dados["MACD_Sinal"] = dados["MACD"].ewm(span=9, adjust=False).mean()
    dados["MACD_Hist"] = dados["MACD"] - dados["MACD_Sinal"]
    return dados


INDICES = {"IBOV": "^BVSP", "IBOVESPA": "^BVSP"}


def simbolo_yahoo(ticker):
    """Código no Yahoo: índices têm códigos próprios; ações e ETFs levam o sufixo .SA"""
    return INDICES.get(ticker, ticker + ".SA")


def baixar_dados(ticker, periodo=PERIODO):
    dados = yf.Ticker(simbolo_yahoo(ticker)).history(period=periodo)
    dados = adicionar_indicadores(dados)
    if dados.empty:
        raise ValueError("não encontrei dados. Confira o código do ativo.")
    return dados


def baixar_universo(tickers):
    """Baixa todos os ativos de uma vez só (bem mais rápido que um por um)."""
    simbolos = [simbolo_yahoo(t) for t in tickers]
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
        "macd": d["MACD"].to_numpy(), "macd_sinal": d["MACD_Sinal"].to_numpy(),
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


def pivo_anterior(pivos, de, ate):
    """Índice do pivô mais recente entre as posições `ate` e `de` (andando para trás)."""
    for j in range(de, max(ate, 0) - 1, -1):
        if not np.isnan(pivos[j]):
            return j
    return None


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

    # 5) Cruzamento do MACD com a linha de sinal, vindo do lado "esticado" da linha zero
    for k in range(janela_cruzamento):
        macd, sinal_ = a["macd"][i - k], a["macd_sinal"][i - k]
        atual = macd - sinal_
        anterior = a["macd"][i - k - 1] - a["macd_sinal"][i - k - 1]
        if anterior <= 0 < atual and macd < 0:
            sinais.append(("Cruzamento MACD", "🟢 Alta",
                           f"MACD cruzou para cima da linha de sinal abaixo de zero {quando(k)}"))
            break
        if anterior >= 0 > atual and macd > 0:
            sinais.append(("Cruzamento MACD", "🔴 Baixa",
                           f"MACD cruzou para baixo da linha de sinal acima de zero {quando(k)}"))
            break

    # 6) Divergência no MACD: o preço renova o fundo (ou topo), mas o MACD não acompanha.
    #    O sinal aparece quando o segundo fundo/topo acaba de ser confirmado como pivô.
    for k in range(janela_cruzamento):
        j2 = i - k - janela_pivo
        if j2 < 10:
            break
        if not np.isnan(a["piv_fundo"][j2]):
            j1 = pivo_anterior(a["piv_fundo"], j2 - 5, j2 - 60)
            if (j1 is not None and a["low"][j2] < a["low"][j1]
                    and a["macd"][j2] > a["macd"][j1] and a["macd"][j2] < 0):
                sinais.append(("Divergência no MACD", "🟢 Alta",
                               f"Preço fez fundo mais baixo (R$ {br(a['low'][j2])} contra "
                               f"R$ {br(a['low'][j1])}), mas o MACD fez fundo mais alto; "
                               f"confirmado {quando(k)}"))
                break
        if not np.isnan(a["piv_topo"][j2]):
            j1 = pivo_anterior(a["piv_topo"], j2 - 5, j2 - 60)
            if (j1 is not None and a["high"][j2] > a["high"][j1]
                    and a["macd"][j2] < a["macd"][j1] and a["macd"][j2] > 0):
                sinais.append(("Divergência no MACD", "🔴 Baixa",
                               f"Preço fez topo mais alto (R$ {br(a['high'][j2])} contra "
                               f"R$ {br(a['high'][j1])}), mas o MACD fez topo mais baixo; "
                               f"confirmado {quando(k)}"))
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
def backtest(todos, janela_pivo, tolerancia, horizonte, dias=252):
    """Aplica os setups a cada pregão do último ano e mede o que aconteceu depois."""
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
    if sinais.empty:
        return pd.DataFrame(columns=["Setup", "Direção", "Sinais", "Em aberto", "Acertos",
                                     "Erros", "Taxa de acerto", "Acaso", "Vantagem",
                                     "Resultado médio", "Média nos acertos", "Média nos erros"])
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


# ============ SINAIS DE HOJE + AVALIAÇÃO DO HISTORIADOR ============
# Critérios para um sinal ser considerado "bem avaliado" (usados no e-mail e na Sentinela)
CRITERIOS = {
    "horizonte": 10,         # prazo, em pregões, usado para medir acertos
    "vantagem_minima": 5.0,  # pontos percentuais acima do acaso
    "resultado_minimo": 0.0, # resultado médio precisa ser positivo
    "amostra_individual": 30,   # mínimo de sinais encerrados para um setup isolado
    "amostra_combinacao": 15,   # mínimo de sinais encerrados para uma combinação
    "janela_combinacao": 3,     # pregões entre os dois sinais de uma combinação
    "janela_pivo": 5,
    "tolerancia": 0.02,
}


def avaliar_historico(resumo, setup, direcao, amostra_minima, crit):
    linha = resumo[(resumo["Setup"] == setup) & (resumo["Direção"] == direcao)]
    if linha.empty:
        return None
    r = linha.iloc[0]
    encerrados = int(r["Sinais"] - r["Em aberto"])
    aprovado = bool(encerrados >= amostra_minima
                    and r["Vantagem"] >= crit["vantagem_minima"]
                    and r["Resultado médio"] > crit["resultado_minimo"])
    if aprovado:
        motivo = "Aprovado"
    elif encerrados < amostra_minima:
        motivo = f"Histórico pequeno: {encerrados} sinais (mínimo {amostra_minima})"
    elif r["Vantagem"] < crit["vantagem_minima"]:
        motivo = (f"Vantagem de {br(r['Vantagem'], 1)} p.p., abaixo do mínimo de "
                  f"{br(crit['vantagem_minima'], 0)} p.p.")
    else:
        motivo = f"Resultado médio de {br(r['Resultado médio'], 2)}%, não positivo"
    return {"Taxa de acerto": r["Taxa de acerto"], "Acaso": r["Acaso"],
            "Vantagem": r["Vantagem"], "Resultado médio": r["Resultado médio"],
            "Média nos acertos": r["Média nos acertos"], "Média nos erros": r["Média nos erros"],
            "Amostra": encerrados, "Aprovado": aprovado, "Motivo": motivo}


SEM_HISTORICO = {"Aprovado": False, "Motivo": "Sem histórico deste sinal no último ano"}


def classificar_sinais_de_hoje(todos, resumo_ind, resumo_comb, janela_pivo, tolerancia, crit):
    """Junta os sinais de hoje (Sentinela) com o desempenho histórico (Historiador)."""
    linhas = []
    for ticker, d in todos.items():
        hoje = d.iloc[-1]
        sinais = avaliar_setups(d, janela_pivo, tolerancia)
        base = {"Ativo": ticker, "Fechamento": hoje["Close"], "Var. dia": hoje["Variacao"],
                "IFR": hoje["IFR"]}
        for setup, direcao, detalhe in sinais:
            hist = avaliar_historico(resumo_ind, setup, direcao, crit["amostra_individual"], crit)
            linhas.append({**base, "Tipo": "Individual", "Setup": setup, "Direção": direcao,
                           "Detalhe": detalhe, **(hist or SEM_HISTORICO)})
        # Combinações de dois setups na mesma direção
        for direcao in ("🟢 Alta", "🔴 Baixa"):
            detalhes = {s: det for s, dr, det in sinais if dr == direcao}
            mesmos = sorted(detalhes)
            for x, y in itertools.combinations(mesmos, 2):
                nome = f"{x} + {y}"
                hist = avaliar_historico(resumo_comb, nome, direcao, crit["amostra_combinacao"], crit)
                linhas.append({**base, "Tipo": "Combinação", "Setup": nome, "Direção": direcao,
                               "Detalhe": f"{detalhes[x]}; {detalhes[y]}",
                               **(hist or SEM_HISTORICO)})
    colunas = ["Ativo", "Tipo", "Setup", "Direção", "Detalhe", "Fechamento", "Var. dia", "IFR",
               "Taxa de acerto", "Acaso", "Vantagem", "Resultado médio", "Média nos acertos",
               "Média nos erros", "Amostra", "Aprovado", "Motivo"]
    return pd.DataFrame(linhas, columns=colunas)


# ============ EXPLICAÇÃO DOS SINAIS ============
DESCRICOES = {
    "Cruzamento MME 9/21 | Alta":
        "A média de curto prazo (MME 9) cruzou para cima da média de médio prazo (MME 21), "
        "sugerindo que os compradores voltaram a dominar o curto prazo.",
    "Cruzamento MME 9/21 | Baixa":
        "A média de curto prazo (MME 9) cruzou para baixo da média de médio prazo (MME 21), "
        "sugerindo que os vendedores passaram a dominar o curto prazo.",
    "IFR sobrevendido em tendência de alta | Alta":
        "O ativo caiu forte em pouco tempo (IFR abaixo de 30), mas continua em tendência de alta "
        "de longo prazo. Quedas bruscas dentro de tendências de alta costumam ser seguidas de repique.",
    "IFR sobrecomprado em tendência de baixa | Baixa":
        "O ativo subiu forte em pouco tempo (IFR acima de 70), mas continua em tendência de baixa "
        "de longo prazo. Altas bruscas dentro de tendências de baixa costumam perder força.",
    "Rompimento de resistência | Alta":
        "O preço fechou acima de um nível onde antes encontrava vendedores, com volume alto. "
        "Rompimentos com volume indicam convicção dos compradores.",
    "Perda de suporte | Baixa":
        "O preço fechou abaixo de um nível onde antes encontrava compradores, com volume alto. "
        "Perdas de suporte com volume indicam convicção dos vendedores.",
    "Testando suporte | Alta":
        "O preço chegou a um nível onde, no passado, os compradores apareceram várias vezes, "
        "e fechou acima dele. É o suporte sendo defendido.",
    "Testando resistência | Baixa":
        "O preço chegou a um nível onde, no passado, os vendedores apareceram várias vezes, "
        "e fechou abaixo dele. É a resistência sendo defendida.",
    "Cruzamento MACD | Alta":
        "O MACD estava negativo (o preço vinha caindo) e cruzou para cima da sua linha de "
        "sinal, indicando que a força vendedora está perdendo ímpeto e o momento virou para cima.",
    "Divergência no MACD | Alta":
        "O preço caiu até um fundo mais baixo que o anterior, mas o MACD ficou num nível mais "
        "alto: a queda continuou, só que com cada vez menos força vendedora. É um alerta "
        "antecipado de possível reversão para cima.",
    "Divergência no MACD | Baixa":
        "O preço subiu até um topo mais alto que o anterior, mas o MACD ficou num nível mais "
        "baixo: a alta continuou, só que com cada vez menos força compradora. É um alerta "
        "antecipado de possível reversão para baixo.",
    "Cruzamento MACD | Baixa":
        "O MACD estava positivo (o preço vinha subindo) e cruzou para baixo da sua linha de "
        "sinal, indicando que a força compradora está perdendo ímpeto e o momento virou para baixo.",
}


def _dist(a, b):
    return (a / b - 1) * 100


def explicar_sinal(d, linha, janela_pivo, tolerancia, crit):
    """Monta, com regras objetivas, os motivos de um sinal: o que aconteceu, o contexto
    a favor, os pontos de atenção (fatores contra) e o que diz o histórico."""
    hoje = d.iloc[-1]
    fech = hoje["Close"]
    alta = "Alta" in linha["Direção"]
    lado = "Alta" if alta else "Baixa"
    setups = linha["Setup"].split(" + ")

    porque = [DESCRICOES.get(f"{s} | {lado}", "") for s in setups]
    if len(setups) == 2:
        porque.append("Dois setups diferentes apontaram para o mesmo lado quase ao mesmo tempo "
                      "(confluência), o que tende a tornar o sinal mais consistente.")

    a_favor, atencao = [], []
    referencia = ""

    def classificar(favoravel, texto):
        (a_favor if favoravel else atencao).append(texto)

    # Tendência de longo prazo
    if not pd.isna(hoje["MMA200"]):
        acima = fech > hoje["MMA200"]
        classificar(acima == alta,
                    f"Preço {br(abs(_dist(fech, hoje['MMA200'])), 1)}% "
                    f"{'acima' if acima else 'abaixo'} da MMA 200: tendência de longo prazo de "
                    f"{'alta' if acima else 'baixa'}, {'a favor do' if acima == alta else 'contra o'} sinal.")

    # Curto prazo
    curto_alta = hoje["MME9"] > hoje["MME21"]
    classificar(curto_alta == alta,
                f"MME 9 {'acima' if curto_alta else 'abaixo'} da MME 21: curto prazo "
                f"{'comprador' if curto_alta else 'vendedor'}, "
                f"{'a favor do' if curto_alta == alta else 'contra o'} sinal.")

    # IFR
    ifr = hoje["IFR"]
    if alta and ifr > 70:
        atencao.append(f"IFR em {br(ifr, 1)}: o ativo já está sobrecomprado, com menos fôlego para subir.")
    elif not alta and ifr < 30:
        atencao.append(f"IFR em {br(ifr, 1)}: o ativo já está sobrevendido e sujeito a repiques.")
    else:
        estado = "sobrevendido" if ifr < 30 else ("sobrecomprado" if ifr > 70 else "em zona neutra")
        a_favor.append(f"IFR em {br(ifr, 1)} ({estado}), sem excesso contra a direção do sinal.")

    # Momento pelo MACD (histograma)
    hist = hoje["MACD_Hist"]
    if not pd.isna(hist):
        positivo = hist > 0
        classificar(positivo == alta,
                    f"Histograma do MACD {'positivo' if positivo else 'negativo'}: momento "
                    f"{'comprador' if positivo else 'vendedor'}, "
                    f"{'a favor do' if positivo == alta else 'contra o'} sinal.")

    # Volume
    vol_media = hoje["VolMedia20"]
    if vol_media and vol_media > 0:
        forca = hoje["Volume"] / vol_media
        if forca >= 1.5:
            a_favor.append(f"Volume {br(forca, 1)}x a média de 20 dias: movimento com participação forte.")
        elif forca < 0.7:
            atencao.append(f"Volume de apenas {br(forca, 1)}x a média de 20 dias: pouca "
                           "participação, o que enfraquece o sinal.")

    # Espaço até o próximo obstáculo e nível de invalidação
    suportes, resistencias = suportes_e_resistencias(d, janela_pivo, tolerancia, 1)
    if alta:
        if resistencias:
            r = resistencias[0][0]
            dist = _dist(r, fech)
            classificar(dist >= 2, f"Próxima resistência em R$ {br(r)}, {br(dist, 1)}% acima: "
                                   f"é onde o preço pode encontrar vendedores"
                                   f"{' (pouco espaço até lá)' if dist < 2 else ''}.")
        else:
            a_favor.append("Nenhuma resistência relevante acima no último ano: o preço está "
                           "perto das máximas, sem obstáculos claros.")
        if suportes:
            s_ = suportes[0][0]
            referencia = (f"Se o preço perder o suporte em R$ {br(s_)} "
                          f"({br(abs(_dist(s_, fech)), 1)}% abaixo), o cenário do sinal perde força.")
    else:
        if suportes:
            s_ = suportes[0][0]
            dist = abs(_dist(s_, fech))
            classificar(dist >= 2, f"Próximo suporte em R$ {br(s_)}, {br(dist, 1)}% abaixo: "
                                   f"é onde o preço pode encontrar compradores"
                                   f"{' (pouco espaço até lá)' if dist < 2 else ''}.")
        else:
            a_favor.append("Nenhum suporte relevante abaixo no último ano: o preço está perto "
                           "das mínimas, sem pisos claros.")
        if resistencias:
            r = resistencias[0][0]
            referencia = (f"Se o preço superar a resistência em R$ {br(r)} "
                          f"({br(_dist(r, fech), 1)}% acima), o cenário do sinal perde força.")

    # O que diz o histórico
    if pd.isna(linha.get("Amostra")):
        historico = "Não há histórico suficiente deste sinal no último ano para avaliá-lo."
    else:
        tipo = "esta combinação" if len(setups) == 2 else "este setup"
        historico = (
            f"No último ano, {tipo} gerou {int(linha['Amostra'])} sinais já encerrados. "
            f"Em {br(linha['Taxa de acerto'], 1)}% deles o preço foi na direção indicada "
            f"após {crit['horizonte']} pregões, contra {br(linha['Acaso'], 1)}% em um dia "
            f"qualquer: uma vantagem de {br(linha['Vantagem'], 1)} pontos percentuais. "
            f"Quando acertou, o preço andou em média {br(linha['Média nos acertos'], 2)}% a favor; "
            f"quando errou, {br(abs(linha['Média nos erros']), 2)}% contra.")

    return {
        "titulo": f"{linha['Ativo']} | {linha['Direção']} | {linha['Setup']}",
        "o_que": linha["Detalhe"], "porque": [p for p in porque if p],
        "a_favor": a_favor, "atencao": atencao, "referencia": referencia,
        "historico": historico,
    }


def explicacao_markdown(e):
    texto = f"**O que aconteceu:** {e['o_que']}.\n\n**Por que chamou atenção:** " + " ".join(e["porque"])
    if e["a_favor"]:
        texto += "\n\n**Contexto a favor:**\n" + "\n".join(f"- {x}" for x in e["a_favor"])
    if e["atencao"]:
        texto += "\n\n**Pontos de atenção:**\n" + "\n".join(f"- {x}" for x in e["atencao"])
    if e["referencia"]:
        texto += f"\n\n**Nível de referência:** {e['referencia']}"
    texto += f"\n\n**O que diz o histórico:** {e['historico']}"
    return texto
