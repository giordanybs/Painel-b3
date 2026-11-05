"""Alerta diário: roda a Sentinela, consulta o Historiador e envia por e-mail as ordens da
Carteira v2 (semiautomação) e os sinais do último pregão.

Roda de madrugada, depois que o Yahoo fecha os candles do dia anterior, e envia um único
e-mail por pregão (a segunda execução da madrugada só envia se a primeira não conseguiu).

Executado automaticamente pelo GitHub Actions (arquivo .github/workflows/alerta.yml).
As credenciais vêm dos "Secrets" do repositório, nunca ficam escritas no código.
"""
import os
import json
import sys
import smtplib
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
import motor
import carteira as v1

# True: envia um resumo todo dia de pregão, mesmo sem sinais aprovados.
# False: só envia quando houver pelo menos um sinal aprovado.
ENVIAR_RESUMO_DIARIO = True


def formatar(v, casas=1, sufixo="", sinal=False):
    if pd.isna(v):
        return "—"
    prefixo = "+" if sinal and v > 0 else ""
    return f"{prefixo}{motor.br(v, casas)}{sufixo}"


def lista_html(itens, cor):
    return "".join(f'<li style="margin-bottom:4px;color:{cor}"><span style="color:#222">{x}</span></li>'
                   for x in itens)


def cartao_html(e, direcao):
    cor = "#1a7f37" if "Alta" in direcao else "#c62828"
    blocos = f"""
      <p style="margin:6px 0"><b>O que aconteceu:</b> {e['o_que']}.</p>
      <p style="margin:6px 0"><b>Por que chamou atenção:</b> {' '.join(e['porque'])}</p>"""
    if e["a_favor"]:
        blocos += f"""<p style="margin:10px 0 2px"><b>Contexto a favor</b></p>
      <ul style="margin:0;padding-left:20px">{lista_html(e['a_favor'], '#1a7f37')}</ul>"""
    if e["atencao"]:
        blocos += f"""<p style="margin:10px 0 2px"><b>Pontos de atenção</b></p>
      <ul style="margin:0;padding-left:20px">{lista_html(e['atencao'], '#b26a00')}</ul>"""
    if e["referencia"]:
        blocos += f"""<p style="margin:10px 0 0"><b>Nível de referência:</b> {e['referencia']}</p>"""
    blocos += f"""<p style="margin:10px 0 0"><b>O que diz o histórico:</b> {e['historico']}</p>"""
    return f"""
    <div style="border:1px solid #e5e7eb;border-left:4px solid {cor};border-radius:6px;
                padding:12px 16px;margin:14px 0">
      <div style="font-size:16px;font-weight:bold;margin-bottom:4px">{e['titulo']}</div>
      {blocos}
    </div>"""


def outros_sinais_html(outros):
    if outros.empty:
        return "<p>Nenhum outro setup apareceu hoje.</p>"
    outros = outros.sort_values("Vantagem", ascending=False, na_position="last").head(15)
    linhas = "".join(
        f"<tr><td style='padding:5px 10px'><b>{r['Ativo']}</b></td>"
        f"<td style='padding:5px 10px'>{r['Direção']}</td>"
        f"<td style='padding:5px 10px'>{r['Setup']}</td>"
        f"<td style='padding:5px 10px;color:#666'>{r['Motivo']}</td></tr>"
        for _, r in outros.iterrows())
    return f"""
        <table style="border-collapse:collapse;font-size:13px">
          <tr style="background:#f3f4f6;text-align:left">
            <th style="padding:5px 10px">Ativo</th><th style="padding:5px 10px">Direção</th>
            <th style="padding:5px 10px">Setup</th>
            <th style="padding:5px 10px">Por que não foi aprovado</th>
          </tr>{linhas}
        </table>"""


PASTA_CARTEIRA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carteira")
ARQ_ALERTA = os.path.join(PASTA_CARTEIRA, "alerta_estado.json")
SP = ZoneInfo("America/Sao_Paulo")
TD = "padding:6px 10px;border-bottom:1px solid #eee"
TH = "padding:6px 10px;text-align:left"


def tabela_html(cabecalho, linhas):
    cab = "".join(f"<th style='{TH}'>{c}</th>" for c in cabecalho)
    corpo = "".join("<tr>" + "".join(f"<td style='{TD}'>{c}</td>" for c in l) + "</tr>"
                    for l in linhas)
    return (f"<table style='border-collapse:collapse;font-size:14px;margin:6px 0 4px'>"
            f"<tr style='background:#f3f4f6'>{cab}</tr>{corpo}</table>")


def reais(v):
    v = pd.to_numeric(v, errors="coerce")
    return "—" if pd.isna(v) else f"R$ {motor.br(v)}"


def quantidade_html(ativo, qtd):
    """Ações: lote padrão de 100 + mercado fracionário (código com F). ETFs: código direto."""
    qtd = int(float(qtd))
    if ativo in motor.ETFS.split():
        return f"<b>{qtd}</b> × {ativo}"
    lote, fracao = divmod(qtd, 100)
    partes = []
    if lote:
        partes.append(f"<b>{lote * 100}</b> × {ativo}")
    if fracao:
        partes.append(f"<b>{fracao}</b> × {ativo}F")
    return " + ".join(partes)


def secao_ordens_v2_html():
    """Semiautomação: as ordens da Carteira v2 prontas para cadastrar na corretora."""
    pasta = os.path.join(PASTA_CARTEIRA, "v2")
    try:
        with open(os.path.join(pasta, "estado.json"), encoding="utf-8") as f:
            estado = json.load(f)
        ops = pd.read_csv(os.path.join(pasta, "operacoes.csv"), dtype=str, keep_default_na=False)
    except Exception:
        return "", {}
    if not estado.get("ultimo_dia"):
        return (f"<h3 style='margin:8px 0 4px'>📋 Ordens da Carteira v2</h3><p>A versão 2 começa "
                f"com os sinais do pregão de {pd.Timestamp(estado.get('inicio')):%d/%m/%Y}.</p>"), {}

    ultimo = pd.Timestamp(estado["ultimo_dia"])
    proximo = ultimo + pd.offsets.BDay(1)
    agora = datetime.now(SP)
    aviso = ""
    if agora.date() > proximo.date():
        aviso = (f"<p style='background:#fdecea;color:#b71c1c;padding:8px 12px;border-radius:6px'>"
                 f"<b>⚠️ Atenção:</b> estas ordens foram geradas com o pregão de {ultimo:%d/%m} e "
                 f"eram para {proximo:%d/%m}, que já passou. Não as execute, a menos que "
                 f"{proximo:%d/%m} tenha sido feriado na B3.</p>")
    elif agora.date() == proximo.date() and agora.hour >= 10:
        aviso = ("<p style='background:#fff4e5;color:#8a5300;padding:8px 12px;border-radius:6px'>"
                 "<b>⚠️ O pregão de hoje já abriu.</b> O simulado usa o preço de abertura; "
                 "avalie se ainda faz sentido executar as ordens agora.</p>")

    pend = ops[ops["status"] == "Pendente"]
    prog = ops[ops["status"] == "Saída programada"]
    abertas = ops[ops["status"] == "Aberta"]
    novas = abertas[abertas["data_entrada"] == estado["ultimo_dia"]]
    antigas = abertas[abertas["data_entrada"] != estado["ultimo_dia"]]
    ev = estado.get("eventos", {})
    enc_dia = [e for e in ev.get("encerradas", []) if e.get("dia") == estado["ultimo_dia"]]
    mes = proximo.strftime("%Y-%m")
    vendas_mes = int(((ops["status"] == "Encerrada") & (ops["data_saida"].str[:7] == mes)).sum())

    partes = [f"<h3 style='margin:8px 0 2px'>📋 Ordens para o pregão de {proximo:%d/%m} "
              f"(Carteira v2)</h3>",
              f"<p style='color:#666;margin-top:0'>Geradas com o fechamento de {ultimo:%d/%m}. "
              f"Vendas em {proximo:%m/%Y}: {vendas_mes} de 20.</p>", aviso]

    if len(pend):
        partes.append("<p style='margin:14px 0 2px'><b>🟢 Comprar na abertura</b> "
                      "(ordem a mercado, enviada antes das 10h)</p>")
        partes.append(tabela_html(
            ["Ativo", "Quantidade", "Preço de referência", "Stop previsto", "Alvo previsto", "Setups"],
            [[f"<b>{r['ativo']}</b>", quantidade_html(r["ativo"], r["quantidade"]),
              reais(r["preco_ref"]), reais(r["stop_previsto"]), reais(r["alvo_previsto"]),
              f"<span style='color:#666;font-size:12px'>{r['setups']}</span>"]
             for _, r in pend.iterrows()]))
        partes.append("<p style='color:#666;font-size:12px;margin-top:0'>O stop e o alvo "
                      "definitivos são calculados pelo preço de abertura e vêm no próximo e-mail. "
                      "Não cadastre stop no dia da compra: a regra é não vender no mesmo dia.</p>")
    if len(prog):
        partes.append("<p style='margin:14px 0 2px'><b>🔴 Vender na abertura</b> "
                      "(ordem a mercado, enviada antes das 10h)</p>")
        partes.append(tabela_html(
            ["Ativo", "Quantidade", "Preço de compra", "Motivo"],
            [[f"<b>{r['ativo']}</b>", quantidade_html(r["ativo"], r["quantidade"]),
              reais(r["preco_entrada"]), r["motivo_saida"]] for _, r in prog.iterrows()]))
    if len(novas):
        partes.append("<p style='margin:14px 0 2px'><b>🛡️ Cadastrar hoje: stop e alvo das compras "
                      "de ontem</b> (ordem de stop e ordem de venda no alvo, ou uma ordem OCO / "
                      "stop duplo, se a plataforma oferecer)</p>")
        partes.append(tabela_html(
            ["Ativo", "Quantidade", "Preço de compra", "Stop", "Alvo"],
            [[f"<b>{r['ativo']}</b>", quantidade_html(r["ativo"], r["quantidade"]),
              reais(r["preco_entrada"]), f"<b>{reais(r['stop'])}</b>", f"<b>{reais(r['alvo'])}</b>"]
             for _, r in novas.iterrows()]))
    if len(antigas):
        lista = ", ".join(f"{r['ativo']} (stop {reais(r['stop'])}, alvo {reais(r['alvo'])})"
                          for _, r in antigas.iterrows())
        partes.append(f"<p style='margin:14px 0 2px'><b>Manter cadastrados:</b> {lista}</p>")
    if enc_dia:
        itens = "".join(
            f"<li><b>{e['ativo']}</b>: {e['motivo']}, "
            f"<span style='color:{'#1a7f37' if e['resultado_rs'] > 0 else '#c62828'}'>"
            f"{formatar(e['resultado_pct'], 2, '%', True)} (R$ {motor.br(e['resultado_rs'])})</span></li>"
            for e in enc_dia)
        partes.append(f"<p style='margin:14px 0 2px'><b>Encerradas em {ultimo:%d/%m}</b> "
                      f"(se o stop ou o alvo estava cadastrado, a corretora já executou)</p>"
                      f"<ul style='margin:0;padding-left:20px'>{itens}</ul>")
    if not (len(pend) or len(prog) or len(novas)):
        partes.append("<p><b>Nenhuma ordem nova para cadastrar.</b></p>")
    fora = ev.get("fora_do_limite", []) + ev.get("sem_stop_valido", [])
    if fora:
        resumo = "; ".join(f"{f['ativo']} ({f['motivo'].lower()})" for f in fora[:10])
        partes.append(f"<p style='color:#666;font-size:12px'>Sinais que não viraram ordem: {resumo}.</p>")
    partes.append("<p style='color:#666;font-size:12px'>Quantidades para R$ 1.000 pelo preço de "
                  "referência. Ações fora de múltiplos de 100 vão para o mercado fracionário "
                  "(código com F); para ETFs, confira o lote na plataforma.</p>")
    contagem = {"compras": len(pend), "vendas": len(prog), "proximo": proximo}
    return "".join(partes), contagem


def secao_carteira_html():
    """Resumo do dia da Carteira Simulada v1 (gerado pelo carteira.py, que roda antes)."""
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carteira")
    try:
        with open(os.path.join(pasta, "estado.json"), encoding="utf-8") as f:
            estado = json.load(f)
        ops = pd.read_csv(os.path.join(pasta, "operacoes.csv"))
    except Exception:
        return ""
    ev = estado.get("eventos_do_dia", {})
    itens = []
    for e in ev.get("encerradas", []):
        cor = "#1a7f37" if e["resultado_rs"] > 0 else "#c62828"
        itens.append(f"Encerrada: <b>{e['ativo']}</b> ({e['motivo']}) "
                     f"<span style='color:{cor}'>{formatar(e['resultado_pct'], 2, '%', True)} | "
                     f"R$ {motor.br(e['resultado_rs'])}</span>")
    for t in ev.get("compradas", []):
        itens.append(f"Comprada hoje na abertura: <b>{t}</b>")
    for n in ev.get("novos_sinais", []):
        marca = " 🏅" if n["bem_avaliado"] else ""
        itens.append(f"Ordem para amanhã: <b>{n['ativo']}</b>{marca} ({n['setups']})")
    if not itens:
        itens.append("Nenhuma movimentação hoje.")
    enc = ops[ops["status"] == "Encerrada"]
    total = pd.to_numeric(enc["resultado_rs"], errors="coerce").sum() if len(enc) else 0
    abertas = (ops["status"] == "Aberta").sum()
    lista = "".join(f"<li style='margin-bottom:4px'>{i}</li>" for i in itens)
    return f"""
        <h3 style="margin:28px 0 6px">💼 Carteira Simulada v1 (comparação)</h3>
        <ul style="margin:0;padding-left:20px">{lista}</ul>
        <p style="color:#666;font-size:13px">{len(enc)} operações encerradas, resultado realizado de
        R$ {motor.br(total)}. {abertas} operação(ões) aberta(s).</p>"""


def montar_email(aprovados, outros, todos, data_pregao, crit, ordens_html=""):
    secao_outros = f"""
        <h3 style="margin:28px 0 6px">Outros sinais de hoje (não aprovados)</h3>
        <p style="color:#666;font-size:13px;margin-top:0">Apareceram na Sentinela, mas não
        passaram nos critérios do Historiador.</p>
        {outros_sinais_html(outros)}"""
    if aprovados.empty:
        corpo = ("<p><b>Nenhum sinal de hoje atingiu os critérios do Historiador.</b></p>"
                 + secao_outros)
    else:
        resumo = "".join(
            f"<tr><td style='padding:6px 10px'><b>{r['Ativo']}</b></td>"
            f"<td style='padding:6px 10px'>{r['Direção']}</td>"
            f"<td style='padding:6px 10px'>{r['Setup']}</td>"
            f"<td style='padding:6px 10px'>R$ {motor.br(r['Fechamento'])}</td>"
            f"<td style='padding:6px 10px'><b>{formatar(r['Vantagem'], 1, ' p.p.', True)}</b></td></tr>"
            for _, r in aprovados.iterrows())
        cartoes = "".join(
            cartao_html(motor.explicar_sinal(todos[r["Ativo"]], r, crit["janela_pivo"],
                                             crit["tolerancia"], crit), r["Direção"])
            for _, r in aprovados.iterrows())
        corpo = f"""
        <h3 style="margin-bottom:6px">Resumo</h3>
        <table style="border-collapse:collapse;font-size:14px">
          <tr style="background:#f3f4f6;text-align:left">
            <th style="padding:6px 10px">Ativo</th><th style="padding:6px 10px">Direção</th>
            <th style="padding:6px 10px">Setup</th><th style="padding:6px 10px">Fechamento</th>
            <th style="padding:6px 10px">Vantagem histórica</th>
          </tr>{resumo}
        </table>
        <h3 style="margin:24px 0 0">Os motivos de cada sinal</h3>
        {cartoes}
        {secao_outros}"""

    return f"""
    <div style="font-family:Arial,sans-serif;color:#222;max-width:760px;line-height:1.45">
      <h2 style="margin-bottom:4px">🛰️ Sentinela</h2>
      <p style="color:#666;margin-top:0">Pregão de {data_pregao.strftime('%d/%m/%Y')}</p>
      {ordens_html}
      <h3 style="margin:28px 0 6px">Sinais bem avaliados pelo Historiador</h3>
      {corpo}
      {secao_carteira_html()}
      <p style="color:#666;font-size:12px;margin-top:24px">
        Critérios: vantagem de pelo menos {motor.br(crit['vantagem_minima'], 0)} p.p. sobre o acaso
        em {crit['horizonte']} pregões, resultado médio positivo e amostra mínima de
        {crit['amostra_individual']} sinais (setup isolado) ou {crit['amostra_combinacao']}
        (combinação), no último ano.<br><br>
        Os sinais descrevem padrões técnicos e seu desempenho passado, que não garante
        resultados futuros. Material educacional, não é recomendação de investimento.
      </p>
    </div>"""


def enviar(assunto, html):
    usuario = os.environ["EMAIL_USUARIO"]
    senha = os.environ["EMAIL_SENHA_APP"]
    destino = os.environ.get("EMAIL_DESTINO") or usuario

    mensagem = MIMEMultipart("alternative")
    mensagem["Subject"] = assunto
    mensagem["From"] = usuario
    mensagem["To"] = destino
    mensagem.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(usuario, senha)
        smtp.sendmail(usuario, destino.split(","), mensagem.as_string())


def dia_de_referencia(todos):
    """Último pregão com os candles completos na maioria dos ativos."""
    datas = sorted({x for d in todos.values() for x in d.index}, reverse=True)
    for dia in datas[:6]:
        if v1.dia_completo(todos, dia):
            return dia
    return None


def ler_estado_alerta():
    try:
        with open(ARQ_ALERTA, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    crit = motor.CRITERIOS
    forcar = os.environ.get("FORCAR_ENVIO", "false").lower() == "true"

    print("Baixando dados...")
    todos = motor.baixar_universo(tuple(motor.UNIVERSO_PADRAO))
    if not todos:
        print("Não foi possível baixar os dados.")
        sys.exit(1)

    dia_ref = dia_de_referencia(todos)
    if dia_ref is None:
        print("Nenhum pregão recente com candles completos. Tento na próxima execução.")
        return
    todos = {t: d.loc[:dia_ref] for t, d in todos.items() if len(d.loc[:dia_ref]) >= 30}
    estado_alerta = ler_estado_alerta()
    if not forcar and estado_alerta.get("ultimo_pregao_enviado", "") >= dia_ref.strftime("%Y-%m-%d"):
        print(f"O e-mail do pregão de {dia_ref:%d/%m/%Y} já foi enviado.")
        return

    print(f"Pregão de referência: {dia_ref:%d/%m/%Y}. {len(todos)} ativos. Rodando o Historiador...")
    sinais, base = motor.backtest(todos, crit["janela_pivo"], crit["tolerancia"], crit["horizonte"])
    resumo_ind = motor.resumir(sinais, base)
    resumo_comb = motor.resumir(motor.combinar(sinais, crit["janela_combinacao"]), base)

    hoje = motor.consolidar_sinais(motor.classificar_sinais_de_hoje(
        todos, resumo_ind, resumo_comb, crit["janela_pivo"], crit["tolerancia"], crit))
    aprovados = hoje[hoje["Aprovado"]].sort_values("Vantagem", ascending=False)
    outros = hoje[~hoje["Aprovado"]]
    print(f"{len(hoje)} sinais, {len(aprovados)} bem avaliados.")
    for _, r in hoje.iterrows():   # registro completo no log do GitHub Actions
        print(f"  {r['Ativo']:7} {r['Direção']} {r['Setup']}: {r['Motivo']}")

    ordens_html, contagem = secao_ordens_v2_html()
    tem_ordens = bool(contagem.get("compras") or contagem.get("vendas"))
    if aprovados.empty and not (forcar or ENVIAR_RESUMO_DIARIO or tem_ordens):
        print("Nada para enviar.")
        return

    if contagem:
        assunto = (f"🛰️ Ordens para {contagem['proximo']:%d/%m}: {contagem['compras']} compra(s), "
                   f"{contagem['vendas']} venda(s) | {len(aprovados)} sinal(is) aprovado(s)")
    else:
        assunto = f"🛰️ Sentinela {dia_ref:%d/%m}: {len(aprovados)} sinal(is) aprovado(s)"
    enviar(assunto, montar_email(aprovados, outros, todos, dia_ref, crit, ordens_html))
    print("E-mail enviado.")
    estado_alerta["ultimo_pregao_enviado"] = dia_ref.strftime("%Y-%m-%d")
    os.makedirs(PASTA_CARTEIRA, exist_ok=True)
    with open(ARQ_ALERTA, "w", encoding="utf-8") as f:
        json.dump(estado_alerta, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
