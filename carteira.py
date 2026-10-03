"""Carteira Simulada: executa, com dinheiro fictício, todas as compras em confluências de alta.

Regras combinadas:
- Entra: toda confluência de alta (2 ou mais setups de alta diferentes em até 3 pregões).
- Compra: R$ 1.000 fictícios, na abertura do pregão seguinte ao sinal.
- Stop loss: o suporte mais próximo abaixo do preço de compra.
- Stop gain: a primeira resistência que esteja a pelo menos 2x o risco acima da compra
  (risco/retorno mínimo de 2 para 1). Se nenhuma resistência atender, o alvo é 2x o risco.
- Sem prazo: a operação só termina no stop ou no alvo.
- Ativo já em carteira: novos sinais dele são ignorados.
- Gap: se abrir além do stop ou do alvo, a saída é no preço de abertura.
- Stop e alvo no mesmo dia: considera o stop (hipótese conservadora).
- Custo: 0,10% por operação (ida e volta).

Roda todo dia útil pelo GitHub Actions, antes do alerta. Os resultados ficam na pasta carteira/.
"""
import os
import json
import itertools
import pandas as pd
import motor

DATA_INICIO = "2026-10-05"   # primeiro pregão em que os sinais são considerados
VALOR = 1000.0
CUSTO = 0.10                 # % por operação
RISCO_RETORNO = 2.0          # o alvo precisa estar a pelo menos 2x a distância do stop

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carteira")
ARQ_OPERACOES = os.path.join(PASTA, "operacoes.csv")
ARQ_ESTADO = os.path.join(PASTA, "estado.json")
COLUNAS = ["id", "ativo", "setups", "bem_avaliado", "data_sinal", "status",
           "data_entrada", "preco_entrada", "quantidade", "stop", "alvo", "tipo_alvo",
           "data_saida", "preco_saida", "motivo_saida", "resultado_pct", "resultado_rs", "niveis"]


def carregar():
    """Lê as operações como uma lista de dicionários (um por operação)."""
    ops = []
    if os.path.exists(ARQ_OPERACOES):
        tabela = pd.read_csv(ARQ_OPERACOES, dtype=str, keep_default_na=False)
        ops = tabela.to_dict("records")
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


def todos_os_niveis(d, ate, crit):
    """Suportes e resistências (2+ toques) conhecidos até o dia do sinal."""
    sup, res = motor.suportes_e_resistencias(d.loc[:ate], crit["janela_pivo"],
                                             crit["tolerancia"], 50)
    return sorted(round(float(n), 2) for n, _ in sup + res)


def foi_bem_avaliado(setups, resumo_comb, crit):
    for x, y in itertools.combinations(sorted(setups), 2):
        hist = motor.avaliar_historico(resumo_comb, f"{x} + {y}", "🟢 Alta",
                                       crit["amostra_combinacao"], crit)
        if hist and hist["Aprovado"]:
            return True
    return False


def encerrar(op, dia, preco, motivo, eventos):
    entrada = float(op["preco_entrada"])
    pct = (preco / entrada - 1) * 100 - CUSTO
    op.update({"status": "Encerrada", "data_saida": dia, "preco_saida": round(preco, 2),
               "motivo_saida": motivo, "resultado_pct": round(pct, 2),
               "resultado_rs": round(VALOR * pct / 100, 2)})
    eventos["encerradas"].append({"ativo": op["ativo"], "motivo": motivo,
                                  "resultado_pct": round(pct, 2),
                                  "resultado_rs": round(VALOR * pct / 100, 2)})


def processar_dia(ops, todos, arrays, dia, resumo_comb, crit, eventos):
    texto_dia = dia.strftime("%Y-%m-%d")

    # 1) Ordens pendentes: compra na abertura de hoje
    for op in ops:
        if op["status"] != "Pendente":
            continue
        d = todos.get(op["ativo"])
        if d is None or dia not in d.index:
            continue
        entrada = round(float(d.loc[dia, "Open"]), 2)
        niveis = json.loads(op["niveis"])
        abaixo = [n for n in niveis if n < entrada]
        acima = [n for n in niveis if n > entrada]
        if not abaixo:
            op.update({"status": "Cancelada", "motivo_saida": "Sem suporte abaixo para o stop"})
            continue
        stop = max(abaixo)
        risco = entrada - stop
        alvo_minimo = entrada + RISCO_RETORNO * risco
        candidatas = [n for n in acima if n >= alvo_minimo]   # pula resistências perto demais
        if candidatas:
            alvo, tipo = min(candidatas), "Resistência"
        else:
            alvo, tipo = round(alvo_minimo, 2), "2x o risco"
        op.update({"status": "Aberta", "data_entrada": texto_dia, "preco_entrada": entrada,
                   "quantidade": round(VALOR / entrada, 4), "stop": stop, "alvo": alvo,
                   "tipo_alvo": tipo})
        eventos["compradas"].append(op["ativo"])

    # 2) Operações abertas: confere stop e alvo com o candle de hoje
    for op in ops:
        if op["status"] != "Aberta":
            continue
        d = todos.get(op["ativo"])
        if d is None or dia not in d.index:
            continue
        abertura, maxima, minima = (float(d.loc[dia, c]) for c in ("Open", "High", "Low"))
        stop, alvo = float(op["stop"]), float(op["alvo"])
        if abertura <= stop:
            encerrar(op, texto_dia, abertura, "Stop (abriu abaixo)", eventos)
        elif minima <= stop:
            encerrar(op, texto_dia, stop, "Stop", eventos)
        elif abertura >= alvo:
            encerrar(op, texto_dia, abertura, "Alvo (abriu acima)", eventos)
        elif maxima >= alvo:
            encerrar(op, texto_dia, alvo, "Alvo", eventos)

    # 3) Novos sinais de hoje viram ordens para a abertura do próximo pregão
    em_carteira = {op["ativo"] for op in ops if op["status"] in ("Pendente", "Aberta")}
    for ativo, d in todos.items():
        if ativo in em_carteira or dia not in d.index:
            continue
        i = d.index.get_loc(dia)
        setups = motor.setups_alta_recentes(arrays[ativo], i, crit["janela_pivo"],
                                            crit["tolerancia"], crit["janela_combinacao"])
        if not setups:
            continue
        nova = {c: "" for c in COLUNAS}
        nova.update({"id": f"{texto_dia}-{ativo}", "ativo": ativo, "setups": " + ".join(setups),
                     "bem_avaliado": foi_bem_avaliado(setups, resumo_comb, crit),
                     "data_sinal": texto_dia, "status": "Pendente",
                     "niveis": json.dumps(todos_os_niveis(d, dia, crit))})
        ops.append(nova)
        eventos["novos_sinais"].append({"ativo": ativo, "setups": nova["setups"],
                                        "bem_avaliado": nova["bem_avaliado"]})


def main():
    crit = motor.CRITERIOS
    ops, estado = carregar()
    estado.setdefault("inicio", DATA_INICIO)
    ultimo = pd.Timestamp(estado.get("ultimo_dia", "1900-01-01"))
    inicio = pd.Timestamp(estado["inicio"])

    print("Baixando dados...")
    todos = motor.baixar_universo(tuple(motor.UNIVERSO_PADRAO))
    if not todos:
        print("Sem dados; nada a fazer hoje.")
        salvar(ops, estado)
        return

    datas = sorted({x for d in todos.values() for x in d.index})
    dias = [x for x in datas if x > ultimo and x >= inicio]
    eventos = {"compradas": [], "encerradas": [], "novos_sinais": []}

    if dias:
        print(f"Processando {len(dias)} pregão(ões). Consultando o Historiador...")
        sinais, base = motor.backtest(todos, crit["janela_pivo"], crit["tolerancia"],
                                      crit["horizonte"])
        resumo_comb = motor.resumir(motor.combinar(sinais, crit["janela_combinacao"]), base)
        arrays = {t: motor.preparar_arrays(d, crit["janela_pivo"]) for t, d in todos.items()}
        for dia in dias:
            processar_dia(ops, todos, arrays, dia, resumo_comb, crit, eventos)
            estado["ultimo_dia"] = dia.strftime("%Y-%m-%d")
    else:
        print(f"Nenhum pregão novo a processar (início do simulado: {estado['inicio']}).")

    estado["eventos_do_dia"] = eventos
    estado["atualizado_em"] = pd.Timestamp.now(tz="America/Sao_Paulo").strftime("%d/%m/%Y %H:%M")
    salvar(ops, estado)
    print(f"Compras executadas: {eventos['compradas']}")
    print(f"Encerradas: {eventos['encerradas']}")
    print(f"Novas ordens para o próximo pregão: {eventos['novos_sinais']}")


if __name__ == "__main__":
    main()
