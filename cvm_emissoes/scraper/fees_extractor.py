"""
Extrai informações de fees (comissões) do Prospecto Definitivo da CVM.

Fonte obrigatória: Prospecto Definitivo da oferta (SRE/CVM).
Regra de fallback: campo fica None se não localizado claramente.
Nunca infere, estima ou assume valores implícitos.
"""

import re
from typing import Optional


# Públicos-alvo elegíveis para extração de fees
PUBLICOS_COM_FEE = {"Investidor Qualificado", "Investidor Geral"}

# Nomes de documento aceitos como Prospecto Definitivo
_NOMES_PROSPECTO_DEFINITIVO = {
    "Prospecto Definitivo",
    "Prospecto Definitivo da Oferta",
}


def documento_e_prospecto_definitivo(nome_doc: str) -> bool:
    """Retorna True se o nome do documento corresponde a um Prospecto Definitivo."""
    nome = nome_doc.strip()
    return any(p.lower() in nome.lower() for p in _NOMES_PROSPECTO_DEFINITIVO) and \
           "preliminar" not in nome.lower()


def publico_elegivel_para_fees(publico_alvo: Optional[str]) -> bool:
    """Retorna True se o público-alvo da oferta qualifica para extração de fees."""
    if not publico_alvo:
        return False
    return publico_alvo.strip() in PUBLICOS_COM_FEE


def extrair_fees_prospecto(texto_pdf: str) -> dict:
    """
    Extrai fees do texto do Prospecto Definitivo.

    Campos retornados:
      fee_flat              — Comissão de estruturação + garantia firme
      fee_canal_distribuicao — Fee Canal / Distribuição
      fee_canal_flat        — Fee Canal Flat
      fee_sucesso           — Fee de Sucesso

    Retorna None em cada campo quando não localizado claramente no texto.
    Nunca infere, estima ou assume valores.
    """
    result = {
        "fee_flat": None,
        "fee_canal_distribuicao": None,
        "fee_canal_flat": None,
        "fee_sucesso": None,
    }

    if not texto_pdf:
        return result

    # Normaliza aspas tipográficas e prepara o texto
    texto = (texto_pdf
             .replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))

    # Tenta isolar a seção de remuneração/comissões para reduzir falsos positivos
    secao = _extrair_secao_remuneracao(texto) or texto

    result["fee_flat"] = _extrair_fee_flat(secao)
    result["fee_canal_distribuicao"] = _extrair_fee_canal_distribuicao(secao)
    result["fee_canal_flat"] = _extrair_fee_canal_flat(secao)
    result["fee_sucesso"] = _extrair_fee_sucesso(secao)

    return result


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _extrair_secao_remuneracao(texto: str) -> Optional[str]:
    """
    Isola a seção do prospecto que descreve a remuneração dos coordenadores.
    Retorna None se não encontrar seção identificável.
    """
    # Marcadores comuns de início de seção de remuneração
    inicio_patterns = [
        r"remunera[cç][aã]o\s+dos\s+coordenadores",
        r"comiss[aõ][oe]s?\s+de\s+distribui[cç][aã]o",
        r"remunera[cç][aã]o\s+da\s+distribui[cç][aã]o",
        r"remunerando\s+os\s+coordenadores",
    ]
    # Marcadores de próxima seção (fim da seção de fees)
    proximo_secao = r"\n[A-Z][A-ZÁÀÃÉÍÓÚÂÊÔÇ\s]{10,}\n"

    for pat in inicio_patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            trecho = texto[m.start():]
            # Corta na próxima seção grande (título em maiúsculas)
            fim = re.search(proximo_secao, trecho[100:])
            if fim:
                return trecho[:100 + fim.start() + 2000]
            return trecho[:3000]

    return None


def _pct_proximo(texto: str, termo: str, janela: int = 300) -> Optional[str]:
    """
    Encontra percentual mais próximo de um termo-chave dentro de uma janela de caracteres.
    Retorna a string do percentual (ex: "0,50%") ou None.
    """
    m = re.search(termo, texto, re.IGNORECASE)
    if not m:
        return None

    # Busca percentual na janela após o termo
    trecho_pos = texto[m.start():m.start() + janela]
    pct = re.search(
        r"(\d+(?:[,.]\d+)?)\s*%\s*(?:a\.a\.?|ao\s+ano)?",
        trecho_pos,
        re.IGNORECASE,
    )
    if pct:
        return pct.group(0).strip().rstrip(".")

    # Busca também antes do termo (formato: "X% de comissão de estruturação")
    trecho_pre = texto[max(0, m.start() - 100):m.start()]
    pct = re.search(r"(\d+(?:[,.]\d+)?)\s*%", trecho_pre)
    if pct:
        return pct.group(0).strip()

    return None


def _extrair_fee_flat(texto: str) -> Optional[str]:
    """
    Fee Flat = comissão de estruturação + garantia firme.

    Tenta primeiro encontrar um "fee flat" explícito.
    Se não, busca estruturação e garantia firme separadamente e combina.
    """
    # 1. Fee flat explícito
    pct = _pct_proximo(texto, r"fee\s+flat")
    if pct:
        return pct

    # 2. Estruturação e garantia firme separados
    pct_estr = _pct_proximo(texto, r"estrutura[cç][aã]o")
    pct_gf = _pct_proximo(texto, r"garantia\s+firme")

    if pct_estr and pct_gf:
        return f"{pct_estr} + {pct_gf}"
    if pct_estr:
        return pct_estr
    if pct_gf:
        return pct_gf

    # 3. "comissão de coordenação e estruturação"
    pct = _pct_proximo(texto, r"coordena[cç][aã]o\s+e\s+estrutura[cç][aã]o")
    return pct


def _extrair_fee_canal_distribuicao(texto: str) -> Optional[str]:
    """Fee Canal / Distribuição."""
    for termo in [
        r"comiss[aã]o\s+de\s+distribui[cç][aã]o",
        r"fee\s+de\s+(?:canal|distribui[cç][aã]o)",
        r"remunera[cç][aã]o\s+de\s+distribui[cç][aã]o",
    ]:
        pct = _pct_proximo(texto, termo)
        if pct:
            return pct
    return None


def _extrair_fee_canal_flat(texto: str) -> Optional[str]:
    """Fee Canal Flat."""
    for termo in [
        r"fee\s+canal\s+flat",
        r"canal\s+flat",
        r"comiss[aã]o\s+flat\s+(?:de\s+)?(?:canal|distribui[cç][aã]o)",
    ]:
        pct = _pct_proximo(texto, termo)
        if pct:
            return pct
    return None


def _extrair_fee_sucesso(texto: str) -> Optional[str]:
    """Fee de Sucesso."""
    for termo in [
        r"success\s+fee",
        r"fee\s+de\s+sucesso",
        r"comiss[aã]o\s+de\s+sucesso",
        r"premia[cç][aã]o\s+de\s+sucesso",
    ]:
        pct = _pct_proximo(texto, termo)
        if pct:
            return pct
    return None
