"""
Agente analista de emissões CVM.

Faz upload do Excel gerado pelo scraper e permite fazer perguntas
sobre os dados usando Claude + code_execution (pandas roda no servidor).

Uso:
    python agente.py                          # análise padrão completa
    python agente.py --xlsx ../meu_arquivo.xlsx   # especifica outro arquivo
    python agente.py --chat                   # modo interativo (perguntas livres)
"""

import argparse
import os
import sys

import anthropic

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """Você é um analista especializado em mercado de capitais brasileiro,
com foco em emissões de CRI, CRA e Debêntures registradas no portal SRE da CVM.

Você tem acesso a uma planilha Excel com dados de emissões públicas. Use a ferramenta
de execução de código (Python/pandas) para analisar os dados e responder às perguntas.

Regras:
- Sempre use código para responder perguntas numéricas — nunca invente números
- Formate valores monetários em R$ com separador de milhar (ex: R$ 1.250.000.000)
- Datas no formato DD/MM/AAAA
- Respostas em português
- Seja direto e objetivo; use tabelas quando ajudar na leitura
- O arquivo está montado em /mnt/user_data/uploads/<nome_do_arquivo>
"""

ANALISES_PADRAO = [
    "Quantas emissões e séries existem no total? Mostre um resumo por tipo de instrumento (Debêntures, CRI, CRA).",
    "Quais foram as 10 maiores emissões por volume da série? Mostre emissor, instrumento, volume e data de encerramento.",
    "Compare a taxa média (Taxa Final) entre Debêntures, CRI e CRA. Mostre também mediana e quantidade de registros com taxa preenchida.",
    "Quantas emissões são incentivadas (Lei 12.431)? Mostre o percentual por instrumento.",
    "Quais são os 10 coordenadores que lideraram mais operações? Inclua o volume total coordenado.",
    "Gere um resumo executivo do período coberto pela planilha: volume total emitido, principais instrumentos, tendências observadas.",
]


# ---------------------------------------------------------------------------
# Upload do arquivo
# ---------------------------------------------------------------------------

def fazer_upload(client: anthropic.Anthropic, caminho_xlsx: str) -> str:
    """Faz upload do Excel e retorna o file_id."""
    print(f"Fazendo upload de: {os.path.basename(caminho_xlsx)}")
    with open(caminho_xlsx, "rb") as f:
        arquivo = client.beta.files.upload(
            file=(os.path.basename(caminho_xlsx), f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
    print(f"Upload concluído — file_id: {arquivo.id}\n")
    return arquivo.id


# ---------------------------------------------------------------------------
# Chamada ao Claude
# ---------------------------------------------------------------------------

def perguntar(
    client: anthropic.Anthropic,
    file_id: str,
    pergunta: str,
    historico: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """
    Envia uma pergunta ao Claude com o arquivo Excel disponível.
    Retorna (resposta_texto, historico_atualizado).
    """
    if historico is None:
        historico = []

    # Monta a mensagem do usuário com referência ao arquivo
    conteudo_usuario: list = []

    # Na primeira mensagem, inclui o arquivo; nas seguintes, só o texto
    if not historico:
        conteudo_usuario.append({
            "type": "document",
            "source": {"type": "file", "file_id": file_id},
            "title": "Planilha de Emissões CVM",
        })

    conteudo_usuario.append({"type": "text", "text": pergunta})

    historico.append({"role": "user", "content": conteudo_usuario})

    # Streaming para não dar timeout em análises longas
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=8096,
        system=SYSTEM_PROMPT,
        messages=historico,
        tools=[{"type": "code_execution_20260120", "name": "code_execution"}],
        betas=["files-api-2025-04-14"],
    ) as stream:
        resposta_completa = stream.get_final_message()

    # Extrai o texto da resposta
    texto = "\n".join(
        bloco.text
        for bloco in resposta_completa.content
        if bloco.type == "text"
    )

    # Atualiza histórico para multi-turn
    historico.append({"role": "assistant", "content": resposta_completa.content})

    return texto, historico


# ---------------------------------------------------------------------------
# Modos de execução
# ---------------------------------------------------------------------------

def modo_analise_padrao(client: anthropic.Anthropic, file_id: str) -> None:
    """Roda as análises pré-definidas em sequência."""
    print("=" * 60)
    print("ANÁLISE PADRÃO DE EMISSÕES CVM")
    print("=" * 60)

    historico: list[dict] = []

    for i, pergunta in enumerate(ANALISES_PADRAO, 1):
        print(f"\n[{i}/{len(ANALISES_PADRAO)}] {pergunta}")
        print("-" * 60)

        resposta, historico = perguntar(client, file_id, pergunta, historico)
        print(resposta)

    print("\n" + "=" * 60)
    print("Análise concluída.")


def modo_chat(client: anthropic.Anthropic, file_id: str) -> None:
    """Modo interativo — o usuário digita perguntas livremente."""
    print("=" * 60)
    print("CHAT COM OS DADOS CVM")
    print("Digite sua pergunta ou 'sair' para encerrar.")
    print("=" * 60)

    historico: list[dict] = []

    while True:
        try:
            pergunta = input("\nVocê: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEncerrando.")
            break

        if not pergunta:
            continue
        if pergunta.lower() in ("sair", "exit", "quit"):
            print("Encerrando.")
            break

        print("\nClaude: ", end="", flush=True)
        resposta, historico = perguntar(client, file_id, pergunta, historico)
        print(resposta)


# ---------------------------------------------------------------------------
# Limpeza
# ---------------------------------------------------------------------------

def deletar_arquivo(client: anthropic.Anthropic, file_id: str) -> None:
    """Remove o arquivo do servidor após o uso."""
    try:
        client.beta.files.delete(file_id)
        print(f"\nArquivo removido do servidor ({file_id}).")
    except Exception:
        pass  # não crítico


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Agente analista de emissões CVM")
    parser.add_argument(
        "--xlsx",
        default=None,
        help="Caminho para o arquivo Excel (padrão: busca automaticamente)",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Modo interativo (perguntas livres)",
    )
    parser.add_argument(
        "--manter-arquivo",
        action="store_true",
        help="Não deleta o arquivo do servidor após o uso",
    )
    args = parser.parse_args()

    # Resolve o caminho do Excel
    if args.xlsx:
        caminho_xlsx = args.xlsx
    else:
        # Busca automaticamente na pasta do projeto
        pasta_projeto = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidatos = [
            os.path.join(pasta_projeto, "emissoes_cvm_dez25_abr26.xlsx"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "emissoes_cvm_deb_cri_cra_out24_mar25.xlsx"),
        ]
        caminho_xlsx = next((c for c in candidatos if os.path.exists(c)), None)
        if not caminho_xlsx:
            print("Erro: nenhum arquivo Excel encontrado. Use --xlsx para especificar o caminho.")
            sys.exit(1)

    if not os.path.exists(caminho_xlsx):
        print(f"Erro: arquivo não encontrado: {caminho_xlsx}")
        sys.exit(1)

    # Verifica a API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Erro: variável de ambiente ANTHROPIC_API_KEY não definida.")
        print("Configure com: export ANTHROPIC_API_KEY='sua-chave-aqui'")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    file_id = fazer_upload(client, caminho_xlsx)

    try:
        if args.chat:
            modo_chat(client, file_id)
        else:
            modo_analise_padrao(client, file_id)
    finally:
        if not args.manter_arquivo:
            deletar_arquivo(client, file_id)


if __name__ == "__main__":
    main()
