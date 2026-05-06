"""
Orquestra a coleta completa: lista todas as emissões e busca os detalhes de cada uma.

Separa logs de erro em uma lista à parte para rastreabilidade.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import io

from .api_client import (
    CVMApiError,
    baixar_pdf_documento,
    buscar_documentos_publicados,
    buscar_historico_status,
    buscar_inf_oferta,
    buscar_participantes,
    buscar_requerimento,
    pesquisar_todas_paginas,
)
from .extractor import extrair_dados_anuncio_encerramento, extrair_registros
from .sector_classifier import classificar_setor
from .fees_extractor import (
    documento_e_prospecto_definitivo,
    extrair_fees_prospecto,
    publico_elegivel_para_fees,
)

logger = logging.getLogger(__name__)

# Limita downloads de PDF simultâneos para não sobrecarregar o servidor da CVM.
# Workers que fazem apenas chamadas de API não são afetados.
_pdf_semaphore = threading.Semaphore(3)

# Cache de PDFs já baixados na coleta atual (chave: UUID do documento).
# Evita baixar o mesmo arquivo mais de uma vez quando múltiplas séries referenciam
# o mesmo documento. Limpo no início de cada chamada a coletar().
_pdf_cache: dict[str, bytes] = {}
_pdf_cache_lock = threading.Lock()

# Se este número de emissões consecutivas falhar em todos os detalhes,
# assume que a API está com problema geral e emite aviso destacado.
LIMITE_FALHAS_CONSECUTIVAS = 10

# Expansão retroativa do pré-filtro da API (por data de requerimento).
# 0 = usa exatamente o período informado pelo usuário, sem expansão.
# A filtragem final também usa data_requerimento, então não há necessidade de expandir.
ANOS_PRE_FILTRO = 0

# Workers paralelos para busca de detalhes + enrichment de PDF.
# Cada worker abre até 5 sub-threads internamente — total máximo: MAX_WORKERS × 5 conexões.
# Aumentado para 10 porque a maior parte do tempo é I/O (API + PDF downloads).
MAX_WORKERS = 10


@dataclass
class ResultadoColeta:
    registros: list[dict] = field(default_factory=list)
    erros: list[dict] = field(default_factory=list)
    total_emissoes: int = 0
    total_series: int = 0
    # Contadores antes do pós-filtro por encerramento (para mensagens explicativas)
    total_emissoes_pre_filtro: int = 0
    total_series_pre_filtro: int = 0


def coletar(
    data_inicio: str,
    data_fim: str,
    valor_mobiliario_nome: Optional[str] = None,
    status: Optional[str] = None,
    progresso_callback: Optional[Callable] = None,
    buscar_fees: bool = False,
) -> ResultadoColeta:
    """
    Executa a coleta completa:
    1. Lista todas as emissões no período/filtro
    2. Para cada emissão, busca os detalhes complementares
    3. Extrai e estrutura os dados por série

    Args:
        data_inicio: DD/MM/AAAA
        data_fim: DD/MM/AAAA
        valor_mobiliario_nome: Opcional — filtra por tipo de instrumento
        status: Opcional — filtra por status
        progresso_callback: Callable(etapa, atual, total, mensagem)

    Returns:
        ResultadoColeta com os registros e log de erros
    """
    resultado = ResultadoColeta()

    global _pdf_cache
    with _pdf_cache_lock:
        _pdf_cache.clear()

    def _cb(etapa, atual, total, msg=""):
        if progresso_callback:
            progresso_callback(etapa, atual, total, msg)

    # Datas para o filtro de encerramento (usadas na etapa final de pós-filtro)
    data_inicio_dt = _parse_data(data_inicio)
    data_fim_dt = _parse_data(data_fim)

    # A API só filtra por data de requerimento. Para capturar emissões cujo
    # encerramento cai no período desejado mas cujo requerimento foi anterior,
    # expandimos o início do pré-filtro em ANOS_PRE_FILTRO anos.
    data_inicio_api = _expandir_data_inicio(data_inicio, anos=ANOS_PRE_FILTRO)

    # ------------------------------------------------------------------
    # Etapa 1: listar emissões (pré-filtro ampliado por data de requerimento)
    # ------------------------------------------------------------------
    _cb("listagem", 0, 1,
        f"Buscando listagem de emissões (pré-filtro: requerimentos de "
        f"{data_inicio_api} a {data_fim})...")

    def _progresso_paginacao(pag, total_pags, total_reg):
        _cb("listagem", pag, total_pags,
            f"Paginando listagem: página {pag}/{total_pags} ({total_reg} emissões)")

    try:
        emissoes = pesquisar_todas_paginas(
            data_inicio_api, data_fim,
            valor_mobiliario_nome=valor_mobiliario_nome,
            status=status,
            progresso_callback=_progresso_paginacao,
        )
    except CVMApiError as e:
        # Erro na listagem é fatal — sem a lista não há o que detalhar.
        # Propaga com mensagem clara para o Streamlit exibir ao usuário.
        logger.error("Falha na listagem de emissões: %s", e)
        raise CVMApiError(
            f"Não foi possível obter a listagem de emissões da CVM.\n\n"
            f"Detalhe: {e}\n\n"
            f"Isso é um problema no servidor externo da CVM. "
            f"Aguarde alguns minutos e tente novamente."
        ) from e
    except Exception as e:
        logger.error("Erro inesperado na listagem: %s", e, exc_info=True)
        raise

    resultado.total_emissoes = len(emissoes)

    if not emissoes:
        logger.info("Nenhuma emissão encontrada para os filtros informados.")
        return resultado

    _cb("detalhes", 0, len(emissoes), f"Coletando detalhes de {len(emissoes)} emissões...")

    # ------------------------------------------------------------------
    # Etapa 2: detalhar cada emissão (paralelo com MAX_WORKERS threads)
    # ------------------------------------------------------------------
    falhas_consecutivas = 0
    concluidos = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submete busca + enrichment completo para cada emissão em paralelo
        future_to_item = {
            executor.submit(_processar_emissao, item, buscar_fees): item
            for item in emissoes
        }

        for future in as_completed(future_to_item):
            item = future_to_item[future]
            id_req = item.get("idRequerimento", "")
            numero_processo = item.get("numeroProcesso", id_req)
            concluidos += 1

            _cb("detalhes", concluidos, len(emissoes),
                f"Detalhando {concluidos}/{len(emissoes)}: {numero_processo}")

            try:
                registros_serie, erros_emissao = future.result()
            except Exception as e:
                logger.error("Erro inesperado ao processar [%s]: %s", id_req, e, exc_info=True)
                registros_serie = []
                erros_emissao = [{"campo": "busca_detalhes", "erro": str(e)}]

            # Detectar sequência de falhas (sinal de instabilidade geral da API)
            if _todos_falharam(erros_emissao):
                falhas_consecutivas += 1
                if falhas_consecutivas >= LIMITE_FALHAS_CONSECUTIVAS:
                    logger.error(
                        "%d emissões consecutivas falharam em todos os detalhes. "
                        "O servidor da CVM pode estar instável.",
                        falhas_consecutivas,
                    )
                    _cb(
                        "aviso", concluidos, len(emissoes),
                        f"⚠️ {falhas_consecutivas} emissões consecutivas com falha — "
                        f"o servidor da CVM pode estar instável. "
                        f"Continuando com dados parciais...",
                    )
            else:
                falhas_consecutivas = 0

            resultado.registros.extend(registros_serie)
            resultado.total_series += len(registros_serie)

            # Registrar erros desta emissão
            if erros_emissao:
                todos_falharam = _todos_falharam(erros_emissao)
                resultado.erros.append({
                    "id_requerimento": id_req,
                    "numero_processo": numero_processo,
                    "nome_emissor": item.get("nomeEmissor", ""),
                    "valor_mobiliario": item.get("nomeValorMobiliario", ""),
                    "data": item.get("data", ""),
                    "link_cvm": f"https://web.cvm.gov.br/sre-publico-cvm/#/oferta-publica/{id_req}",
                    "erros": "; ".join(e["campo"] + ": " + e["erro"] for e in erros_emissao),
                    "todos_campos_falharam": todos_falharam,
                    "tipo_falha": "Total" if todos_falharam else "Parcial",
                })

    # Salva contadores brutos antes do pós-filtro
    resultado.total_emissoes_pre_filtro = len(emissoes)
    resultado.total_series_pre_filtro = resultado.total_series

    # ------------------------------------------------------------------
    # Etapa 3: pós-filtro por data de requerimento e status
    # ------------------------------------------------------------------
    _cb("filtragem", 0, 1,
        f"Filtrando {resultado.total_series} séries por data de requerimento "
        f"({data_inicio} a {data_fim}) e status...")

    registros_filtrados = []
    ids_filtrados: set[str] = set()

    for reg in resultado.registros:
        req_str = reg.get("data_requerimento")
        if req_str:
            try:
                req_dt = _parse_data(req_str)
                if data_inicio_dt <= req_dt <= data_fim_dt:
                    registros_filtrados.append(reg)
                    ids_filtrados.add(reg.get("_id_requerimento", ""))
            except Exception:
                pass  # data malformada — exclui do resultado

    resultado.registros = registros_filtrados
    resultado.total_series = len(registros_filtrados)
    resultado.total_emissoes = len(ids_filtrados)

    _cb("concluido", len(emissoes), len(emissoes),
        f"Concluído: {resultado.total_series} séries de {resultado.total_emissoes} emissões "
        f"com requerimento em {data_inicio}–{data_fim}"
        + (f" · {len(resultado.erros)} com erros parciais" if resultado.erros else ""))

    return resultado


def _parse_data(data_str: str):
    """Converte string DD/MM/AAAA para objeto date."""
    return datetime.strptime(data_str.strip(), "%d/%m/%Y").date()


def _expandir_data_inicio(data_str: str, anos: int) -> str:
    """Recua a data em N anos para ampliar o pré-filtro da API."""
    d = _parse_data(data_str)
    try:
        expandida = d.replace(year=d.year - anos)
    except ValueError:  # 29/02 em ano não bissexto
        from datetime import timedelta
        expandida = d - timedelta(days=anos * 365)
    return expandida.strftime("%d/%m/%Y")


def _resumo_erro(e: CVMApiError) -> str:
    """Gera resumo legível de um CVMApiError para o log de erros do Excel."""
    if e.status_code == 500:
        return f"HTTP 500 — servidor da CVM indisponível (problema externo)"
    if e.status_code:
        return f"HTTP {e.status_code}"
    return str(e)


def _todos_falharam(erros: list[dict]) -> bool:
    """Retorna True se todas as 4 chamadas de detalhe falharam."""
    campos_criticos = {"requerimento", "infOferta", "participantes", "historicoStatus"}
    campos_com_erro = {e["campo"] for e in erros}
    return campos_criticos.issubset(campos_com_erro)


def _enriquecer_com_prospecto(registros: list[dict], documentos: list[dict]) -> None:
    """
    Extrai fees do Prospecto Definitivo para registros com público-alvo elegível.

    Só busca e baixa o PDF quando há pelo menos um registro com público-alvo
    Qualificado ou Geral. Deixa os campos em None se não encontrar claramente.
    Modifica a lista de registros in-place.
    """
    if not registros or not documentos:
        return

    # Inicializa chaves de fee em todos os registros (None = não encontrado)
    for reg in registros:
        reg.setdefault("fee_flat", None)
        reg.setdefault("fee_canal_distribuicao", None)
        reg.setdefault("fee_canal_flat", None)
        reg.setdefault("fee_sucesso", None)

    # Verifica se algum registro é elegível
    elegiveis = [r for r in registros if publico_elegivel_para_fees(r.get("publico_alvo"))]
    if not elegiveis:
        return

    doc_prospecto = next(
        (d for d in documentos if documento_e_prospecto_definitivo(d.get("nome", ""))),
        None,
    )
    if not doc_prospecto:
        return

    try:
        pdf_bytes = _baixar_pdf_cached(doc_prospecto["valor"])
    except Exception as e:
        logger.warning("Falha ao baixar Prospecto Definitivo: %s", e)
        return

    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # Prefixos em vez de palavras completas para cobrir singular e plural:
            # comiss* → comissão, comissões, comissionamento
            # remuner* → remuneração, remunerando
            # fee cobre "fee flat", "fee de sucesso", "fee canal"
            _PREFIXOS_FEES = ("comiss", "remuner", "fee")
            paginas_relevantes = []
            for p in pdf.pages:
                txt = p.extract_text() or ""
                if any(pref in txt.lower() for pref in _PREFIXOS_FEES):
                    paginas_relevantes.append(txt)
            texto = "\n".join(paginas_relevantes)
    except Exception as e:
        logger.warning("Falha ao extrair texto do Prospecto Definitivo: %s", e)
        return

    fees = extrair_fees_prospecto(texto)

    for reg in elegiveis:
        reg["fee_flat"] = fees["fee_flat"]
        reg["fee_canal_distribuicao"] = fees["fee_canal_distribuicao"]
        reg["fee_canal_flat"] = fees["fee_canal_flat"]
        reg["fee_sucesso"] = fees["fee_sucesso"]


def _baixar_pdf_cached(valor: str) -> bytes:
    """
    Baixa um PDF com semáforo (máx. 3 simultâneos) e cache por UUID.
    Se o mesmo documento for solicitado por múltiplos workers, apenas um baixa
    e os demais recebem o resultado do cache.
    """
    uuid = valor.split(",")[0].strip()

    with _pdf_cache_lock:
        if uuid in _pdf_cache:
            return _pdf_cache[uuid]

    with _pdf_semaphore:
        with _pdf_cache_lock:
            if uuid in _pdf_cache:
                return _pdf_cache[uuid]

        pdf_bytes = baixar_pdf_documento(valor)

        with _pdf_cache_lock:
            _pdf_cache[uuid] = pdf_bytes

        return pdf_bytes


def _enriquecer_com_setor(registros: list[dict]) -> None:
    """
    Classifica o setor econômico de cada registro usando duas estratégias:

    1. Pré-classificação por instrumento (instantânea, sem API):
       CRA → "Agronegócio"  |  CRI → "Imobiliário"

    2. Lookup por CNPJ via BrasilAPI para os demais instrumentos.
       Extrai o CNPJ do texto bruto da emissão (campo de devedores / nome emissor)
       e consulta o CNAE principal da empresa.

    Modifica a lista de registros in-place. Setor fica None quando não identificado.
    """
    for reg in registros:
        if reg.get("setor") is not None:
            continue

        nome_vm = (reg.get("valor_mobiliario") or "").lower()

        # Classificação estrutural por instrumento (não requer API)
        if "agroneg" in nome_vm:
            reg["setor"] = "Agronegócio"
            continue
        if "imobiliár" in nome_vm or "imobiliar" in nome_vm:
            reg["setor"] = "Real Estate"
            continue

        # Classificação por CNPJ via BrasilAPI
        texto_cnpj = reg.get("_texto_busca_cnpj") or ""
        if texto_cnpj:
            reg["setor"] = classificar_setor([texto_cnpj])


def _processar_emissao(item: dict, buscar_fees: bool) -> tuple[list[dict], list[dict]]:
    """
    Executa busca de detalhes via API + enrichment de PDFs em um único worker thread.
    Mover o enrichment para cá (em vez do loop principal) permite que múltiplas
    emissões façam downloads de PDF em paralelo.
    """
    requerimento, inf_oferta, participantes, historico, documentos, erros = _buscar_detalhes_emissao(item)
    registros: list[dict] = []
    try:
        registros = extrair_registros(
            item_listagem=item,
            requerimento=requerimento,
            inf_oferta=inf_oferta,
            participantes=participantes,
            historico=historico,
        )
        _enriquecer_com_setor(registros)
        _enriquecer_com_pdf(registros, documentos)
        if buscar_fees:
            _enriquecer_com_prospecto(registros, documentos)
    except Exception as e:
        id_req = item.get("idRequerimento", "")
        logger.error("Erro ao extrair dados [%s]: %s", id_req, e, exc_info=True)
        erros.append({"campo": "extração", "erro": str(e)})
    return registros, erros


def _buscar_detalhes_emissao(
    item: dict,
) -> tuple[Optional[dict], list, list, list, list, list]:
    """
    Busca todos os detalhes de uma emissão em paralelo.
    As 5 chamadas são independentes entre si e disparadas simultaneamente.

    Returns:
        (requerimento, inf_oferta, participantes, historico, documentos, erros)
    """
    id_req = item.get("idRequerimento", "")

    tarefas = {
        "requerimento":    lambda: buscar_requerimento(id_req),
        "infOferta":       lambda: buscar_inf_oferta(id_req),
        "participantes":   lambda: buscar_participantes(id_req),
        "historicoStatus": lambda: buscar_historico_status(id_req),
        "documentos":      lambda: buscar_documentos_publicados(id_req),
    }

    resultados: dict = {campo: None for campo in tarefas}
    erros: list = []

    with ThreadPoolExecutor(max_workers=5) as sub_exec:
        future_to_campo = {sub_exec.submit(func): campo for campo, func in tarefas.items()}

        for future in as_completed(future_to_campo):
            campo = future_to_campo[future]
            try:
                resultados[campo] = future.result()
            except CVMApiError as e:
                logger.warning("Falha em %s [%s]: %s", campo, id_req, e)
                if campo != "documentos":  # documentos é não-crítico
                    erros.append({"campo": campo, "erro": _resumo_erro(e)})
            except Exception as e:
                logger.warning("Erro inesperado em %s [%s]: %s", campo, id_req, e)
                if campo != "documentos":
                    erros.append({"campo": campo, "erro": str(e)})

    return (
        resultados["requerimento"],
        resultados["infOferta"] or [],
        resultados["participantes"] or [],
        resultados["historicoStatus"] or [],
        resultados["documentos"] or [],
        erros,
    )


def _enriquecer_com_pdf(registros: list[dict], documentos: list[dict]) -> None:
    """
    Enriquece registros com dados do Anúncio de Encerramento quando campos estão ausentes.

    Baixa o PDF apenas se houver campos faltantes (rating, agencia_rating ou setor).
    Modifica a lista de registros in-place.
    """
    if not registros or not documentos:
        return

    precisa_rating = any(not r.get("rating") for r in registros)
    precisa_agencia = any(not r.get("agencia_rating") for r in registros)
    precisa_setor = any(r.get("setor") is None for r in registros)

    if not (precisa_rating or precisa_agencia or precisa_setor):
        return

    doc_enc = next(
        (d for d in documentos if "Encerramento" in d.get("nome", "")),
        None,
    )
    if not doc_enc:
        return

    try:
        pdf_bytes = _baixar_pdf_cached(doc_enc["valor"])
    except Exception as e:
        logger.warning("Falha ao baixar PDF do Anúncio de Encerramento: %s", e)
        return

    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # Rating aparece sempre na página 1; setor geralmente também
            paginas = pdf.pages[:3]
            texto = "\n".join(p.extract_text() or "" for p in paginas)
    except Exception as e:
        logger.warning("Falha ao extrair texto do PDF de encerramento: %s", e)
        return

    dados = extrair_dados_anuncio_encerramento(texto)

    for reg in registros:
        if precisa_rating and not reg.get("rating") and dados["rating"]:
            reg["rating"] = dados["rating"]
        if precisa_agencia and not reg.get("agencia_rating") and dados["agencia_rating"]:
            reg["agencia_rating"] = dados["agencia_rating"]
        if precisa_setor and reg.get("setor") is None and dados["setor"]:
            reg["setor"] = dados["setor"]
