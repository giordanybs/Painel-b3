"""Carteira Simulada — versão 2.

Roda em paralelo com a versão 1 (carteira.py). O mesmo motor serve para o robô diário
e para o teste no passado, então as duas coisas seguem exatamente as mesmas regras.

Regras da versão 2:
- Entra: confluência de alta, ou seja, 2 ou mais setups de alta INDEPENDENTES no mesmo
  ativo em até 3 pregões (Cruzamento MME 9/21 e Cruzamento MACD contam como um só).
- Compra: R$ 1.000 fictícios, em quantidade inteira de ações calculada pelo fechamento do
  dia do sinal, executada na abertura do pregão seguinte.
- Limites: no máximo 3 compras por dia e 20 vendas por mês. Para garantir as 20 vendas,
  uma nova compra só entra se (vendas já feitas no mês + operações em aberto) < 20.
  Quando há mais sinais do que vagas, entram os de maior força (setups independentes)
  e, no empate, os de melhor relação alvo/risco.
- Stop: o suporte mais alto que esteja a pelo menos 1,5x o ATR de 14 dias abaixo do preço.
  Se esse suporte estiver a mais de 3x o ATR ou a mais de 8% do preço, a operação não entra.
- Alvo: a primeira resistência a pelo menos 2x o risco; sem resistência, exatamente 2x o risco.
- Sem venda no dia da compra: se o stop ou o alvo for atingido no próprio dia da compra,
  a venda é feita na abertura do pregão seguinte.
- Gap: se abrir além do stop ou do alvo, a saída é no preço de abertura.
- Stop e alvo no mesmo dia: considera o stop (hipótese conservadora).
- Custo: 0,10% por operação. Proventos não são considerados.

Os resultados ficam em carteira/v2/.
"""
import os
import json
import math
import numpy as np
import pandas as pd
import motor
import carteira as v1   # reaproveita a checagem de candles incompletos

DATA_INICIO = "2026-11-05"   # primeiro pregão cujos sinais a versão 2 considera ao vivo
VALOR = 1000.0
CUSTO = 0.10                 # % por operação

REGRAS_V2 = {
    "nome": "Versão 2",
    "primas_juntas": True,
    "limite_compras_dia": 3,
    "limite_vendas_mes": 20,
    "stop_atr_min": 1.5,
    "stop_atr_max": 3.0,
    "stop_pct_max": 8.0,
    "venda_no_dia_da_compra": False,
    "quantidade_inteira": True,
    "risco_retorno": 2.0,
    "janela_confluencia": 3,
}

# A versão 1, reescrita neste motor, para comparar as duas no mesmo período do passado
REGRAS_V1 = {
    "nome": "Versão 1",
    "primas_juntas": False,
    "limite_compras_dia": None,
    "limite_vendas_mes": None,
    "stop_atr_min": None,
    "stop_atr_max": None,
    "stop_pct_max": None,
    "venda_no_dia_da_compra": True,
    "quantidade_inteira": False,
    "risco_retorno": 2.0,
    "janela_confluencia": 3,
}

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carteira", "v2")
ARQ_OPERACOES = os.path.join(PASTA, "operacoes.csv")
ARQ_ESTADO = os.path.join(PASTA, "estado.json")
COLUNAS = ["id", "ativo", "setups", "forca", "data_sinal", "preco_ref", "quantidade",
           "stop_previsto", "alvo_previsto", "atr", "status", "data_entrada", "preco_entrada",
           "stop", "alvo", "tipo_alvo", "risco_pct", "data_saida", "preco_saida",
           "motivo_saida", "resultado_pct", "resultado_rs", "niveis"]
VIVAS = ("Pendente", "Aberta", "Saída programada")


def num(x):
    try:
        v = float(x)
        return v if not math.isnan(v) else None
    except (TypeError, ValueError):
        return None


def texto(dia):
    return pd.Timestamp(dia).strftime("%Y-%m-%d")


# ============ PREPARO (uma passada por ativo) ============
def preparar(todos, janela_pivo, tolerancia):
    """Para cada ativo, guarda os arrays do motor e os setups de alta de cada dia.
    Cada dia só usa dados até ele mesmo, como na Sentinela."""
    prep = {}
    for t, d in todos.items():
        a = motor.preparar_arrays(d, janela_pivo)
        alta = [frozenset()] * len(d)
        for i in range(31, len(d)):
            s = {x for x, dr, _ in motor.setups_no_dia(a, i, janela_pivo, tolerancia)
                 if dr == "🟢 Alta"}
            if s:
                alta[i] = frozenset(s)
        if "ATR14" in d:
            atr = d["ATR14"].to_numpy()
        else:
            atr = np.full(len(d), np.nan)
        prep[t] = {"d": d, "a": a, "alta": alta, "atr": atr,
                   "pos": {x: k for k, x in enumerate(d.index)}}
    return prep


def familia(setup, regras):
    return motor.FAMILIAS.get(setup, setup) if regras["primas_juntas"] else setup


def confluencia(p, i, regras):
    """Setups de alta nos últimos pregões (pelo menos um hoje) e a força: quantos são
    independentes. Devolve (setups, força) ou None."""
    hoje = p["alta"][i]
    if not hoje:
        return None
    setups = set(hoje)
    for k in range(1, regras["janela_confluencia"] + 1):
        if i - k >= 30:
            setups |= p["alta"][i - k]
    forca = len({familia(s, regras) for s in setups})
    return (sorted(setups), forca) if forca >= 2 else None


def niveis(p, i, janela_pivo, tolerancia):
    """Suportes e resistências (2+ toques) dos últimos 250 pregões, só com pivôs já
    confirmados no dia i."""
    a = p["a"]
    ini, fim = max(0, i - 249), i - janela_pivo + 1
    if fim <= ini:
        return []
    precos = np.concatenate([a["piv_topo"][ini:fim], a["piv_fundo"][ini:fim]])
    precos = precos[~np.isnan(precos)].tolist()
    return sorted(round(float(n), 2) for n, c in motor.agrupar_niveis(precos, tolerancia) if c >= 2)


def stop_e_alvo(preco, nv, atr, regras):
    """Calcula stop e alvo para um preço de compra. Devolve (dict, motivo da recusa)."""
    abaixo = [n for n in nv if n < preco]
    if regras["stop_atr_min"] is None:
        if not abaixo:
            return None, "Sem suporte abaixo para o stop"
        stop = max(abaixo)
    else:
        if atr is None or not atr > 0:
            return None, "Sem ATR para calcular o stop"
        validos = [n for n in abaixo if preco - n >= regras["stop_atr_min"] * atr]
        if not validos:
            return None, "Nenhum suporte a pelo menos 1,5x o ATR abaixo do preço"
        stop = max(validos)
        dist = preco - stop
        dist_pct = dist / preco * 100
        if dist > regras["stop_atr_max"] * atr or dist_pct > regras["stop_pct_max"]:
            return None, (f"Stop longe demais ({motor.br(dist_pct, 1)}% abaixo; "
                          f"o máximo é 3x o ATR e 8%)")
    risco = preco - stop
    alvo_minimo = preco + regras["risco_retorno"] * risco
    candidatas = [n for n in nv if n >= alvo_minimo]
    if candidatas:
        alvo, tipo = min(candidatas), "Resistência"
    else:
        alvo, tipo = round(alvo_minimo, 2), "2x o risco"
    return {"stop": round(stop, 2), "alvo": round(alvo, 2), "tipo_alvo": tipo,
            "risco_pct": round(risco / preco * 100, 2)}, ""


# ============ LIVRO DE OPERAÇÕES ============
class Livro:
    def __init__(self, ops=None):
        self.ops = ops or []
        self.vivas = [op for op in self.ops if op["status"] in VIVAS]
        self.vendas_mes = {}
        for op in self.ops:
            if op["status"] == "Encerrada" and op.get("data_saida"):
                m = str(op["data_saida"])[:7]
                self.vendas_mes[m] = self.vendas_mes.get(m, 0) + 1

    def limpar(self):
        self.vivas = [op for op in self.vivas if op["status"] in VIVAS]

    def em_carteira(self):
        return {op["ativo"] for op in self.vivas}


def novos_eventos():
    return {"compradas": [], "encerradas": [], "programadas": [], "novas_ordens": [],
            "fora_do_limite": [], "sem_stop_valido": [], "canceladas": []}


def encerrar(livro, op, dia, preco, motivo, eventos):
    qtd, entrada = float(op["quantidade"]), float(op["preco_entrada"])
    rs = qtd * (preco - entrada) - qtd * entrada * CUSTO / 100
    pct = rs / (qtd * entrada) * 100
    op.update({"status": "Encerrada", "data_saida": texto(dia), "preco_saida": round(preco, 2),
               "motivo_saida": motivo, "resultado_pct": round(pct, 2), "resultado_rs": round(rs, 2)})
    m = texto(dia)[:7]
    livro.vendas_mes[m] = livro.vendas_mes.get(m, 0) + 1
    eventos["encerradas"].append({"ativo": op["ativo"], "dia": texto(dia), "motivo": motivo,
                                  "resultado_pct": round(pct, 2), "resultado_rs": round(rs, 2)})


def candle(p, dia):
    """Abertura, máxima, mínima e fechamento do dia, se o candle estiver completo."""
    if dia not in p["pos"] or not v1.candle_valido(p["d"], dia):
        return None
    i = p["pos"][dia]
    a = p["a"]
    return float(p["d"]["Open"].iloc[i]), float(a["high"][i]), float(a["low"][i]), float(a["close"][i])


def processar_dia(livro, prep, dia, regras, eventos, janela_pivo, tolerancia):
    hoje = texto(dia)

    # 1) Vendas programadas: na abertura de hoje
    for op in list(livro.vivas):
        if op["status"] != "Saída programada":
            continue
        c = candle(prep.get(op["ativo"], {"pos": {}}), dia) if op["ativo"] in prep else None
        if c is None:
            continue
        encerrar(livro, op, dia, c[0], op["motivo_saida"] + " (vendida na abertura seguinte)", eventos)

    # 2) Compras pendentes: na abertura de hoje, com stop e alvo recalculados pela abertura
    for op in list(livro.vivas):
        if op["status"] != "Pendente":
            continue
        p = prep.get(op["ativo"])
        c = candle(p, dia) if p else None
        if c is None:
            continue
        abertura, maxima, minima, _ = c
        calc, motivo = stop_e_alvo(abertura, json.loads(op["niveis"]), num(op["atr"]), regras)
        if calc is None:
            op.update({"status": "Cancelada", "motivo_saida": motivo + " na abertura",
                       "data_saida": hoje})
            eventos["canceladas"].append({"ativo": op["ativo"], "motivo": op["motivo_saida"]})
            continue
        qtd = num(op["quantidade"]) if regras["quantidade_inteira"] else VALOR / abertura
        op.update({"status": "Aberta", "data_entrada": hoje, "preco_entrada": round(abertura, 2),
                   "quantidade": qtd, **calc})
        eventos["compradas"].append({"ativo": op["ativo"], "dia": hoje, "preco": round(abertura, 2),
                                     "quantidade": qtd})
        if not regras["venda_no_dia_da_compra"]:
            if minima <= calc["stop"] or maxima >= calc["alvo"]:
                motivo = ("Stop atingido no dia da compra" if minima <= calc["stop"]
                          else "Alvo atingido no dia da compra")
                op.update({"status": "Saída programada", "motivo_saida": motivo})
                eventos["programadas"].append({"ativo": op["ativo"], "motivo": motivo})

    # 3) Operações abertas: stop e alvo com o candle de hoje
    for op in list(livro.vivas):
        if op["status"] != "Aberta":
            continue
        p = prep.get(op["ativo"])
        c = candle(p, dia) if p else None
        if c is None:
            continue
        abertura, maxima, minima, _ = c
        stop, alvo = float(op["stop"]), float(op["alvo"])
        if abertura <= stop:
            encerrar(livro, op, dia, abertura, "Stop (abriu abaixo)", eventos)
        elif minima <= stop:
            encerrar(livro, op, dia, stop, "Stop", eventos)
        elif abertura >= alvo:
            encerrar(livro, op, dia, abertura, "Alvo (abriu acima)", eventos)
        elif maxima >= alvo:
            encerrar(livro, op, dia, alvo, "Alvo", eventos)
    livro.limpar()

    # 4) Novos sinais: viram ordens para a abertura do próximo pregão
    ocupados = livro.em_carteira()
    candidatos = []
    for t, p in prep.items():
        if t in ocupados or dia not in p["pos"]:
            continue
        i = p["pos"][dia]
        conf = confluencia(p, i, regras)
        if conf is None or candle(p, dia) is None:
            continue
        setups, forca = conf
        ref = float(p["a"]["close"][i])
        nv = niveis(p, i, janela_pivo, tolerancia)
        atr = float(p["atr"][i]) if not np.isnan(p["atr"][i]) else None
        prev, motivo = stop_e_alvo(ref, nv, atr, regras)
        if prev is None and regras["stop_atr_min"] is not None:
            eventos["sem_stop_valido"].append({"ativo": t, "setups": " + ".join(setups),
                                               "motivo": motivo})
            continue
        qtd = math.floor(VALOR / ref) if regras["quantidade_inteira"] else ""
        if regras["quantidade_inteira"] and qtd < 1:
            continue
        rr = ((prev["alvo"] - ref) / (ref - prev["stop"])) if prev else 0.0
        candidatos.append({"ativo": t, "setups": setups, "forca": forca, "ref": ref, "nv": nv,
                           "atr": atr, "prev": prev, "qtd": qtd, "rr": rr})

    candidatos.sort(key=lambda c: (-c["forca"], -c["rr"], c["ativo"]))
    compras_hoje = 0
    mes = hoje[:7]
    for c in candidatos:
        nome = " + ".join(c["setups"])
        lim_dia, lim_mes = regras["limite_compras_dia"], regras["limite_vendas_mes"]
        if lim_dia is not None and compras_hoje >= lim_dia:
            eventos["fora_do_limite"].append({"ativo": c["ativo"], "setups": nome,
                                              "motivo": f"Limite de {lim_dia} compras por dia"})
            continue
        if lim_mes is not None and livro.vendas_mes.get(mes, 0) + len(livro.vivas) >= lim_mes:
            eventos["fora_do_limite"].append({"ativo": c["ativo"], "setups": nome,
                                              "motivo": f"Limite de {lim_mes} vendas no mês"})
            continue
        prev = c["prev"] or {}
        op = {k: "" for k in COLUNAS}
        op.update({"id": f"{hoje}-{c['ativo']}", "ativo": c["ativo"], "setups": nome,
                   "forca": c["forca"], "data_sinal": hoje, "preco_ref": round(c["ref"], 2),
                   "quantidade": c["qtd"], "stop_previsto": prev.get("stop", ""),
                   "alvo_previsto": prev.get("alvo", ""),
                   "atr": round(c["atr"], 4) if c["atr"] else "", "status": "Pendente",
                   "niveis": json.dumps(c["nv"])})
        livro.ops.append(op)
        livro.vivas.append(op)
        compras_hoje += 1
        eventos["novas_ordens"].append({"ativo": c["ativo"], "setups": nome, "forca": c["forca"]})


# ============ TESTE NO PASSADO ============
def simular_passado(todos, regras, inicio=None, fim=None, prep=None):
    """Roda as regras sobre o histórico, dia a dia, sem olhar o futuro."""
    crit = motor.CRITERIOS
    jp, tol = crit["janela_pivo"], crit["tolerancia"]
    if prep is None:
        prep = preparar(todos, jp, tol)
    datas = sorted({x for d in todos.values() for x in d.index})
    if inicio is None:
        inicio = datas[min(260, len(datas) - 1)]   # um ano de aquecimento (MMA 200, níveis)
    datas = [x for x in datas if x >= pd.Timestamp(inicio) and (fim is None or x <= pd.Timestamp(fim))]
    livro = Livro()
    eventos = novos_eventos()
    for dia in datas:
        processar_dia(livro, prep, dia, regras, eventos, jp, tol)
    tabela = pd.DataFrame(livro.ops, columns=COLUNAS)
    return tabela, eventos, (datas[0], datas[-1]) if datas else (None, None)


# ============ ROBÔ DIÁRIO ============
def carregar():
    ops = []
    if os.path.exists(ARQ_OPERACOES):
        ops = pd.read_csv(ARQ_OPERACOES, dtype=str, keep_default_na=False).to_dict("records")
    estado = {}
    if os.path.exists(ARQ_ESTADO):
        with open(ARQ_ESTADO, encoding="utf-8") as f:
            estado = json.load(f)
    return ops, estado


def salvar(ops, estado):
    os.makedirs(PASTA, exist_ok=True)
    pd.DataFrame(ops, columns=COLUNAS).to_csv(ARQ_OPERACOES, index=False)
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def diagnostico(todos, dia):
    """Mostra no registro do GitHub por que o pregão ainda não está completo."""
    problemas = []
    for t, d in todos.items():
        if dia in d.index and not v1.candle_valido(d, dia):
            campos = [c for c in ("Open", "High", "Low", "Close")
                      if not (pd.notna(d.loc[dia, c]) and d.loc[dia, c] > 0)]
            problemas.append(f"{t}({','.join(campos)})")
    print(f"  {len(problemas)} ativos com candle incompleto em {dia:%d/%m}: {', '.join(problemas[:15])}")


def main():
    crit = motor.CRITERIOS
    jp, tol = crit["janela_pivo"], crit["tolerancia"]
    ops, estado = carregar()
    estado.setdefault("inicio", DATA_INICIO)
    ultimo = pd.Timestamp(estado.get("ultimo_dia", "1900-01-01"))
    inicio = pd.Timestamp(estado["inicio"])

    print("Versão 2: baixando dados...")
    todos = motor.baixar_universo(tuple(motor.UNIVERSO_PADRAO))
    eventos = novos_eventos()
    if todos:
        datas = sorted({x for d in todos.values() for x in d.index})
        dias = [x for x in datas if x > ultimo and x >= inicio]
        if dias:
            prep = preparar(todos, jp, tol)
            livro = Livro(ops)
            for dia in dias:
                if not v1.dia_completo(todos, dia):
                    print(f"Candles de {dia:%d/%m/%Y} ainda incompletos; ficam para a próxima execução.")
                    diagnostico(todos, dia)
                    break
                processar_dia(livro, prep, dia, REGRAS_V2, eventos, jp, tol)
                estado["ultimo_dia"] = texto(dia)
                print(f"Processado {dia:%d/%m/%Y}.")
            ops = livro.ops
        else:
            print(f"Nenhum pregão novo (início da versão 2: {estado['inicio']}).")
    else:
        print("Sem dados; nada a fazer agora.")

    if estado.get("ultimo_dia") != texto(ultimo) or "eventos" not in estado:
        estado["eventos"] = eventos   # só troca o resumo quando um pregão novo foi processado
    estado["atualizado_em"] = pd.Timestamp.now(tz="America/Sao_Paulo").strftime("%d/%m/%Y %H:%M")
    salvar(ops, estado)
    for k, v in eventos.items():
        if v:
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
