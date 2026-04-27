"""
Interface Streamlit para coleta e exportação de emissões públicas da CVM.

Uso:
    streamlit run app.py
"""

import logging
import os
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st
from google import genai
from google.genai import types as genai_types

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Importações locais
from scraper.api_client import CVMApiError, listar_status, listar_valores_mobiliarios
from scraper.collector import ResultadoColeta, coletar
from exporter.excel import exportar_excel, COLUNAS_EMISSOES

# ---------------------------------------------------------------------------
# Configuração Gemini
# ---------------------------------------------------------------------------

def _get_google_key() -> Optional[str]:
    try:
        return st.secrets.get("GOOGLE_API_KEY")
    except Exception:
        return os.environ.get("GOOGLE_API_KEY")


def _responder_gemini(df: pd.DataFrame, historico: list[dict]) -> str:
    key = _get_google_key()
    if not key:
        return "⚠️ Chave do Google AI não configurada."

    client = genai.Client(api_key=key)

    # Contexto: primeiras 500 linhas do df como CSV
    csv_ctx = df.head(500).to_csv(index=False)
    n_total = len(df)
    system_prompt = (
        f"Você é um assistente especialista em mercado de capitais brasileiro. "
        f"O usuário coletou dados de emissões públicas da CVM. "
        f"Abaixo estão os dados em formato CSV ({n_total} séries, mostrando até 500):\n\n"
        f"```csv\n{csv_ctx}\n```\n\n"
        f"Responda em português, de forma objetiva e estruturada. "
        f"Use tabelas markdown quando apresentar listas de dados."
    )

    # Monta histórico no formato google-genai
    contents = []
    for msg in historico[:-1]:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=msg["content"])]))
    # Última mensagem (pergunta atual) inclui o contexto dos dados
    pergunta_atual = historico[-1]["content"]
    contents.append(genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"{system_prompt}\n\nPergunta: {pergunta_atual}")]
    ))

    try:
        resposta = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=contents,
        )
        return resposta.text
    except Exception as e:
        msg = str(e)
        if "429" in msg or "quota" in msg.lower() or "ResourceExhausted" in type(e).__name__:
            return (
                "⚠️ **Limite de requisições atingido** — a cota gratuita do Gemini foi esgotada. "
                "Aguarde alguns minutos e tente novamente."
            )
        return f"❌ Erro ao consultar o assistente: {e}"

# ---------------------------------------------------------------------------
# Configuração da página
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CVM – Emissões Públicas",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 Base de Emissões Públicas – CVM")
st.caption(
    "Consulta diretamente o portal SRE da CVM • "
    "Dados extraídos sem modificações • "
    "Campos não encontrados ficam em branco"
)

# ---------------------------------------------------------------------------
# Cache para dados de referência (atualiza a cada hora)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def _carregar_valores_mobiliarios():
    try:
        vms = listar_valores_mobiliarios()
        return sorted([v["nome"] for v in vms if v.get("nome")], key=str.lower)
    except Exception as e:
        logger.warning(f"Não foi possível carregar valores mobiliários: {e}")
        return []


@st.cache_data(ttl=3600)
def _carregar_status():
    try:
        statuses = listar_status()
        return [s["statusExterno"] for s in statuses if s.get("statusExterno")]
    except Exception as e:
        logger.warning(f"Não foi possível carregar status: {e}")
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gerar_chunks(data_inicio: date, data_fim: date, dias_por_chunk: int = 90) -> list[tuple[date, date]]:
    """Divide o período em chunks para coleta incremental e mais robusta."""
    chunks = []
    atual = data_inicio
    while atual <= data_fim:
        fim_chunk = min(atual + timedelta(days=dias_por_chunk - 1), data_fim)
        chunks.append((atual, fim_chunk))
        atual = fim_chunk + timedelta(days=1)
    return chunks


def _build_df_preview(registros: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(registros)
    df = df.drop(columns=["_id_requerimento"], errors="ignore")
    rename_map = {k: v for k, v in COLUNAS_EMISSOES.items() if k in df.columns}
    return df.rename(columns=rename_map)


# ---------------------------------------------------------------------------
# Sidebar — Filtros
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("🔍 Filtros")

    # Período (data de requerimento)
    st.subheader("Data de Requerimento")
    hoje = date.today()
    um_ano_atras = hoje - timedelta(days=365)

    col_de, col_ate = st.columns(2)
    with col_de:
        data_inicio = st.date_input(
            "De", value=um_ano_atras, format="DD/MM/YYYY",
            help="Data de requerimento da oferta — início do período"
        )
    with col_ate:
        data_fim = st.date_input(
            "Até", value=hoje, format="DD/MM/YYYY",
            help="Data de requerimento da oferta — fim do período"
        )

    # Instrumento / Valor Mobiliário
    st.subheader("Instrumento")
    lista_vms = _carregar_valores_mobiliarios()
    vms_selecionados = st.multiselect(
        "Valor Mobiliário",
        lista_vms,
        default=[],
        placeholder="Todos os instrumentos",
        help="Selecione um ou mais instrumentos. Vazio = todos.",
    )

    # Status
    st.subheader("Status")
    lista_status = _carregar_status()
    opcoes_status = ["(Todos)"] + lista_status
    status_selecionado = st.selectbox(
        "Status da Oferta",
        opcoes_status,
        index=0,
    )

    st.divider()

    # Botão de consulta
    btn_consultar = st.button(
        "🔎 Consultar",
        use_container_width=True,
        type="primary",
    )

    st.divider()
    st.caption(
        "**Fonte:** [Portal SRE – CVM](https://web.cvm.gov.br/sre-publico-cvm/)\n\n"
        "**Período:** filtro por **data de requerimento** da oferta.\n\n"
        "**Output:** apenas ofertas com status **Oferta Encerrada**.\n\n"
        "**Atenção:** campos ausentes no portal ficam em branco na base. "
        "Nenhum dado é inventado ou estimado."
    )

# ---------------------------------------------------------------------------
# Estado da sessão
# ---------------------------------------------------------------------------

for _key in ("resultado", "df_preview", "erros_chunks", "chunks_total", "filtros_usados"):
    if _key not in st.session_state:
        st.session_state[_key] = None

# ---------------------------------------------------------------------------
# Execução da consulta
# ---------------------------------------------------------------------------

if btn_consultar:
    if data_inicio > data_fim:
        st.error("A data de início não pode ser posterior à data de término.")
    else:
        data_inicio_fmt = data_inicio.strftime("%d/%m/%Y")
        data_fim_fmt = data_fim.strftime("%d/%m/%Y")
        # Nenhum instrumento selecionado = coleta todos (None = sem filtro de VM)
        vms_filtro: list[Optional[str]] = vms_selecionados if vms_selecionados else [None]
        status_filtro = status_selecionado if status_selecionado != "(Todos)" else None

        # Limpa estado anterior
        for _k in ("resultado", "df_preview", "erros_chunks", "chunks_total"):
            st.session_state[_k] = None
        st.session_state["filtros_usados"] = {
            "data_inicio": data_inicio_fmt,
            "data_fim": data_fim_fmt,
            "vms": vms_selecionados,
            "status": status_filtro,
        }

        # Divide período em chunks para maior robustez
        # 365 dias por chunk: evita listagens duplas sem perder a proteção contra falhas
        chunks = _gerar_chunks(data_inicio, data_fim, dias_por_chunk=365)
        num_chunks = len(chunks)
        # Total de iterações = chunks × instrumentos
        total_iteracoes = num_chunks * len(vms_filtro)
        st.session_state["chunks_total"] = total_iteracoes

        # --- UI de progresso ---
        st.subheader("⏳ Progresso da Coleta")
        texto_status = st.empty()
        barra = st.progress(0)
        info_box = st.empty()
        info_box.info("Iniciando consulta...")

        # Resultado consolidado (acumula todos os chunks e instrumentos)
        resultado_consolidado = ResultadoColeta()
        erros_chunks: list[dict] = []
        i_iter = 0  # contador global de iterações para a barra de progresso

        for vm_filtro in vms_filtro:
            vm_label = vm_filtro or "Todos"

            for i_chunk, (chunk_de, chunk_ate) in enumerate(chunks):
                chunk_de_fmt = chunk_de.strftime("%d/%m/%Y")
                chunk_ate_fmt = chunk_ate.strftime("%d/%m/%Y")
                iter_atual = i_iter
                prefix = f"[{vm_label}] " if len(vms_filtro) > 1 else ""

                def _progresso(etapa, atual, total, mensagem="",
                               _i=iter_atual, _n=total_iteracoes, _p=prefix):
                    chunk_offset = _i / _n
                    chunk_scale = 1.0 / _n
                    pct = int((chunk_offset + (atual / max(total, 1)) * chunk_scale) * 100)
                    barra.progress(min(pct, 99))
                    texto_status.markdown(f"**{_p}{mensagem}**")

                try:
                    resultado_chunk = coletar(
                        data_inicio=chunk_de_fmt,
                        data_fim=chunk_ate_fmt,
                        valor_mobiliario_nome=vm_filtro,
                        status=status_filtro,
                        progresso_callback=_progresso,
                    )
                    # Merge no consolidado
                    resultado_consolidado.registros.extend(resultado_chunk.registros)
                    resultado_consolidado.erros.extend(resultado_chunk.erros)
                    resultado_consolidado.total_emissoes += resultado_chunk.total_emissoes
                    resultado_consolidado.total_series += resultado_chunk.total_series
                    resultado_consolidado.total_emissoes_pre_filtro += resultado_chunk.total_emissoes_pre_filtro
                    resultado_consolidado.total_series_pre_filtro += resultado_chunk.total_series_pre_filtro

                except CVMApiError as e:
                    logger.error("Falha na listagem [%s] %s–%s: %s", vm_label, chunk_de_fmt, chunk_ate_fmt, e)
                    erros_chunks.append({
                        "Instrumento": vm_label,
                        "Período": f"{chunk_de_fmt} – {chunk_ate_fmt}",
                        "Tipo": "Falha na listagem CVM",
                        "Erro": str(e),
                    })
                except Exception as e:
                    logger.error("Erro inesperado [%s] %s–%s", vm_label, chunk_de_fmt, chunk_ate_fmt, exc_info=True)
                    erros_chunks.append({
                        "Instrumento": vm_label,
                        "Período": f"{chunk_de_fmt} – {chunk_ate_fmt}",
                        "Tipo": "Erro inesperado",
                        "Erro": str(e),
                    })

                i_iter += 1

            # Salva estado parcial após cada chunk — nunca perde progresso
            st.session_state["resultado"] = resultado_consolidado
            st.session_state["erros_chunks"] = erros_chunks if erros_chunks else None

        # Constrói preview do DataFrame
        if resultado_consolidado.registros:
            st.session_state["df_preview"] = _build_df_preview(resultado_consolidado.registros)

        barra.progress(100)
        info_box.empty()
        texto_status.empty()

# ---------------------------------------------------------------------------
# Exibição dos resultados — persiste entre reruns via session_state
# ---------------------------------------------------------------------------

resultado = st.session_state.get("resultado")
df_preview = st.session_state.get("df_preview")
erros_chunks = st.session_state.get("erros_chunks")
chunks_total = st.session_state.get("chunks_total") or 1
filtros_usados = st.session_state.get("filtros_usados") or {}

has_data = resultado is not None and bool(resultado.registros)
query_done = resultado is not None or erros_chunks is not None

# ------------------------------------------------------------------
# Caso A: todos os chunks falharam — nenhum dado coletado
# ------------------------------------------------------------------
if not has_data and erros_chunks:
    st.divider()
    n_erros = len(erros_chunks)
    if n_erros == chunks_total:
        st.error(
            f"A coleta falhou em **todas as {n_erros} parte(s)** do período. "
            f"Nenhum dado foi coletado."
        )
    else:
        st.warning(
            f"**{n_erros} de {chunks_total} partes** do período falharam. "
            f"Nenhum dado passou pelo filtro de encerramento."
        )

    with st.expander("Detalhes das falhas por período"):
        st.dataframe(pd.DataFrame(erros_chunks), use_container_width=True)

    if st.button("🔄 Limpar e tentar novamente"):
        for _k in ("resultado", "df_preview", "erros_chunks", "chunks_total", "filtros_usados"):
            st.session_state[_k] = None
        st.rerun()

# ------------------------------------------------------------------
# Caso B: coleta concluída com resultados (totais ou parciais)
# ------------------------------------------------------------------
elif has_data and df_preview is not None:
    st.divider()

    # Aviso sobre chunks que falharam (resultados parciais)
    if erros_chunks:
        n_ok = chunks_total - len(erros_chunks)
        st.warning(
            f"**Atenção: dados parciais.** "
            f"{n_ok} de {chunks_total} partes coletadas com sucesso. "
            f"{len(erros_chunks)} período(s) falharam — veja detalhes abaixo."
        )
        with st.expander(f"⚠️ Períodos com falha ({len(erros_chunks)})"):
            st.dataframe(pd.DataFrame(erros_chunks), use_container_width=True)
            st.caption(
                "Os dados acima não foram incluídos na base. "
                "Tente consultar esses períodos separadamente."
            )
        st.divider()

    st.success(
        f"✅ **{resultado.total_emissoes}** emissões · "
        f"**{resultado.total_series}** séries"
        + (f" · {len(resultado.erros)} com erros parciais" if resultado.erros else "")
    )
    st.subheader(f"📋 Pré-visualização — {len(df_preview)} séries")

    # Métricas — separa falhas por tipo
    erros_totais_count = sum(
        1 for e in resultado.erros if e.get("tipo_falha") == "Total"
    )
    erros_parciais_count = sum(
        1 for e in resultado.erros if e.get("tipo_falha") == "Parcial"
    )
    tipos_vm = df_preview.get("Valor Mobiliário", pd.Series()).nunique()

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Emissões", resultado.total_emissoes)
    with col2:
        st.metric("Séries", resultado.total_series)
    with col3:
        st.metric("Tipos de VM", tipos_vm)
    with col4:
        st.metric("Erros Parciais", erros_parciais_count,
                  help="Emissão incluída, mas alguns campos falharam")
    with col5:
        st.metric("Falhas Totais", erros_totais_count,
                  help="Emissão sem nenhum dado — não incluída na base")

    MAX_PREVIEW = 200
    df_show = df_preview.head(MAX_PREVIEW)
    if len(df_preview) > MAX_PREVIEW:
        st.caption(f"*Mostrando as primeiras {MAX_PREVIEW} de {len(df_preview)} linhas.*")

    st.dataframe(
        df_show,
        use_container_width=True,
        height=400,
        column_config={
            "Link CVM": st.column_config.LinkColumn("Link CVM"),
            "Book Mercado (Qtd. VM)": st.column_config.NumberColumn(
                "Book Mercado (Qtd. VM)",
                help="Soma das QVM: pessoas naturais, clubes, fundos, previdência, "
                     "seguradoras e estrangeiros",
                format="%.4f",
            ),
            "Book Consórcio (Qtd. VM)": st.column_config.NumberColumn(
                "Book Consórcio (Qtd. VM)",
                help="Soma das QVM: intermediárias consórcio, inst. financeiras ligadas, "
                     "demais inst. financeiras, demais PJ ligadas e sócios/admins",
                format="%.4f",
            ),
        },
    )

    # Exportar
    st.divider()
    st.subheader("💾 Exportar")

    col_exp1, col_exp2 = st.columns([2, 1])
    with col_exp1:
        nome_arquivo = st.text_input(
            "Nome do arquivo",
            value=f"emissoes_cvm_{date.today().strftime('%Y%m%d')}.xlsx",
        )
    with col_exp2:
        st.write("")
        st.write("")
        btn_exportar = st.button("⬇️ Gerar Excel", use_container_width=True, type="primary")

    if btn_exportar:
        with st.spinner("Gerando arquivo Excel..."):
            try:
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                    tmp_path = tmp.name
                exportar_excel(
                    registros=resultado.registros,
                    erros=resultado.erros,
                    output_path=tmp_path,
                )
                with open(tmp_path, "rb") as f:
                    dados_excel = f.read()
                os.unlink(tmp_path)
                st.download_button(
                    label="📥 Clique para baixar o Excel",
                    data=dados_excel,
                    file_name=nome_arquivo,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Erro ao gerar Excel: {e}")
                logger.error("Erro ao exportar Excel", exc_info=True)

    # ------------------------------------------------------------------
    # Chatbot Gemini — análise dos dados coletados
    # ------------------------------------------------------------------
    st.divider()
    st.subheader("💬 Assistente de Análise")
    st.caption("Faça perguntas sobre os dados coletados em linguagem natural.")

    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = []

    # Limpa chat quando nova consulta é feita
    filtros_atuais = st.session_state.get("filtros_usados")
    if st.session_state.get("_chat_filtros") != filtros_atuais:
        st.session_state["chat_messages"] = []
        st.session_state["_chat_filtros"] = filtros_atuais

    # Exibe histórico
    for msg in st.session_state["chat_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input do usuário
    if prompt := st.chat_input("Ex: Qual o maior emissor? Liste as debêntures com vencimento em 2026."):
        st.session_state["chat_messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Analisando..."):
                resposta = _responder_gemini(df_preview, st.session_state["chat_messages"])
            st.markdown(resposta)
            st.session_state["chat_messages"].append({"role": "assistant", "content": resposta})

    if st.session_state["chat_messages"]:
        if st.button("🗑️ Limpar conversa", key="btn_limpar_chat"):
            st.session_state["chat_messages"] = []
            st.rerun()

    # Relatório detalhado de falhas por emissão
    if resultado.erros:
        st.divider()
        n_total_err = len(resultado.erros)
        with st.expander(
            f"⚠️ Relatório de falhas por emissão ({n_total_err} emissões com problemas)"
        ):
            # Separar em duas tabelas: falhas totais e parciais
            erros_df = pd.DataFrame(resultado.erros)

            # Falhas totais (emissão sem nenhum dado)
            mask_total = erros_df.get("tipo_falha", pd.Series()) == "Total"
            df_falhas_totais = erros_df[mask_total] if mask_total.any() else pd.DataFrame()
            df_falhas_parciais = erros_df[~mask_total] if (~mask_total).any() else pd.DataFrame()

            if not df_falhas_totais.empty:
                st.markdown("**Falhas totais** — emissão sem nenhum campo coletado (não incluída na base):")
                cols_show = ["numero_processo", "nome_emissor", "valor_mobiliario",
                             "data", "erros", "link_cvm"]
                cols_show = [c for c in cols_show if c in df_falhas_totais.columns]
                st.dataframe(
                    df_falhas_totais[cols_show],
                    use_container_width=True,
                    column_config={"link_cvm": st.column_config.LinkColumn("Link CVM")},
                )

            if not df_falhas_parciais.empty:
                st.markdown("**Falhas parciais** — emissão incluída com campos disponíveis:")
                cols_show = ["numero_processo", "nome_emissor", "valor_mobiliario",
                             "data", "erros", "link_cvm"]
                cols_show = [c for c in cols_show if c in df_falhas_parciais.columns]
                st.dataframe(
                    df_falhas_parciais[cols_show],
                    use_container_width=True,
                    column_config={"link_cvm": st.column_config.LinkColumn("Link CVM")},
                )

            st.caption(
                "Falhas parciais: emissões com alguns campos em branco por indisponibilidade da API. "
                "Falhas totais: dados completamente inacessíveis no momento da coleta."
            )

# ------------------------------------------------------------------
# Caso C: coleta concluída mas nenhum registro após pós-filtro
# ------------------------------------------------------------------
elif resultado is not None and not has_data and not erros_chunks:
    st.divider()
    pre = resultado.total_emissoes_pre_filtro
    di = filtros_usados.get("data_inicio", "")
    df_ = filtros_usados.get("data_fim", "")

    if pre > 0:
        st.warning(
            f"Nenhuma série com data de encerramento no período **{di} – {df_}**.\n\n"
            f"Foram coletadas **{pre}** emissões no pré-filtro (requerimentos), "
            f"mas nenhuma tinha `dataEncerramento` dentro do intervalo selecionado. "
            f"Tente ampliar o período ou verificar se as ofertas já foram encerradas."
        )
    else:
        st.info("Nenhuma emissão encontrada para os filtros informados.")

# ------------------------------------------------------------------
# Caso D: nenhuma consulta realizada ainda — tela inicial
# ------------------------------------------------------------------
elif not query_done:
    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Como usar")
        st.markdown("""
        1. Defina o **período de encerramento** na barra lateral
        2. Selecione o **tipo de instrumento** (Debêntures, CRI, CRA, etc.) ou deixe em branco para todos
        3. Opcionalmente filtre por **status** da oferta
        4. Clique em **Consultar**
        5. Aguarde a coleta — períodos longos são divididos automaticamente em trimestres
        6. Exporte para **Excel** com um clique

        > O filtro de período aplica-se à **data de requerimento** da oferta.
        > Apenas ofertas com status **Oferta Encerrada** são incluídas no resultado.
        """)

    with col_b:
        st.subheader("Campos coletados por série")
        st.markdown("""
        | Campo | Origem / Regra |
        |-------|----------------|
        | Status, datas | Listagem CVM |
        | Emissora / Devedora | Para securitizados: 1º devedor identificado |
        | **Público-Alvo** | infOferta → normalizado para Qualificado / Profissional / Geral |
        | **Taxa Teto** | Lote inicial → extraído como `indexador + spread` |
        | Taxa Final | Séries — lote final |
        | Amortização, Prazo | Campos cadastrados por série |
        | Rating, Agência | infOferta + campos da série |
        | Coordenadores | Lista de participantes |
        | Firme / Esforços | Regime de distribuição |
        | **Book Mercado (Qtd. VM)** | Soma das QVM: pessoas naturais, clubes, fundos, previdência, seguradoras, estrangeiros, demais inst. financeiras |
        | Book Consórcio | Sim/Não — instituições intermediárias no consórcio |
        | Link CVM | Link direto para auditoria |
        """)

    st.info(
        "⚡ A coleta usa diretamente a **API REST do portal SRE da CVM** — "
        "sem Playwright ou automação de browser. "
        "Períodos longos são divididos em trimestres para maior robustez."
    )
