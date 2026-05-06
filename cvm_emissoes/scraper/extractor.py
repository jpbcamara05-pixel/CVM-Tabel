"""
Transforma dados brutos das APIs da CVM em registros estruturados por série.

Cada emissão pode ter múltiplas séries; cada série gera uma linha na base final.
Campos ausentes são registrados como None — nunca inventados.
"""

import logging
import re
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers para navegação segura em dicionários aninhados
# ---------------------------------------------------------------------------

def _safe(d: Any, *keys, default=None):
    """Navega de forma segura por dicionários/listas aninhados."""
    cur = d
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if k < len(cur) else None
        else:
            return default
    return cur if cur is not None else default


def _campos_para_dict(campos: list[dict]) -> dict[str, str]:
    """Converte lista [{campoNome, campoValor}] em dicionário de fácil acesso."""
    if not campos:
        return {}
    result = {}
    for c in campos:
        nome = c.get("campoNome", "").strip()
        valor = c.get("campoValor", "") or ""
        if nome:
            result[nome] = valor.strip()
    return result


def _inf_oferta_dict(inf_oferta: list[dict]) -> dict[str, str]:
    """Converte lista de infOferta em dicionário por campoNome."""
    if not inf_oferta:
        return {}
    return {
        item.get("campoNome", "").strip(): (item.get("valor") or "").strip()
        for item in inf_oferta
        if item.get("campoNome")
    }


def _participantes_por_tipo(participantes: list[dict]) -> dict[str, list[str]]:
    """Agrupa participantes por tipo em dicionário."""
    grupos: dict[str, list[str]] = {}
    for p in (participantes or []):
        tipo = p.get("tipo", "")
        nome = (p.get("razaoSocial") or "").strip()
        if tipo and nome:
            grupos.setdefault(tipo, []).append(nome)
    return grupos


def _calcular_prazo(data_emissao: Optional[str], data_vencimento: Optional[str]) -> Optional[str]:
    """Calcula prazo como 'X anos' ou 'X meses' a partir das datas DD/MM/AAAA."""
    if not data_emissao or not data_vencimento:
        return None
    try:
        fmt = "%d/%m/%Y"
        emissao = datetime.strptime(data_emissao.strip(), fmt)
        venc = datetime.strptime(data_vencimento.strip(), fmt)
        delta_dias = (venc - emissao).days
        if delta_dias <= 0:
            return None
        anos = delta_dias / 365.25
        if anos >= 1.0:
            return f"{anos:.1f} anos".replace(".", ",")
        meses = delta_dias / 30.44
        return f"{meses:.1f} meses".replace(".", ",")
    except Exception:
        return None


def _normalizar_data(data_str: Optional[str]) -> Optional[str]:
    """Garante formato DD/MM/AAAA para datas em vários formatos."""
    if not data_str:
        return None
    data_str = data_str.strip()
    # Formato ISO: 2024-03-27T17:00:00Z
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", data_str)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    # Já no formato DD/MM/AAAA
    if re.match(r"^\d{2}/\d{2}/\d{4}$", data_str):
        return data_str
    return data_str  # retorna original se não reconhecido


def _identificar_emissora_devedora(
    nome_emissor: str,
    inf_oferta: dict[str, str],
    nome_vm: str,
) -> str:
    """
    Determina a empresa devedora/lastro econômico.

    Para instrumentos securitizados (CRI, CRA, FIDC, CRE),
    usa 'Identificação dos devedores e coobrigados' se disponível.
    Para os demais, usa o nome do emissor.

    Retorna sempre string — nunca infere ou inventa.
    """
    SECURITIZADOS = {
        "certificados de recebíveis imobiliários",
        "certificados de recebíveis do agronegócio",
        "certificados de recebíveis",
        "outros títulos de securitização",
        "cotas de fidc",
        "cotas de fif",  # inclui FIDC sob RCVM 175
        "cédulas de crédito bancário",
    }

    eh_securitizado = any(s in nome_vm.lower() for s in SECURITIZADOS)

    if eh_securitizado:
        devedores_raw = inf_oferta.get(
            "Identificação dos devedores e coobrigados", ""
        ).strip()
        if devedores_raw:
            # Pega apenas o primeiro devedor listado (principal)
            primeiro = devedores_raw.split("\n")[0].strip()
            if primeiro:
                return _limpar_nome_devedor(primeiro)
        # Sem identificação segura de devedor — retorna vazio (não inventa)
        return ""

    return nome_emissor.strip() if nome_emissor else ""


# ---------------------------------------------------------------------------
# Extração principal: uma lista de registros por série
# ---------------------------------------------------------------------------

def extrair_registros(
    item_listagem: dict,
    requerimento: Optional[dict],
    inf_oferta: list[dict],
    participantes: list[dict],
    historico: list[dict],
) -> list[dict]:
    """
    Extrai todos os registros (um por série) de uma emissão.

    Args:
        item_listagem: Registro retornado pela pesquisa detalhada
        requerimento:  Dados do endpoint pesquisar/requerimento/{id}
        inf_oferta:    Dados do endpoint pesquisar/infOferta/{id}
        participantes: Dados do endpoint pesquisar/participantes/{id}
        historico:     Dados do endpoint pesquisar/historicoStatus/{id}

    Returns:
        Lista de dicionários, um por série (ou um registro sem série se não houver grupos).
    """
    id_req = item_listagem.get("idRequerimento", "")
    nome_vm = item_listagem.get("nomeValorMobiliario", "") or ""
    nome_emissor = item_listagem.get("nomeEmissor", "") or ""

    inf_dict = _inf_oferta_dict(inf_oferta)
    partic_dict = _participantes_por_tipo(participantes)

    # Texto bruto para extração de CNPJ pelo sector_classifier.
    # Prioriza o campo de devedores (contém CNPJ explícito para securitizados)
    # e inclui o nome do emissor como fallback para debêntures.
    _texto_busca_cnpj = " ".join(filter(None, [
        inf_dict.get("Identificação dos devedores e coobrigados", ""),
        nome_emissor,
    ]))

    # ------------------------------------------------------------------
    # Campos de nível emissão
    # ------------------------------------------------------------------
    link_cvm = f"https://web.cvm.gov.br/sre-publico-cvm/#/oferta-publica/{id_req}"

    status = item_listagem.get("statusDaOferta") or ""

    # Data de requerimento (campo "data" da listagem)
    data_requerimento = _normalizar_data(item_listagem.get("data"))

    # Data de encerramento: vem do dadosColocacao.dataEncerramento
    data_encerramento_raw = _safe(requerimento, "dadosColocacao", "dataEncerramento")
    data_encerramento = _normalizar_data(data_encerramento_raw)

    # Volumes gerais da oferta
    info_gerais = _safe(requerimento, "informacoesGerais") or {}
    volume_inicial = info_gerais.get("valorTotalInicial") or info_gerais.get("valorTotal")
    volume_final = info_gerais.get("valorTotalFinal") or info_gerais.get("valorTotal")

    # Público alvo — busca case-insensitive para cobrir variações da API
    # (ex: "Público alvo", "Público-alvo", "Público Alvo", "publico alvo")
    _publico_chave = next(
        (v for k, v in inf_dict.items() if "p" in k.lower() and "blico" in k.lower() and "lvo" in k.lower()),
        ""
    )
    publico_alvo = _normalizar_publico_alvo(_publico_chave)

    # Regime de distribuição (firme / melhores esforços)
    regime_distribuicao = inf_dict.get("Regime de distribuição") or ""

    # Coordenadores
    coordenadores_lista = (
        partic_dict.get("COORDENADOR", []) +
        [n for n in partic_dict.get("REQUERENTE", [])
         if n not in partic_dict.get("COORDENADOR", [])]
    )
    coordenadores = "; ".join(coordenadores_lista) if coordenadores_lista else ""

    # Agência de rating (nível da emissão)
    agencia_rating = inf_dict.get("Avaliador de risco") or ""

    # Emissora/devedora
    emissora_devedora = _identificar_emissora_devedora(nome_emissor, inf_dict, nome_vm)

    # ------------------------------------------------------------------
    # Book mercado / book consórcio
    # ------------------------------------------------------------------
    dados_colocacao = _safe(requerimento, "dadosColocacao")

    # Book mercado: soma QVM de investidores de mercado (não ligados ao consórcio)
    book_mercado = _calcular_book_mercado(dados_colocacao)

    # Book consórcio: soma QVM de intermediárias, ligadas ao emissor e relacionadas
    book_consorcio = _calcular_book_consorcio(dados_colocacao)

    # ------------------------------------------------------------------
    # Processar séries (grupos -> series)
    # ------------------------------------------------------------------
    grupos = _safe(requerimento, "grupos") or []

    registros = []

    for grupo in grupos:
        series = grupo.get("series", []) or []
        for serie in series:
            registro = _extrair_serie(
                serie=serie,
                # Campos de emissão
                status=status,
                data_requerimento=data_requerimento,
                data_encerramento=data_encerramento,
                emissora_devedora=emissora_devedora,
                nome_vm=nome_vm,
                volume_inicial=volume_inicial,
                volume_final=volume_final,
                publico_alvo=publico_alvo,
                regime_distribuicao=regime_distribuicao,
                agencia_rating=agencia_rating,
                book_mercado=book_mercado,
                book_consorcio=book_consorcio,
                coordenadores=coordenadores,
                link_cvm=link_cvm,
                id_req=id_req,
                texto_busca_cnpj=_texto_busca_cnpj,
            )
            registros.append(registro)

    # Se não houver séries estruturadas (emissão sem grupos, ex: ações),
    # cria um registro mínimo com os dados disponíveis
    if not registros:
        registros.append(_registro_sem_serie(
            status=status,
            data_requerimento=data_requerimento,
            data_encerramento=data_encerramento,
            emissora_devedora=emissora_devedora,
            nome_vm=nome_vm,
            volume_inicial=volume_inicial,
            volume_final=volume_final,
            publico_alvo=publico_alvo,
            regime_distribuicao=regime_distribuicao,
            agencia_rating=agencia_rating,
            book_mercado=book_mercado,
            book_consorcio=book_consorcio,
            coordenadores=coordenadores,
            link_cvm=link_cvm,
            id_req=id_req,
            numero_registro=item_listagem.get("numeroRegistro", ""),
            texto_busca_cnpj=_texto_busca_cnpj,
        ))

    return registros


def _extrair_serie(serie: dict, **emissao_fields) -> dict:
    """Extrai um registro para uma série específica."""

    numero_registro_serie = serie.get("numeroRegistro", "")

    # loteInicial e loteFinal
    lote_inicial = serie.get("loteInicial") or {}
    lote_final = serie.get("loteFinal") or {}

    campos_inicial = _campos_para_dict(lote_inicial.get("camposCadastrados", []))
    campos_final = _campos_para_dict(lote_final.get("camposCadastrados", []))

    # Volume da série
    lote_base_final = lote_final.get("loteBase") or lote_inicial.get("loteBase") or {}
    volume_serie = lote_final.get("valorTotalLote") or lote_inicial.get("valorTotalLote")

    # Data de emissão
    data_emissao = (
        campos_final.get("Data de emissão")
        or campos_inicial.get("Data de emissão")
    )

    # Data de vencimento
    data_vencimento = (
        campos_final.get("Data de vencimento")
        or campos_inicial.get("Data de vencimento")
    )

    # Prazo
    prazo = _calcular_prazo(data_emissao, data_vencimento)

    # Incentivada
    # CRI e CRA são sempre incentivados (isenção fiscal estrutural).
    # Para debêntures e demais, lê o campo específico da série.
    _nome_vm_lower = emissao_fields["nome_vm"].lower()
    _sempre_incentivado = any(s in _nome_vm_lower for s in [
        "certificados de recebíveis imobiliários",
        "certificados de recebíveis do agronegócio",
        "certificados de recebíveis",
    ])
    if _sempre_incentivado:
        incentivada = "Sim"
    else:
        incentivada_raw = (
            campos_final.get("Título incentivado - Lei 12.431/11")
            or campos_inicial.get("Título incentivado - Lei 12.431/11")
            or ""
        )
        incentivada = _normalizar_sim_nao(incentivada_raw)

    # Taxa teto: extrai da descrição bruta do lote inicial
    taxa_teto_raw = (
        _campo_taxa(campos_inicial, "máxima")
        or _campo_taxa(campos_inicial, "remuneração")
    )
    taxa_teto = _extrair_taxa_teto(taxa_teto_raw)

    # Taxa final: parser com lógica de maior spread quando houver duas opções
    taxa_final_raw = (
        lote_final.get("taxaRemuneracao")
        or _campo_taxa(campos_final, "final")
        or _campo_taxa(campos_final, "remuneração")
    )
    taxa_final = _extrair_taxa_final(taxa_final_raw)

    # Regra NTN-B + IPCA: quando teto referencia NTN-B e o final já é IPCA,
    # exibe ambas as formas no Taxa Teto ("NTN-B+X% ou IPCA+Y%").
    # O Taxa Final permanece como está — já é a taxa vencedora pós-bookbuilding.
    if (taxa_teto and taxa_final
            and taxa_teto not in ("Não informado",)
            and taxa_final not in ("Não informado",)
            and re.match(r"NTN-B", taxa_teto, re.IGNORECASE)
            and re.match(r"IPCA", taxa_final, re.IGNORECASE)):
        taxa_teto = f"{taxa_teto} ou {taxa_final}"

    # Amortização
    amortizacao = (
        _campo_amort(campos_final)
        or _campo_amort(campos_inicial)
    )

    # Rating (avaliação de risco da série)
    rating = (
        campos_final.get("Avaliação de risco")
        or campos_inicial.get("Avaliação de risco")
        or ""
    )
    # Rating "N/A" → vazio (sem avaliação, não erro)
    if rating.upper() in {"N/A", "NA", "-"}:
        rating = ""

    return {
        "status": emissao_fields["status"],
        "data_requerimento": emissao_fields["data_requerimento"],
        "data_encerramento": emissao_fields["data_encerramento"],
        "emissora_devedora": emissao_fields["emissora_devedora"],
        "setor": None,  # não disponível no payload do SRE
        "data_emissao": data_emissao,
        "valor_mobiliario": emissao_fields["nome_vm"],
        "incentivada": incentivada,
        "nome": numero_registro_serie or emissao_fields.get("numero_registro", ""),
        "publico_alvo": emissao_fields["publico_alvo"],
        "volume_serie": volume_serie,
        "volume_inicial_oferta": emissao_fields["volume_inicial"],
        "volume_final_oferta": emissao_fields["volume_final"],
        "prazo": prazo,
        "amortizacao": amortizacao,
        "taxa_teto": taxa_teto,
        "taxa_final": taxa_final,
        "agencia_rating": emissao_fields["agencia_rating"],
        "rating": rating,
        "firme_sindicato": emissao_fields["regime_distribuicao"],
        "book_mercado": emissao_fields["book_mercado"],
        "book_consorcio": emissao_fields["book_consorcio"],
        "coordenadores": emissao_fields["coordenadores"],
        "link_cvm": emissao_fields["link_cvm"],
        # Metadados internos (removidos na exibição/exportação)
        "_id_requerimento": emissao_fields["id_req"],
        "_texto_busca_cnpj": emissao_fields.get("texto_busca_cnpj", ""),
    }


def _registro_sem_serie(**fields) -> dict:
    """Cria registro quando não há séries estruturadas (ex: ações, fundos simples)."""
    _nome_vm_lower = (fields.get("nome_vm") or "").lower()
    _sempre_incentivado = any(s in _nome_vm_lower for s in [
        "certificados de recebíveis imobiliários",
        "certificados de recebíveis do agronegócio",
        "certificados de recebíveis",
    ])
    return {
        "status": fields["status"],
        "data_requerimento": fields["data_requerimento"],
        "data_encerramento": fields["data_encerramento"],
        "emissora_devedora": fields["emissora_devedora"],
        "setor": None,
        "data_emissao": None,
        "valor_mobiliario": fields["nome_vm"],
        "incentivada": "Sim" if _sempre_incentivado else None,
        "nome": fields.get("numero_registro", ""),
        "publico_alvo": fields["publico_alvo"],
        "volume_serie": None,
        "volume_inicial_oferta": fields["volume_inicial"],
        "volume_final_oferta": fields["volume_final"],
        "prazo": None,
        "amortizacao": None,
        "taxa_teto": None,
        "taxa_final": None,
        "agencia_rating": fields["agencia_rating"],
        "rating": None,
        "firme_sindicato": fields["regime_distribuicao"],
        "book_mercado": fields["book_mercado"],
        "book_consorcio": fields["book_consorcio"],
        "coordenadores": fields["coordenadores"],
        "link_cvm": fields["link_cvm"],
        "_id_requerimento": fields["id_req"],
        "_texto_busca_cnpj": fields.get("texto_busca_cnpj", ""),
    }


# ---------------------------------------------------------------------------
# Pequenos helpers de normalização
# ---------------------------------------------------------------------------

def _limpar_nome_devedor(texto: str) -> str:
    """
    Remove prefixos comuns (a), Devedora:, etc.) e detalhes de endereço/CNPJ
    do campo de identificação de devedores para extrair apenas o nome da empresa.
    Nunca inventa nem trunca além do necessário.
    """
    if not texto:
        return texto
    t = texto.strip()
    # Remove prefixos de lista: "a) ", "b) ", "i) ", "1. ", etc.
    t = re.sub(r"^[a-zA-Zi0-9]+[\)\.][\s]+", "", t)
    # Remove "Devedora: ", "Devedor: ", "Emissora: " do início
    t = re.sub(r"^(Devedora?|Emissora?|Coobrigad[ao]):\s*", "", t, flags=re.IGNORECASE)
    # Remove detalhes de endereço após hífen longo ou vírgula com "com sede"/"CNPJ"
    # Padrão: "Nome Empresa – CNPJ:..." ou "Nome Empresa, com sede..."
    t = re.sub(r"\s*[-–—]\s*(CNPJ|CPF|com sede|Rua|Av|Avenida|Cidade).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r",\s*(com sede|CNPJ|CPF|inscrito).*$", "", t, flags=re.IGNORECASE)
    # Remove "(Devedora)" no final
    t = re.sub(r"\s*\(Devedora?\)\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def _normalizar_sim_nao(valor: str) -> Optional[str]:
    if not valor:
        return None
    v = valor.strip().lower()
    if v in {"sim", "s", "yes", "true", "1"}:
        return "Sim"
    if v in {"não", "nao", "n", "no", "false", "0"}:
        return "Não"
    return valor.strip() or None


def _campo_taxa(campos: dict, hint: str) -> Optional[str]:
    """Encontra campo de taxa por hint em parte do nome do campo."""
    for nome, valor in campos.items():
        if hint in nome.lower() and valor:
            return valor
    return None


def _campo_amort(campos: dict) -> Optional[str]:
    for nome, valor in campos.items():
        if "amortiz" in nome.lower() and valor:
            return valor
    return None


def _normalizar_publico_alvo(texto: str) -> str:
    """
    Normaliza o campo 'Público alvo' da CVM para uma das três categorias padrão.

    Ordem importa: Profissional ⊂ Qualificado, então verifica profissional primeiro.
    Retorna o texto original quando não há mapeamento seguro.
    """
    if not texto:
        return ""
    t = texto.strip().lower()
    if "profissional" in t:
        return "Investidor Profissional"
    if "qualificado" in t:
        return "Investidor Qualificado"
    if "geral" in t or "varejo" in t:
        return "Investidor Geral"
    return texto.strip()


def _ano_2dig_texto(texto: str) -> Optional[str]:
    """Extrai os 2 últimos dígitos do ano de referências temporais no texto."""
    m = re.search(r"\b20(\d{2})\b", texto)
    if m:
        return m.group(1)
    # Formato curto de contrato DI: "F27", "F29"
    m = re.search(r"\bF(\d{2})\b", texto, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _spread_do_texto(texto: str) -> tuple[Optional[str], bool]:
    """
    Extrai (valor_spread_str, is_negativo) da descrição de taxa.

    Tenta padrões do mais específico ao mais genérico.
    Retorna (None, False) se não encontrar.
    """
    if not texto:
        return None, False

    # 1. "spread negativo de -X%"
    m = re.search(r"spread\s+negativo\s+de\s+(-?\d+(?:[,.]\d+)?)\s*%", texto, re.IGNORECASE)
    if m:
        return m.group(1).lstrip("+-").replace(".", ","), True

    # 2. "decrescida [exponencialmente de] [sobretaxa/spread] [equivalente a] X%" → negativo
    m = re.search(
        r"decrescid[ao]s?\s+(?:exponencialmente\s+)?(?:de\s+uma?\s+)?"
        r"(?:sobretaxa|spread)?\s*(?:\([^)]*\)\s*)?(?:equivalente\s+a\s+)?(\d+(?:[,.]\d+)?)\s*%",
        texto, re.IGNORECASE,
    )
    if m:
        return m.group(1).replace(".", ","), True

    # 3. "+ X%" ou "+ X ao ano" (sinal positivo explícito)
    m = re.search(r"\+\s*(\d+(?:[,.]\d+)?)\s*(?:%|ao?\s+ano\b|a\.a\.?)", texto, re.IGNORECASE)
    if m:
        return m.group(1).replace(".", ","), False

    # 4. "+ Xbps" → converte para %
    m = re.search(r"\+\s*(\d+)\s*bps?\b", texto, re.IGNORECASE)
    if m:
        return f"{int(m.group(1)) / 100:.2f}".replace(".", ","), False

    # 5. "spread/sobretaxa [(...)] [equivalente a / correspondente a / de] X%"
    m = re.search(
        r"(?:spread|sobretaxa)\s*(?:\([^)]*\))?\s*"
        r"(?:equivalente\s+a\s+|correspondente\s+a\s+|de\s+)?(\d+(?:[,.]\d+)?)\s*%",
        texto, re.IGNORECASE,
    )
    if m:
        return m.group(1).replace(".", ","), False

    # 6. "limitado a X% ao ano" (spread máximo de bookbuilding)
    m = re.search(r"limitado\s+a\s+(\d+(?:[,.]\d+)?)\s*%", texto, re.IGNORECASE)
    if m:
        return m.group(1).replace(".", ","), False

    # 7. "acrescida(o) [exponencialmente] [de juros] de X%"
    m = re.search(
        r"acrescid[ao]s?\s+(?:exponencialmente\s+)?(?:de\s+)?(?:juros\s+(?:de\s+)?)?(\d+(?:[,.]\d+)?)\s*%",
        texto, re.IGNORECASE,
    )
    if m:
        return m.group(1).replace(".", ","), False

    # 8. "- X%" (sinal negativo explícito)
    m = re.search(r"-\s*(\d+(?:[,.]\d+)?)\s*%", texto)
    if m:
        return m.group(1).replace(".", ","), True

    # 9. Percentual simples (último recurso)
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:%|ao?\s+ano\b|a\.a\.?)", texto, re.IGNORECASE)
    if m:
        return m.group(1).replace(".", ","), False

    return None, False


def _formatar_taxa(indexador: str, spread: Optional[str] = None, negativo: bool = False) -> str:
    """Formata a taxa no padrão sem espaços: 'CDI+0,75%', 'NTN-B35-0,12%', 'IPCA+5%'."""
    if not spread:
        return indexador
    return f"{indexador}{'-' if negativo else '+'}{spread}%"


def _spread_numerico(taxa: str) -> float:
    """Valor numérico do spread para comparação entre taxas candidatas."""
    # Prefere o número após +/- explícito
    m = re.search(r"[+-](\d+(?:[,.]\d+)?)\s*%", taxa)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    # Fallback: qualquer percentual no texto (para prefixadas como "6,37%")
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*%", taxa)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return 0.0


def _extrair_taxa_teto(texto: str) -> Optional[str]:
    """
    Extrai e normaliza a taxa de remuneração da CVM.

    Saída sem espaços em torno de +/-:
      CDI+X%  |  IPCA+X%  |  NTN-Bano±X%  |  PreDIano+X%  |  X%  |  X% CDI

    Retorna None quando o texto está vazio/ausente.
    Retorna 'Não informado' quando o texto existe mas não é identificável.
    """
    if not texto or not texto.strip():
        return None
    t = texto.strip()

    # ------------------------------------------------------------------
    # 1. Reverso com + literal: "X% [a.a.] + NTN-B/IPCA/..."
    # ------------------------------------------------------------------
    for padrao_idx, nome_idx in [
        (r"NTN[-\s]?B(?:\s+\d{4})?", "NTN-B"),
        (r"IPCA", "IPCA"),
        (r"IGP[-\s]?M", "IGP-M"),
        (r"SELIC", "SELIC"),
    ]:
        m = re.search(
            rf"(\d+(?:[,.]\d+)?)\s*%?\s*(?:a\.a\.?)?\s*\+\s*{padrao_idx}",
            t, re.IGNORECASE,
        )
        if m:
            return f"{nome_idx}+{m.group(1).replace('.', ',')}%"

    # ------------------------------------------------------------------
    # 2. Fator × CDI: "X% CDI" / "X% do CDI" sem spread seguinte
    # ------------------------------------------------------------------
    m_fator = re.search(
        r"(\d+(?:[,.]\d+)?)\s*%\s*(?:d[ao]\s+)?(?:CDI|DI)\b(?!\s*\+)",
        t, re.IGNORECASE,
    )
    if m_fator:
        return f"{m_fator.group(1).replace('.', ',')}% CDI"

    # ------------------------------------------------------------------
    # 3. PreDI: contrato futuro de DI com referência temporal
    # ------------------------------------------------------------------
    m_predi = re.search(
        r"(?:Pr[eé]x?[-\s]?DI"                              # PréxDI, PreDI, Pré-DI
        r"|DI\s+[Ff]uturo|[Ff]uturo\s+(?:de\s+)?DI"
        r"|contrato\s+(?:futuro\s+)?(?:de\s+)?DI"
        r"|taxa\s+prefixada\s+(?:baseada\s+)?(?:no|em|do)\s+DI)",
        t, re.IGNORECASE,
    )
    if m_predi:
        ano = _ano_2dig_texto(t)
        spread, neg = _spread_do_texto(t[m_predi.end():])
        if ano:
            return _formatar_taxa(f"PreDI{ano}", spread, neg)
        # Sem ano identificável → não preenche PreDI, cai para CDI abaixo

    # ------------------------------------------------------------------
    # 4. NTN-B (explícita) ou Tesouro IPCA+ — com extração de ano
    # Cede para seção 5 (IPCA) quando um IPCA explícito aparece antes do
    # token NTN-B no texto (ex: "IPCA+8% ou NTN-B+1%").
    # Tesouro IPCA+ tem precedência por ser a própria referência NTN-B.
    # ------------------------------------------------------------------
    m_ntnb = re.search(r"NTN[-\s]?B\s*(\d{2,4})?\b", t, re.IGNORECASE)
    m_tip  = re.search(r"Tesouro\s+IPCA\+?", t, re.IGNORECASE)
    _m_ipca_pos = re.search(r"\bIPCA\b", t, re.IGNORECASE)
    _ntnb_antes_ipca = (
        m_ntnb is not None
        and _m_ipca_pos is not None
        and _m_ipca_pos.start() < m_ntnb.start()
        and m_tip is None
    )

    if (m_ntnb or m_tip) and not _ntnb_antes_ipca:
        # Ano de dois dígitos
        ano = None
        if m_ntnb and m_ntnb.group(1):
            raw = m_ntnb.group(1)
            ano = raw[-2:] if len(raw) == 4 else raw.zfill(2)
        else:
            ano = _ano_2dig_texto(t)

        indexador = f"NTN-B{ano}" if ano else "NTN-B"
        ref = m_ntnb or m_tip
        restante = t[ref.end():]
        spread, neg = None, False

        # Verifica primeiro o padrão reverso: "X% acrescido [à/a] NTN-B/IPCA+"
        # Prioridade sobre o forward para evitar capturar a taxa "ou" no final do texto
        antes = t[:ref.start()]
        m_pct_antes = re.search(r"(\d+(?:[,.]\d+)?)\s*%", antes)
        if m_pct_antes:
            between = t[m_pct_antes.end():ref.start()]
            if re.search(r"acrescid[ao]", between, re.IGNORECASE):
                spread = m_pct_antes.group(1).replace(".", ",")
                neg = False

        # Se não houver reverso, busca spread após o indexador (caso normal/"decrescida")
        if not spread:
            spread, neg = _spread_do_texto(restante)

        return _formatar_taxa(indexador, spread, neg) if spread else indexador

    # ------------------------------------------------------------------
    # 5. IPCA + spread
    # ------------------------------------------------------------------
    m_ipca = re.search(r"\bIPCA\b", t, re.IGNORECASE)
    if m_ipca:
        spread, neg = _spread_do_texto(t[m_ipca.end():])
        if not spread:
            # Spread pode preceder o indexador em algumas construções
            spread, neg = _spread_do_texto(t[:m_ipca.start()])
        return _formatar_taxa("IPCA", spread, neg) if spread else "IPCA"

    # ------------------------------------------------------------------
    # 6. CDI / DI — \b garante que não captura "incidirão" etc.
    # ------------------------------------------------------------------
    m_cdi = re.search(
        r"(?:(?:\d+(?:[,.]\d+)?)\s*%\s*(?:d[ao]\s+)?)?(?:Taxa\s+)?\b(?:DI|CDI)\b",
        t, re.IGNORECASE,
    )
    if m_cdi:
        spread, neg = _spread_do_texto(t[m_cdi.end():])
        return _formatar_taxa("CDI", spread, neg) if spread else "CDI"

    # ------------------------------------------------------------------
    # 7. Outros indexadores: IGP-M, SELIC, TJLP, TR
    # ------------------------------------------------------------------
    for padrao, nome in [
        (r"IGP[-\s]?M", "IGP-M"),
        (r"\bSELIC\b", "SELIC"),
        (r"\bTJLP\b", "TJLP"),
        (r"\bTR\b", "TR"),
    ]:
        m = re.search(padrao, t, re.IGNORECASE)
        if m:
            spread, neg = _spread_do_texto(t[m.end():])
            return _formatar_taxa(nome, spread, neg) if spread else nome

    # ------------------------------------------------------------------
    # 8. Taxa prefixada (sem indexador flutuante)
    # ------------------------------------------------------------------
    # A: texto começa com a taxa — "14,53% a.a." ou "17,8565% (por extenso...)"
    m_inicio = re.match(r"\s*(\d+(?:[,.]\d+)?)\s*%", t)
    if m_inicio:
        return m_inicio.group(1).replace(".", ",") + "%"

    # B: percentual seguido de "ao ano" / "a.a." em qualquer posição
    # (ex: "(ii) 6,37% ao ano, base 252" após split de dual-rate)
    m_anual = re.search(r"(\d+(?:[,.]\d+)?)\s*%\s*(?:ao?\s+ano|a\.a\.?)", t, re.IGNORECASE)
    if m_anual:
        return m_anual.group(1).replace(".", ",") + "%"

    # C: taxa descrita por extenso — "juros equivalentes a 8,5334%"
    m_desc = re.search(
        r"(?:equivalentes?\s+a|correspondentes?\s+a"
        r"|juros\s+(?:prefixados?\s+)?(?:de\s+)?|taxa\s+de\s+)"
        r"\s*(\d+(?:[,.]\d+)?)\s*%",
        t, re.IGNORECASE,
    )
    if m_desc:
        return m_desc.group(1).replace(".", ",") + "%"

    # ------------------------------------------------------------------
    # 9. Fallback
    # ------------------------------------------------------------------
    return "Não informado"


def _extrair_taxa_final(texto: str) -> Optional[str]:
    """
    Extrai taxa final usando o mesmo parser de taxa_teto.

    Se o texto apresentar duas opções separadas por 'ou' (ex: "Maior entre...ou..."),
    retorna a taxa com o maior spread numérico.
    """
    if not texto or not texto.strip():
        return None

    # Divide em candidatos separados por "; ou" ou simples "ou"
    partes = re.split(r";\s*ou\s+|\s+ou\s+", texto, flags=re.IGNORECASE)
    if len(partes) >= 2:
        taxas = [_extrair_taxa_teto(p.strip()) for p in partes]
        taxas = [tx for tx in taxas if tx and tx != "Não informado"]
        if len(taxas) >= 2:
            return max(taxas, key=_spread_numerico)
        if len(taxas) == 1:
            return taxas[0]

    return _extrair_taxa_teto(texto)


def extrair_dados_anuncio_encerramento(texto_pdf: str) -> dict:
    """
    Extrai rating, agência de rating e setor do texto do Anúncio de Encerramento.

    Padrões observados nos PDFs da CVM:
      - "atribuída pela Fitch Ratings Brasil Ltda: "AAA(bra)""
      - "realizada pela Standard & Poor's Ratings: "brAA""

    Retorna dict com chaves rating, agencia_rating, setor — None quando não encontrado.
    """
    result: dict = {"rating": None, "agencia_rating": None, "setor": None}

    if not texto_pdf:
        return result

    # Normaliza aspas tipográficas para aspas retas (padrão dos PDFs da CVM)
    texto = (texto_pdf
             .replace("\u201c", '"').replace("\u201d", '"')
             .replace("\u2018", "'").replace("\u2019", "'"))

    # Rating + agência (agência pode ter quebra de linha no meio do nome)
    # Exemplos:
    #   realizada pela Fitch\nRatings Brasil Ltda: “AAA(bra)”
    #   atribuída pela\nStandard & Poor’s Ratings: “brAA”
    m = re.search(
        r'(?:atribu[i\u00ed]da|realizada)\s+pela\s+'
        r'([^:"]{4,80}?)\s*:\s*"'
        r'([A-Za-z0-9()+ /-]{2,20})'
        r'"',
        texto,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        agencia = " ".join(m.group(1).split())
        result["agencia_rating"] = agencia
        result["rating"] = m.group(2).strip()

    # Setor (tabela do Projeto de Investimento — só em debêntures incentivadas)
    # O valor do setor pode ser simples ("Energia") ou longo e multilinhas.
    # Captura até o próximo campo da tabela (linha que começa com letra maiúscula após quebra).
    m_setor = re.search(
        r'[Ss]etor\s+priorit[áa]rio\s+em\s+que\s+o\s+[Pp]rojeto'
        r'(?:\s+de\s+[Ii]nvestimento)?\s+se\s+enquadra\s+'
        r'(.+?)(?=\n[A-ZÁÀÃÉÍÓÚ][^\n]{5,}|\Z)',
        texto_pdf,
        re.DOTALL,
    )
    if m_setor:
        # Colapsa quebras de linha internas em espaço
        setor_raw = " ".join(m_setor.group(1).split())
        # Remove ponto final e parênteses explicativos longos
        setor_raw = re.sub(r'\s*\(.*?\)\s*\.?$', '', setor_raw).rstrip('.')
        result["setor"] = setor_raw.strip() or None

    return result


def _parse_qvm(valor) -> float:
    """Converte string no formato brasileiro '118.000,0000' para float."""
    if valor is None:
        return 0.0
    try:
        s = str(valor).strip().replace(".", "").replace(",", ".")
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


def _calcular_book_mercado(dados_colocacao: Optional[dict]) -> Optional[float]:
    """
    Soma as QVM de investidores genuinamente de mercado (não ligados ao consórcio/emissor).

    Inclui: pessoas naturais, clubes, fundos, previdência, seguradoras, estrangeiros.
    Exclui: consórcio, ligados ao emissor e demais PJ — esses compõem Book Consórcio.
    """
    if dados_colocacao is None:
        return None

    CAMPOS_MERCADO = [
        "qVMPessoasNaturais",
        "qVMClubesInvestimento",
        "qVMFundosInvestimento",
        "qVMEntidadesPrevidenciaPrivada",
        "qVMCompanhiasSeguradoras",
        "qVMInvestidoresEstrangeiros",
    ]

    return sum(_parse_qvm(dados_colocacao.get(campo)) for campo in CAMPOS_MERCADO)


def _calcular_book_consorcio(dados_colocacao: Optional[dict]) -> Optional[float]:
    """
    Soma as QVM de categorias ligadas ao consórcio de distribuição e ao emissor.

    Inclui: intermediárias consórcio, inst. financeiras ligadas, demais inst.
    financeiras, demais PJ ligadas e sócios/administradores/empregados.
    Complementar a _calcular_book_mercado (sem sobreposição de campos).
    """
    if dados_colocacao is None:
        return None

    CAMPOS_CONSORCIO = [
        "qVMInstituicaoIntermediariasConsorcioDistribuicao",
        "qVMInstituicaoFinanceirasParticipantesConsorcio",
        "qVMDemaisInstituicoesFinanceiras",
        "qVMDemaisPjParticipantesConsorcio",
        "qVMSociosAdministradoresEmpregadosPropostosParticipantesConsorcio",
    ]

    return sum(_parse_qvm(dados_colocacao.get(campo)) for campo in CAMPOS_CONSORCIO)
