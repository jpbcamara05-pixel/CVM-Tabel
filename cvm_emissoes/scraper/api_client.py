"""
Cliente para a API REST do portal SRE da CVM.

Endpoints base: https://web.cvm.gov.br/sre-publico-cvm/rest/
"""

import logging
import threading
import time
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Sessões por thread — permite requisições paralelas sem compartilhar estado
_thread_local = threading.local()

BASE_URL = "https://web.cvm.gov.br/sre-publico-cvm"
SITE_PUBLICO = f"{BASE_URL}/rest/sitePublico/"
VALOR_MOBILIARIO = f"{BASE_URL}/rest/valorMobiliario/"
STATUS_REST = f"{BASE_URL}/rest/status/"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Delay entre requisições para não sobrecarregar o servidor da CVM
REQUEST_DELAY_SECONDS = 0.2

# Quantas vezes tentar recriar a sessão (novo JSESSIONID) antes de desistir
MAX_SESSION_RESETS = 2


def _build_session() -> requests.Session:
    """
    Cria uma nova sessão HTTP com retry automático para falhas transitórias.

    Nota sobre backoff_factor: urllib3 usa {backoff_factor} * (2 ** (tentativa - 1)).
    Com backoff_factor=2: esperas de 2s, 4s, 8s entre tentativas — adequado para
    um servidor que retorna 500 por sobrecarga temporária.
    """
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=3,
        status_forcelist=[429, 502, 503, 504],  # 500 tratado manualmente (ver _request)
        allowed_methods=["GET", "POST"],
        raise_on_status=False,  # deixa _request decidir o que fazer com o status
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session


def get_session() -> requests.Session:
    """Retorna a sessão HTTP do thread atual (thread-safe para uso paralelo)."""
    if not hasattr(_thread_local, "session") or _thread_local.session is None:
        _thread_local.session = _build_session()
    return _thread_local.session


def reset_session() -> None:
    """
    Descarta a sessão do thread atual e força criação de uma nova.
    Útil quando o JSESSIONID expirou no servidor (retorna 500 em apps Java EE).
    """
    _thread_local.session = None
    logger.info("Sessão HTTP do thread resetada — nova sessão será criada na próxima requisição.")


class CVMApiError(Exception):
    """
    Erro da API da CVM com informações úteis para diagnóstico.
    Distingue falhas temporárias do servidor de erros permanentes.
    """
    def __init__(self, message: str, status_code: Optional[int] = None, url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(message)

    @property
    def is_server_error(self) -> bool:
        return self.status_code is not None and self.status_code >= 500

    @property
    def is_client_error(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500


def _request(method: str, url: str, timeout: int = 30, **kwargs) -> Any:
    """
    Executa uma requisição HTTP com:
    - delay entre chamadas
    - tratamento explícito de HTTP 500 (recria sessão e tenta novamente)
    - mensagens de erro legíveis que indicam origem externa
    """
    time.sleep(REQUEST_DELAY_SECONDS)

    for tentativa in range(1 + MAX_SESSION_RESETS):
        try:
            resp = get_session().request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.ConnectionError as e:
            # MaxRetryError (urllib3) é encapsulado aqui — esgotou as tentativas internas
            raise CVMApiError(
                f"Falha de conexão com o servidor da CVM após múltiplas tentativas. "
                f"O portal pode estar temporariamente indisponível. URL: {url}",
                url=url,
            ) from e
        except requests.exceptions.Timeout:
            raise CVMApiError(
                f"Timeout ao aguardar resposta da CVM (>{timeout}s). URL: {url}",
                url=url,
            ) from None

        if resp.status_code == 500:
            # HTTP 500 em app Java EE frequentemente indica sessão expirada.
            # Recriar a sessão (novo JSESSIONID) e tentar novamente.
            if tentativa < MAX_SESSION_RESETS:
                logger.warning(
                    "HTTP 500 recebido de %s (tentativa %d/%d). "
                    "Recriando sessão e aguardando antes de tentar novamente...",
                    url, tentativa + 1, MAX_SESSION_RESETS + 1,
                )
                reset_session()
                time.sleep(3 * (tentativa + 1))  # backoff: 3s, 6s
                continue
            else:
                raise CVMApiError(
                    f"O servidor da CVM retornou erro interno (HTTP 500) após "
                    f"{MAX_SESSION_RESETS + 1} tentativas. "
                    f"Isso é um problema no servidor externo — tente novamente mais tarde. "
                    f"URL: {url}",
                    status_code=500,
                    url=url,
                )

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise CVMApiError(
                f"Erro HTTP {resp.status_code} na API da CVM. URL: {url}",
                status_code=resp.status_code,
                url=url,
            ) from e

        return resp.json()

    # Nunca deve chegar aqui, mas satisfaz o type checker
    raise CVMApiError(f"Falha desconhecida ao acessar {url}", url=url)


def _get(url: str, timeout: int = 30) -> Any:
    return _request("GET", url, timeout=timeout)


def _get_bytes(url: str, timeout: int = 60) -> bytes:
    """GET que retorna bytes brutos (para download de PDF)."""
    time.sleep(REQUEST_DELAY_SECONDS)
    for tentativa in range(1 + MAX_SESSION_RESETS):
        try:
            resp = get_session().get(url, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise CVMApiError(f"Falha de conexão ao baixar arquivo. URL: {url}", url=url) from e
        except requests.exceptions.Timeout:
            raise CVMApiError(f"Timeout ao baixar arquivo (>{timeout}s). URL: {url}", url=url) from None

        if resp.status_code == 500 and tentativa < MAX_SESSION_RESETS:
            reset_session()
            time.sleep(3 * (tentativa + 1))
            continue

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise CVMApiError(f"Erro HTTP {resp.status_code} ao baixar arquivo. URL: {url}",
                              status_code=resp.status_code, url=url) from e
        return resp.content

    raise CVMApiError(f"Falha desconhecida ao baixar {url}", url=url)


def _post(url: str, payload: dict, timeout: int = 30) -> Any:
    return _request("POST", url, timeout=timeout, json=payload)


# ---------------------------------------------------------------------------
# Dados de referência
# ---------------------------------------------------------------------------

_cache_valores_mobiliarios: Optional[list] = None


def listar_valores_mobiliarios() -> list[dict]:
    """Retorna a lista de valores mobiliários disponíveis para filtro (cacheado em memória)."""
    global _cache_valores_mobiliarios
    if _cache_valores_mobiliarios is not None:
        return _cache_valores_mobiliarios
    payload = {
        "filtro": {"opa": False, "tipoDeConsulta": "OFERTA_DISTRIBUICAO"},
        "paginacao": None,
    }
    data = _post(f"{VALOR_MOBILIARIO}pesquisar", payload)
    _cache_valores_mobiliarios = data.get("registros", [])
    return _cache_valores_mobiliarios


def _resolver_valor_mobiliario(nome: str) -> Optional[dict]:
    """
    Resolve o nome de um valor mobiliário para o objeto completo da API.
    A API só filtra corretamente quando recebe o objeto VM inteiro (com oid, codigo, etc.).
    """
    vms = listar_valores_mobiliarios()
    nome_lower = nome.lower().strip()
    # Busca exata
    for vm in vms:
        if vm.get("nome", "").lower().strip() == nome_lower:
            return vm
    # Busca parcial como fallback
    for vm in vms:
        if nome_lower in vm.get("nome", "").lower():
            return vm
    return None


def listar_status() -> list[dict]:
    """Retorna a lista de status disponíveis para filtro."""
    payload = {
        "filtro": {"faseMinima": "PROCESSO", "parametrosOrdenacao": []},
        "paginacao": None,
    }
    data = _post(f"{STATUS_REST}pesquisar", payload)
    registros = data.get("registros", [])
    # Deduplica por statusExterno (mesmo comportamento da UI)
    seen = set()
    result = []
    for r in registros:
        key = r.get("statusExterno")
        if key and key not in seen:
            seen.add(key)
            result.append(r)
    return sorted(result, key=lambda x: x.get("statusExterno", ""))


# ---------------------------------------------------------------------------
# Consulta de listagem
# ---------------------------------------------------------------------------

def pesquisar_detalhado(
    data_inicio: str,
    data_fim: str,
    valor_mobiliario_nome: Optional[str] = None,
    status: Optional[str] = None,
    pagina: int = 1,
    tamanho_pagina: int = 50,
) -> dict:
    """
    Pesquisa emissões com paginação.

    Args:
        data_inicio: Formato DD/MM/AAAA
        data_fim:    Formato DD/MM/AAAA
        valor_mobiliario_nome: Nome exato do valor mobiliário (ex: "Debêntures")
        status: Status externo (ex: "Oferta Encerrada")
        pagina: Número da página (base 1)
        tamanho_pagina: Quantidade de registros por página (max recomendado: 100)
    """
    filtro: dict[str, Any] = {
        "tipoOferta": "OFERTA_REGULAR",
        "pagina": pagina,
        "tamanhoPagina": str(tamanho_pagina),
    }

    if data_inicio or data_fim:
        filtro["periodoCriacaoProcesso"] = {"de": data_inicio, "ate": data_fim}

    if valor_mobiliario_nome:
        vm_obj = _resolver_valor_mobiliario(valor_mobiliario_nome)
        if vm_obj:
            filtro["valorMobiliario"] = vm_obj
        else:
            # Fallback: passa apenas o nome (pode não filtrar corretamente)
            filtro["valorMobiliario"] = {"nome": valor_mobiliario_nome}

    if status:
        filtro["status"] = status

    return _post(f"{SITE_PUBLICO}pesquisar/detalhado", filtro)


def pesquisar_todas_paginas(
    data_inicio: str,
    data_fim: str,
    valor_mobiliario_nome: Optional[str] = None,
    status: Optional[str] = None,
    tamanho_pagina: int = 100,
    progresso_callback=None,
) -> list[dict]:
    """
    Percorre todas as páginas da pesquisa e retorna a lista completa de emissões.

    Args:
        progresso_callback: Função opcional (pagina_atual, total_paginas, total_registros)
    """
    resultado = pesquisar_detalhado(
        data_inicio, data_fim, valor_mobiliario_nome, status, pagina=1,
        tamanho_pagina=tamanho_pagina,
    )
    registros = resultado.get("registros", [])
    total = resultado.get("totalRegistros", 0)

    if total == 0:
        return []

    total_paginas = -(-total // tamanho_pagina)  # ceiling division

    if progresso_callback:
        progresso_callback(1, total_paginas, total)

    pagina = 2
    while pagina <= total_paginas:
        resultado = pesquisar_detalhado(
            data_inicio, data_fim, valor_mobiliario_nome, status,
            pagina=pagina, tamanho_pagina=tamanho_pagina,
        )
        registros.extend(resultado.get("registros", []))
        if progresso_callback:
            progresso_callback(pagina, total_paginas, total)
        pagina += 1

    return registros


# ---------------------------------------------------------------------------
# Detalhe de cada emissão
# ---------------------------------------------------------------------------

def buscar_requerimento(id_requerimento: str) -> dict:
    """
    Retorna estrutura completa do requerimento: séries, lotes inicial/final,
    campos cadastrados (taxa, amortização, data emissão, etc.) e dados de colocação.
    """
    return _get(f"{SITE_PUBLICO}pesquisar/requerimento/{id_requerimento}")


def buscar_inf_oferta(id_requerimento: str) -> list[dict]:
    """
    Retorna campos adicionais da oferta em formato lista de {campoNome, valor}.
    Inclui: avaliador de risco, tipo de lastro, identificação de devedores,
    público alvo, regime de distribuição, etc.
    """
    return _get(f"{SITE_PUBLICO}pesquisar/infOferta/{id_requerimento}")


def buscar_participantes(id_requerimento: str) -> list[dict]:
    """
    Retorna lista de participantes com tipo: EMISSOR, REQUERENTE, OFERTANTE, COORDENADOR.
    """
    return _get(f"{SITE_PUBLICO}pesquisar/participantes/{id_requerimento}")


def buscar_historico_status(id_requerimento: str) -> list[dict]:
    """
    Retorna histórico de status com data/hora.
    """
    return _get(f"{SITE_PUBLICO}pesquisar/historicoStatus/{id_requerimento}")


def buscar_informacoes_gerais(id_requerimento: str) -> dict:
    """
    Retorna informações gerais da oferta (complemento rápido sem séries).
    """
    return _get(f"{SITE_PUBLICO}pesquisar/informacoesGerais/{id_requerimento}")


def buscar_documentos_publicados(id_requerimento: str) -> list[dict]:
    """
    Retorna lista de documentos publicados de uma oferta.
    Cada item: {idDocumento, nome, data, hora, extencao, tamanho, valor}.
    'valor' é o UUID usado para download via baixar_pdf_documento.
    """
    result = _get(f"{SITE_PUBLICO}pesquisar/documentosPublicados/{id_requerimento}")
    return result if isinstance(result, list) else []


def baixar_pdf_documento(valor: str) -> bytes:
    """Baixa o conteúdo binário de um documento pelo seu UUID ('valor').
    Quando o campo 'valor' contém UUIDs duplicados separados por vírgula, usa apenas o primeiro.
    """
    uuid = valor.split(",")[0].strip()
    return _get_bytes(f"{BASE_URL}/rest/download/{uuid}")


def montar_link_cvm(id_requerimento: str) -> str:
    return f"{BASE_URL}/#/oferta-publica/{id_requerimento}"
