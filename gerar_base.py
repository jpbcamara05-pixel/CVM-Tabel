"""
Script para gerar base de dados Excel de emissões CVM.
CRI, CRA e Debêntures — dezembro 2025 a 19 de abril de 2026.
"""
import logging
import sys
import time
import os

# Garante imports do projeto
sys.path.insert(0, "/Users/joaopedrocamara/Documents/VS/CVM Tabel/cvm_emissoes")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from scraper.collector import coletar, ResultadoColeta
from exporter.excel import exportar_excel

DATA_INICIO = "01/12/2025"
DATA_FIM    = "19/04/2026"

TIPOS = [
    "Debêntures",
    "Certificados de Recebíveis Imobiliários",
    "Certificados de Recebíveis do Agronegócio",
]

# Nome amigável para log
NOME_CURTO = {
    "Debêntures": "Debêntures",
    "Certificados de Recebíveis Imobiliários": "CRI",
    "Certificados de Recebíveis do Agronegócio": "CRA",
}

resultado_total = ResultadoColeta()
inicio_geral = time.time()

for tipo in TIPOS:
    nome = NOME_CURTO[tipo]
    print(f"\n{'='*60}")
    print(f"Coletando {nome} ({DATA_INICIO} a {DATA_FIM})...")
    print(f"{'='*60}")

    inicio = time.time()
    contadores = {"emissao": 0}

    def prog(etapa, atual, total, msg="", _nome=nome, _ini=inicio):
        elapsed = time.time() - _ini
        if etapa == "detalhes":
            pct = int(atual / max(total, 1) * 100)
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r  [{bar}] {pct:3d}% — {atual}/{total} emissões ({elapsed:.0f}s)", end="", flush=True)
        elif etapa == "filtragem":
            print(f"\r  Filtrando por data de encerramento...{' '*20}", flush=True)
        elif etapa == "concluido":
            print(f"\r  {msg}{' '*20}", flush=True)
        elif etapa == "listagem" and "Paginando" not in (msg or ""):
            print(f"  {msg}", flush=True)

    try:
        r = coletar(
            data_inicio=DATA_INICIO,
            data_fim=DATA_FIM,
            valor_mobiliario_nome=tipo,
            progresso_callback=prog,
        )
        elapsed = time.time() - inicio
        print(f"\n  ✓ {nome}: {r.total_emissoes} emissões, {r.total_series} séries, {len(r.erros)} com erros ({elapsed:.0f}s)")

        resultado_total.registros.extend(r.registros)
        resultado_total.erros.extend(r.erros)
        resultado_total.total_emissoes += r.total_emissoes
        resultado_total.total_series += r.total_series

    except Exception as e:
        print(f"\n  ✗ {nome}: ERRO — {e}")
        import traceback; traceback.print_exc()

# Gerar Excel
output_path = "/Users/joaopedrocamara/Documents/VS/CVM Tabel/emissoes_cvm_dez25_abr26.xlsx"
print(f"\n{'='*60}")
print(f"Total consolidado: {resultado_total.total_emissoes} emissões, {resultado_total.total_series} séries")
print(f"Gerando Excel em: {output_path}")

exportar_excel(
    registros=resultado_total.registros,
    erros=resultado_total.erros,
    output_path=output_path,
)

elapsed_geral = time.time() - inicio_geral
print(f"✓ Excel gerado com sucesso! ({elapsed_geral:.0f}s total)")
