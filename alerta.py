"""Alerta diário: roda a Sentinela, consulta o Historiador e envia por e-mail
os sinais de hoje que foram bem avaliados no último ano.

Executado automaticamente pelo GitHub Actions (arquivo .github/workflows/alerta.yml).
As credenciais vêm dos "Secrets" do repositório, nunca ficam escritas no código.
"""
import os
import sys
import smtplib
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
import motor

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


def montar_email(aprovados, outros, todos, data_pregao, crit):
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
      <h2 style="margin-bottom:4px">🛰️ Sentinela: sinais bem avaliados</h2>
      <p style="color:#666;margin-top:0">Pregão de {data_pregao.strftime('%d/%m/%Y')}</p>
      {corpo}
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


def main():
    crit = motor.CRITERIOS
    forcar = os.environ.get("FORCAR_ENVIO", "false").lower() == "true"

    print("Baixando dados...")
    todos = motor.baixar_universo(tuple(motor.UNIVERSO_PADRAO))
    if not todos:
        print("Não foi possível baixar os dados.")
        sys.exit(1)

    ultima = max(df.index[-1] for df in todos.values())
    hoje_brasil = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    if ultima.date() != hoje_brasil and not forcar:
        print(f"Último pregão nos dados: {ultima.date()}. Hoje não houve pregão (feriado?).")
        return

    print(f"{len(todos)} ativos. Rodando o Historiador...")
    sinais, base = motor.backtest(todos, crit["janela_pivo"], crit["tolerancia"], crit["horizonte"])
    resumo_ind = motor.resumir(sinais, base)
    resumo_comb = motor.resumir(motor.combinar(sinais, crit["janela_combinacao"]), base)

    hoje = motor.classificar_sinais_de_hoje(todos, resumo_ind, resumo_comb,
                                            crit["janela_pivo"], crit["tolerancia"], crit)
    aprovados = hoje[hoje["Aprovado"]].sort_values("Vantagem", ascending=False)
    outros = hoje[~hoje["Aprovado"]]
    print(f"{len(hoje)} sinais hoje, {len(aprovados)} bem avaliados.")
    for _, r in hoje.iterrows():   # registro completo no log do GitHub Actions
        print(f"  {r['Ativo']:7} {r['Direção']} {r['Setup']}: {r['Motivo']}")

    if aprovados.empty and not (forcar or ENVIAR_RESUMO_DIARIO):
        print("Nada para enviar hoje.")
        return

    if aprovados.empty:
        assunto = (f"🛰️ Sentinela {ultima.strftime('%d/%m')}: nenhum sinal aprovado "
                   f"({len(hoje)} observados)")
    else:
        assunto = (f"🛰️ Sentinela {ultima.strftime('%d/%m')}: "
                   f"{len(aprovados)} sinal(is) bem avaliado(s)")
    enviar(assunto, montar_email(aprovados, outros, todos, ultima, crit))
    print("E-mail enviado.")


if __name__ == "__main__":
    main()
