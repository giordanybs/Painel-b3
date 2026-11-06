name: Alerta da Sentinela

on:
  schedule:
    # Várias tentativas na madrugada (horário de Brasília entre parênteses).
    # A primeira que conseguir rodar envia o e-mail; as outras só conferem e saem.
    - cron: "13 6 * * 1-5"    # 03h13
    - cron: "43 7 * * 1-5"    # 04h43
    - cron: "13 9 * * 1-5"    # 06h13
    - cron: "43 10 * * 1-5"   # 07h43
    - cron: "43 11 * * 1-5"   # 08h43
  workflow_dispatch:
    inputs:
      forcar_envio:
        description: "Enviar o e-mail mesmo que já tenha sido enviado (para testar)"
        type: boolean
        default: true

permissions:
  contents: write

concurrency:
  group: robo-b3
  cancel-in-progress: false

jobs:
  alerta:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Conferir se o e-mail de hoje ja foi enviado
        id: conferir
        run: |
          python3 - <<'PY'
          import json, os, datetime as dt
          from zoneinfo import ZoneInfo
          hoje = dt.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
          anterior = hoje - dt.timedelta(days=1)
          while anterior.weekday() >= 5:
              anterior -= dt.timedelta(days=1)
          try:
              enviado = json.load(open("carteira/alerta_estado.json")).get("ultimo_pregao_enviado", "")
          except Exception:
              enviado = ""
          pular = os.environ.get("GITHUB_EVENT_NAME") == "schedule" and enviado >= anterior.isoformat()
          print(f"Último pregão enviado: {enviado or 'nenhum'}; pregão esperado: {anterior}; pular: {pular}")
          with open(os.environ["GITHUB_OUTPUT"], "a") as f:
              f.write(f"pular={'true' if pular else 'false'}\n")
          PY
      - uses: actions/setup-python@v5
        if: steps.conferir.outputs.pular != 'true'
        with:
          python-version: "3.12"
      - run: pip install yfinance pandas numpy
        if: steps.conferir.outputs.pular != 'true'
      - name: Carteira simulada v1
        if: steps.conferir.outputs.pular != 'true'
        run: python carteira.py
        continue-on-error: true
      - name: Carteira simulada v2
        if: steps.conferir.outputs.pular != 'true'
        run: python carteira_v2.py
        continue-on-error: true
      - name: Alerta por e-mail
        if: steps.conferir.outputs.pular != 'true'
        run: python alerta.py
        env:
          EMAIL_USUARIO: ${{ secrets.EMAIL_USUARIO }}
          EMAIL_SENHA_APP: ${{ secrets.EMAIL_SENHA_APP }}
          EMAIL_DESTINO: ${{ secrets.EMAIL_DESTINO }}
          FORCAR_ENVIO: ${{ github.event.inputs.forcar_envio || 'false' }}
      - name: Salvar a carteira no repositorio
        if: always() && steps.conferir.outputs.pular != 'true'
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add carteira
          git diff --cached --quiet || git commit -m "Carteira simulada: atualizacao diaria"
          git pull --rebase --autostash
          git push
