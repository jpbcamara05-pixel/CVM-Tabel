"""
Classificação de setor econômico por CNPJ via BrasilAPI.

Fluxo:
  1. Extrai CNPJ do texto bruto da emissão (infOferta, devedores, nome do emissor)
  2. Consulta BrasilAPI /cnpj para obter CNAE principal + descrição da atividade
  3. Mapeia para a lista fechada de setores permitidos usando descrição (primário)
     e prefixo CNAE (fallback)
  4. Cache thread-safe por CNPJ

Setor = None quando não há mapeamento claro — nunca inferido ou aproximado.

Lista fechada de setores:
  Agronegócio | Proteínas | Energia | Real Estate | Varejo | Saúde |
  Estacionamento | Logística | Gás Natural | Oil & Gas | Porto |
  Saneamento | Rodoviário | Siderurgia | Tech | Telecom | Turismo
"""

import re
import threading
from typing import Optional

import requests

_CNPJ_RE = re.compile(r"\d{2}\.?\d{3}\.?\d{3}[/\\]?\d{4}-?\d{2}")

_cache: dict[str, Optional[str]] = {}
_cache_lock = threading.Lock()
_semaphore = threading.Semaphore(5)


# ---------------------------------------------------------------------------
# Mapeamento por descrição da atividade CNAE (primário — mais preciso)
# Ordem importa: regras mais específicas aparecem antes de mais genéricas.
# ---------------------------------------------------------------------------
_DESCRICAO_REGRAS: list[tuple[list[str], str]] = [
    # Proteínas — abate e processamento de carnes (CNAE 10.1x)
    (["abate", "carne bovina", "carne suína", "frigorif", "avicultura",
      "processamento de carne", "fabricação de produtos de carne"], "Proteínas"),

    # Gás Natural — deve aparecer antes de Energia para não ser absorvido
    (["gás natural", "distribuição de gás", "gasoduto",
      "gás canalizado", "gás liquefeito"], "Gás Natural"),

    # Oil & Gas
    (["petróleo", "extração de petróleo", "refin", "combustível",
      "derivados do petróleo", "petroleum"], "Oil & Gas"),

    # Energia — geração, transmissão, distribuição elétrica
    (["energia elétrica", "geração de energia", "transmissão de energia",
      "distribuição de energia", "energia solar", "energia eólica",
      "energia hidráulica", "usina", "termelétrica", "sucroenerg"], "Energia"),

    # Saneamento
    (["saneamento", "abastecimento de água", "tratamento de esgoto",
      "coleta de resíduo", "limpeza urbana"], "Saneamento"),

    # Real Estate — incorporação, construção de edifícios, shoppings
    (["incorporação", "construção de edifício", "loteamento",
      "empreendimento imobiliário", "shopping"], "Real Estate"),

    # Rodoviário — rodovias e concessões (não logística genérica)
    (["concessão de rodovia", "exploração de rodovia",
      "transporte rodoviário coletivo", "pedágio"], "Rodoviário"),

    # Porto
    (["porto", "terminal portuário", "instalação portuária",
      "operação portuária", "aquaviário"], "Porto"),

    # Logística — armazéns, terminais de carga, ferrovia, aéreo
    (["armazenagem", "terminal de carga", "transporte ferroviário",
      "transporte aéreo de carga", "logística", "distribuição de mercadorias",
      "courier", "expresso"], "Logística"),

    # Estacionamento
    (["estacionamento", "parking"], "Estacionamento"),

    # Varejo
    (["comércio varejista", "supermercado", "hipermercado",
      "loja de departamento", "varejo"], "Varejo"),

    # Saúde
    (["hospital", "clínica", "diagnóstico", "plano de saúde",
      "assistência médica", "farmácia", "odontologia",
      "laboratório clínico"], "Saúde"),

    # Siderurgia — metais ferrosos e não ferrosos
    (["siderurgi", "produção de ferro", "produção de aço",
      "ferro-liga", "metalurgi", "laminação"], "Siderurgia"),

    # Telecom
    (["telecomunicação", "telefonia", "telefônica",
      "operadora de telecom", "provedor de internet",
      "transmissão de dados"], "Telecom"),

    # Tech — software e TI
    (["desenvolvimento de software", "tecnologia da informação",
      "processamento de dados", "consultoria em ti",
      "serviços de ti", "plataforma digital"], "Tech"),

    # Turismo — hospedagem e lazer
    (["hospedagem", "hotel", "resort", "turismo",
      "parque temático", "parque de diversão"], "Turismo"),

    # Agronegócio — produção primária (após Proteínas para não colidir)
    (["agropecuária", "agricultura", "pecuária", "cultivo",
      "lavoura", "soja", "milho", "cana-de-açúcar",
      "produção de grãos", "produção vegetal", "produção animal",
      "cooperativa agrícola", "insumo agrícola"], "Agronegócio"),
]

# ---------------------------------------------------------------------------
# Mapeamento por prefixo CNAE de 2 dígitos (fallback quando descrição não basta)
# Apenas setores com mapeamento inequívoco recebem valor; demais ficam ausentes.
# ---------------------------------------------------------------------------
_CNAE_PREFIXO: dict[str, str] = {
    "01": "Agronegócio", "02": "Agronegócio", "03": "Agronegócio",
    "06": "Oil & Gas", "19": "Oil & Gas",
    "24": "Siderurgia", "25": "Siderurgia",
    "35": "Energia",
    "36": "Saneamento", "37": "Saneamento", "38": "Saneamento", "39": "Saneamento",
    "47": "Varejo",
    "50": "Porto",
    "51": "Logística", "52": "Logística", "53": "Logística",
    "55": "Turismo",
    "61": "Telecom",
    "62": "Tech", "63": "Tech",
    "68": "Real Estate",
    "86": "Saúde", "87": "Saúde", "88": "Saúde",
}


# ---------------------------------------------------------------------------
# Funções públicas
# ---------------------------------------------------------------------------

def extrair_cnpj(texto: str) -> Optional[str]:
    """Extrai e normaliza o primeiro CNPJ válido encontrado no texto."""
    if not texto:
        return None
    m = _CNPJ_RE.search(texto)
    if not m:
        return None
    cnpj = re.sub(r"[^\d]", "", m.group(0))
    return cnpj if len(cnpj) == 14 else None


def classificar_por_cnpj(cnpj: str) -> Optional[str]:
    """
    Consulta BrasilAPI, classifica pelo texto da atividade CNAE e,
    se necessário, pelo prefixo do código. Retorna None quando não há
    mapeamento claro para a lista fechada de setores.
    """
    with _cache_lock:
        if cnpj in _cache:
            return _cache[cnpj]

    setor = None
    with _semaphore:
        with _cache_lock:
            if cnpj in _cache:
                return _cache[cnpj]

        try:
            resp = requests.get(
                f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                atividades = data.get("atividade_principal") or []
                if atividades:
                    code = atividades[0].get("code", "") or ""
                    descricao = (atividades[0].get("text") or "").lower()

                    # 1. Tenta classificar pela descrição (mais preciso)
                    setor = _setor_por_descricao(descricao)

                    # 2. Fallback: prefixo numérico do CNAE
                    if setor is None:
                        prefixo = re.sub(r"[^\d]", "", code)[:2]
                        setor = _CNAE_PREFIXO.get(prefixo)
        except Exception:
            pass

        with _cache_lock:
            _cache[cnpj] = setor

    return setor


def classificar_setor(textos: list[str]) -> Optional[str]:
    """
    Tenta classificar o setor extraindo CNPJ dos textos candidatos (em ordem).
    Retorna o primeiro setor encontrado ou None.
    """
    for texto in textos:
        cnpj = extrair_cnpj(texto or "")
        if cnpj:
            return classificar_por_cnpj(cnpj)
    return None


# ---------------------------------------------------------------------------
# Helper interno
# ---------------------------------------------------------------------------

def _setor_por_descricao(descricao: str) -> Optional[str]:
    """Mapeia descrição da atividade CNAE para a lista fechada de setores."""
    if not descricao:
        return None
    for palavras, setor in _DESCRICAO_REGRAS:
        if any(p in descricao for p in palavras):
            return setor
    return None
