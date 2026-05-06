# CVM Emissões — Base de Dados de Ofertas Públicas

Interface para montar base de dados de emissões públicas registradas no portal SRE da CVM,
com exportação para Excel.

---

## Requisitos

- Python 3.11 ou superior
- Conexão com internet (acesso ao portal da CVM)
- Chave de API do Google AI (`GOOGLE_API_KEY`) para o assistente de análise (Gemini)

---

## Instalação

```bash
# Clonar ou abrir a pasta do projeto
cd "CVM Tabel/cvm_emissoes"

# Criar e ativar virtualenv (recomendado)
python3 -m venv .venv
source .venv/bin/activate      # Mac/Linux
# .venv\Scripts\activate       # Windows

# Instalar dependências
pip install -r requirements.txt
```

---

## Configuração da chave do Google AI (Gemini)

O assistente de análise usa a API Gemini. Configure a chave de uma das formas abaixo:

**Opção 1 — variável de ambiente (recomendado para produção):**
```bash
export GOOGLE_API_KEY="sua_chave_aqui"
```

**Opção 2 — arquivo local (apenas para desenvolvimento, nunca versionar):**

Crie o arquivo `cvm_emissoes/.streamlit/secrets.toml` com o conteúdo:
```toml
GOOGLE_API_KEY = "sua_chave_aqui"
```

> ⚠️ **Nunca commite `secrets.toml` ou qualquer arquivo com chaves de API.**
> Esse arquivo já está listado no `.gitignore`. Obtenha sua chave em
> [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

---

## Execução

```bash
streamlit run app.py
```

A interface abrirá automaticamente no navegador em `http://localhost:8501`.

---

## Como usar

1. **Período** — selecione a data de início e fim do requerimento
2. **Instrumento** — escolha o tipo de valor mobiliário (Debêntures, CRI, CRA, etc.) ou deixe "Todos"
3. **Status** — filtre por status da oferta (Encerrada, Em Análise, etc.) ou deixe "Todos"
4. Clique em **Consultar** — a coleta percorre todas as páginas e entra em cada emissão individualmente
5. Após concluir, visualize a prévia e clique em **Gerar Excel** para baixar

---

## Estrutura do Projeto

```
cvm_emissoes/
├── app.py                          # Interface Streamlit
├── requirements.txt
├── .streamlit/
│   └── secrets.toml                # ⚠️ NÃO versionar — chaves locais
├── scraper/
│   ├── api_client.py               # Cliente HTTP para a API REST da CVM
│   ├── collector.py                # Orquestração: listagem + detalhamento
│   ├── extractor.py                # Transformação: dados brutos → registros por série
│   ├── fees_extractor.py           # Extração de fees do Prospecto Definitivo (PDF)
│   └── sector_classifier.py       # Classificação de setor via CNPJ + BrasilAPI
└── exporter/
    └── excel.py                    # Geração do arquivo Excel (.xlsx)
```

> Arquivos `.xlsx` são outputs gerados pela aplicação e não fazem parte do repositório.

---

## Arquitetura

### Camada de coleta — `api_client.py`

O portal SRE da CVM é uma SPA Angular que consome uma API REST interna descoberta
via análise do JavaScript bundle. **Não é necessário Playwright ou automação de browser.**

**Endpoints utilizados:**

| Endpoint | Método | Descrição |
|----------|--------|-----------|
| `rest/sitePublico/pesquisar/detalhado` | POST | Listagem paginada de emissões com filtros |
| `rest/sitePublico/pesquisar/requerimento/{id}` | GET | Estrutura completa: séries, lotes, taxas, amortização |
| `rest/sitePublico/pesquisar/infOferta/{id}` | GET | Campos adicionais: avaliador de risco, devedores, público alvo, regime |
| `rest/sitePublico/pesquisar/participantes/{id}` | GET | Coordenadores, emissor, ofertante |
| `rest/sitePublico/pesquisar/historicoStatus/{id}` | GET | Histórico de status com datas |
| `rest/valorMobiliario/pesquisar` | POST | Lista de tipos de instrumento para filtro |
| `rest/status/pesquisar` | POST | Lista de status disponíveis para filtro |

### Camada de extração — `extractor.py`

- Uma linha no Excel por **série** (não por emissão), pois CRI/CRA/FIDC frequentemente
  têm múltiplas séries com características diferentes (taxa, vencimento, amortização)
- Campos nunca inventados — ausentes ficam `None`/em branco no Excel
- Para instrumentos securitizados (CRI, CRA, FIDC), o campo `Emissora / Devedora` é
  preenchido com o **primeiro devedor/coobrigado identificado** no campo
  `Identificação dos devedores e coobrigados` da infOferta — não com a securitizadora

### Camada de exportação — `excel.py`

- Aba **"Emissões"**: base consolidada com todas as séries coletadas
- Aba **"Erros"**: log de emissões onde alguma chamada de API falhou
- Datas convertidas para tipo `datetime` no Excel (formato DD/MM/AAAA)
- Valores monetários convertidos para `float` com formatação numérica

---

## Colunas do Excel

| Coluna | Origem |
|--------|--------|
| Status | `statusDaOferta` da listagem |
| Data de Requerimento | Campo `data` da listagem |
| Data de Encerramento | `dadosColocacao.dataEncerramento` do requerimento |
| Emissora / Devedora | Devedor identificado (securitizados) ou nome do emissor |
| Data de Emissão | Campo `Data de emissão` dos campos cadastrados da série |
| Valor Mobiliário | `nomeValorMobiliario` da listagem |
| Incentivada | Campo `Título incentivado - Lei 12.431/11` da série |
| Nome | Número de registro da série |
| Público-Alvo | Campo `Público alvo` da infOferta |
| Volume da Série | `valorTotalLote` do lote final da série |
| Volume Inicial da Oferta | `valorTotalInicial` das informaçoes gerais |
| Volume Final da Oferta | `valorTotalFinal` das informações gerais |
| Prazo | Calculado: diferença entre data de emissão e vencimento |
| Amortização | Campo `Informações sobre amortização` da série |
| Taxa Teto | Campo `Informações sobre remuneração máxima` do lote inicial |
| Taxa Final | Campo `Informações sobre remuneração final` do lote final |
| Agência Avaliadora de Rating | Campo `Avaliador de risco` da infOferta |
| Rating | Campo `Avaliação de risco` dos campos da série |
| Firme do Sindicato | Campo `Regime de distribuição` da infOferta |
| Book Mercado | Derivado de `nomeTipoRequerimento` e `possuiBook` |
| Book Consórcio | Derivado de `dadosColocacao.nuIInstituicaoIntermediarias...` |
| Coordenadores | Participantes do tipo COORDENADOR e REQUERENTE |
| Link CVM | `https://web.cvm.gov.br/sre-publico-cvm/#/oferta-publica/{id}` |

---

## Limitações e pontos de manutenção

### 1. Campos opcionais por tipo de instrumento
Cada tipo de valor mobiliário (ações, debêntures, CRI, CRA, FIDC, FII, etc.) pode ter
campos diferentes nos `camposCadastrados`. A extração é genérica por nome do campo —
se a CVM alterar os nomes dos campos, será necessário atualizar os mapeamentos em
`extractor.py`.

### 2. Identificação de devedores em securitizações
O campo `Identificação dos devedores e coobrigados` pode ter múltiplos nomes
(pessoas físicas, holdings intermediárias, SPEs). O código captura apenas o **primeiro**
devedor listado como principal. Para casos complexos, o campo completo está disponível
para análise manual.

### 3. Book mercado / book consórcio
Esses campos são inferidos de `nomeTipoRequerimento` e `dadosColocacao`. Se a CVM
alterar a nomenclatura dos tipos de requerimento, revisar a lógica em `extractor.py`.

### 4. Emissões antigas (pré-2022) e ICVM 400
O dataset `oferta_distribuicao.csv` dos Dados Abertos cobre de 2022 em diante. Para
emissões mais antigas registradas sob ICVM 400, o portal SRE pode ter estrutura diferente
ou dados menos completos. Testar em caso de uso com períodos anteriores.

### 5. Rate limiting
O cliente faz uma pausa de 0,3s entre requisições. Se a CVM implementar rate limiting
mais agressivo, aumentar `REQUEST_DELAY_SECONDS` em `api_client.py`.

### 6. Mudança de layout da API
O portal é uma aplicação interna sem documentação pública. Em caso de mudança da API,
a URL dos endpoints está centralizada em `api_client.py` — apenas um ponto de ajuste.
