# AUDIT_REPORT.md — Auditoria `h4child/mcp-solidworks`

## 1. Resumo executivo

Esta auditoria confirmou, refutou ou atualizou os achados do pacote de
auditoria recebido (`auditoria_mcp_solidworks/00` a `08`) contra o código
real do repositório, executou a suíte de testes existente, inventariou as
158 ferramentas MCP incondicionais + `execute_python` (condicional), e
corrigiu dois bugs reproduzíveis sem acesso a uma instalação do SolidWorks:

1. **Confirmado e corrigido** — uma chamada COM que excede seu timeout
   continua rodando na única worker thread do executor; toda chamada
   seguinte ficava na fila e expirava seu próprio timeout com a mesma
   mensagem genérica, mesmo sem nunca ter começado a executar. `_run()`
   agora detecta esse estado e falha imediatamente com uma mensagem que
   nomeia a chamada travada, em vez de fingir que a nova chamada também
   está lenta.
2. **Confirmado e corrigido** — `validate_model` estava anotado
   `readOnlyHint=True` mas chama `doc.ForceRebuild3(False)`, a mesma
   operação que `rebuild_model` corretamente anota como não-somente-leitura.

Nenhuma validação ao vivo no SolidWorks foi realizada — este ambiente não
tem a aplicação instalada. Tudo abaixo que exige confirmação com SolidWorks
real está listado em `LIVE_TESTS_REQUIRED.md` e **não** foi testado.

A suíte de testes completa (`pytest tests/`, excluindo os
`tests/run_*_live_test.py`, que exigem COM real) passa: **1339 passed, 7
deselected, 0 failed** (1336 pré-existentes + 3 novos para o bug do
timeout).

## 2. Versão/commit auditado

| Campo | Valor |
|---|---|
| Repositório | `h4child/mcp-solidworks` |
| Branch | `ccr-339a8fb8-38asl1` (idêntica a `origin/main` no início da auditoria) |
| Commit inicial | `668f3105fe49196720fba35fe4c0ef72416eebac` |
| Working tree | limpo antes de qualquer edição |
| `pyproject.toml` version | `5.21.5` |
| Python | 3.13.16 |
| SO deste ambiente | Linux (sem SolidWorks — `tests/conftest.py` falseia `win32com`/`pythoncom`) |
| SolidWorks | não disponível neste ambiente; README refere-se a testes ao vivo em "SolidWorks 2025 PT-BR" |

## 3. Arquitetura (confirmada no código atual)

- `FastMCP("solidworks-mcp", instructions=SERVER_INSTRUCTIONS)` — `server.py:176`.
- COM via `win32com.client`/`pythoncom`.
- Executor dedicado: `ThreadPoolExecutor(max_workers=1, thread_name_prefix="sw-com")` — `server.py:219` — **uma única** thread de trabalho para todas as chamadas COM, porque objetos COM são apartment-threaded.
- `_run()` (`server.py:341`) submete o trabalho ao executor e aguarda com `asyncio.wait_for`, usando um orçamento de timeout por ferramenta (`_timeout_budget_for`, `server.py:283`).
- Log estruturado por chamada em `calls.jsonl` (`CALL_LOG_PATH`, `server.py:77`), instrumentado uma única vez sobre `ToolManager.call_tool` (`_install_call_instrumentation`, `server.py:179`).
- `execute_python` só é **registrado** (não apenas bloqueado em runtime) quando `SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1` — `server.py:15048` em diante.
- Política de caminho de escrita opcional via `SOLIDWORKS_MCP_PROJECT_ROOTS` (`_resolve_write_path`, `server.py:439`) e allowlist separada para macros via `SOLIDWORKS_MCP_MACRO_ROOTS` (`_resolve_macro_path`, `server.py:14888`-ish).
- Recurso `solidworks://tool-status` é gerado **lendo o próprio `README.md`** em runtime (`README_PATH = ...`, perto de `server.py:15087`) — ou seja, a matriz OK/EXP do README *é* o que a IA cliente vê ao vivo, não é só documentação estática.

## 4. Inventário de ferramentas

Ver `TOOL_INVENTORY.csv` (gerado mecanicamente a partir do `server.py` atual,
não copiado da análise anterior). Resumo:

| Métrica | Valor |
|---|---|
| Total de ferramentas (incondicionais) | 158 |
| + `execute_python` (condicional, só com env var) | 159 no total |
| Leitura (`readOnlyHint=True`) | 38 |
| Destrutivas (`destructiveHint=True`) | 16 |
| Demais (escrita, não-destrutiva) | 105 |
| Cobertas pela matriz OK/EXP do README | 137 (112 após remover `execute_python`, que é tratado à parte) — **ver achado F1** |
| Sem *qualquer* referência em `tests/` (unitário ou live) | 41 — **ver achado F2** |

## 5. Achados, classificados por severidade

Legenda de status: **CONFIRMADO** (reproduzido/verificado no código atual),
**REFUTADO** (a hipótese do pacote não se sustenta no código atual),
**NÃO INVESTIGADO EXAUSTIVAMENTE** (amostrado, não coberto 100%).

### F1 — [MÉDIO] Matriz OK/EXP do README (= resource `tool-status`) cobre só 137/158 ferramentas
**Status:** CONFIRMADO.
**Evidência:** `TOOL_INVENTORY.csv`, coluna `status_readme`; 47 ferramentas
(`extract_assembly_bom`, `get_component_transform`, `list_equations`,
`pack_and_go`, `rebuild_model`, `validate_model`, etc. — lista completa no
CSV e no README) não aparecem em nenhuma das seções "-- OK"/"-- EXP"/"--
parcial" do README.
**Risco:** a IA cliente consulta esse resource para decidir em quais
ferramentas confiar (`.claude/CLAUDE.md` do próprio projeto instrui isso
explicitamente). Para essas 47, ela não tem como saber se a ferramenta é
confiável nesta instalação.
**Correção aplicada:** README.md ganhou uma seção "Cobertura desta matriz"
nomeando exatamente essas 47 ferramentas e remetendo ao CSV — não inventei
status OK/EXP para elas, porque isso exigiria teste ao vivo (ver
`LIVE_TESTS_REQUIRED.md`).
**Risco residual:** nenhum additional — é uma correção de documentação.

### F2 — [MÉDIO] Timeout não implica cancelamento COM; chamadas subsequentes falham com mensagem enganosa
**Status:** CONFIRMADO e CORRIGIDO.
**Evidência:** `tests/test_timeout_lockup.py` (novo) reproduz isso sem
SolidWorks: `_run()` usa `_executor.submit()` sobre um
`ThreadPoolExecutor(max_workers=1)`; quando `asyncio.wait_for` expira, ele só
cancela o *future* do lado asyncio — a thread real continua executando (ou
bloqueada, no caso real, num diálogo modal do SolidWorks). Toda chamada
seguinte entra na fila atrás dela e, ao não conseguir nem começar antes do
*seu próprio* timeout, relatava o mesmo erro genérico ("pode estar bloqueado
por um diálogo"), como se ela mesma estivesse lenta.
**Correção aplicada (`server.py`, função `_run`):** um marcador de módulo
(`_stuck_call`) é gravado quando uma chamada expira; chamadas seguintes
verificam esse marcador **antes** de submeter qualquer trabalho ao executor
e falham imediatamente com uma mensagem que nomeia a ferramenta travada, há
quanto tempo, e por quê ("COM não pode ser cancelado com segurança"). O
marcador usa o `concurrent.futures.Future` bruto (não o wrapper asyncio) para
saber quando o trabalho realmente termina — o wrapper asyncio marca-se como
cancelado imediatamente, independente do estado real da thread, o que seria
um sinal falso de "já terminou".
**Teste:** `tests/test_timeout_lockup.py` (3 testes, todos passam).
**Risco residual:** este fix melhora o diagnóstico; não resolve a limitação
de fundo (COM não pode ser interrompido com segurança uma vez iniciado —
isso é inerente à API COM do SolidWorks, não uma falha de implementação). O
pacote de auditoria propõe um contrato `indeterminate` mais amplo para
escritas (P0-3/P1-2) — não implementado aqui porque mudaria o schema de
retorno de toda ferramenta de escrita, o que é maior do que "correção
pequena e segura" permite sem o aval do maintainer sobre compatibilidade.

### F3 — [BAIXO] `validate_model` anotado `readOnlyHint=True` apesar de forçar rebuild
**Status:** CONFIRMADO e CORRIGIDO.
**Evidência:** `server.py`, função `validate_model` (chamava
`doc.ForceRebuild3(False)` antes de inspecionar a árvore de features) tinha
`ToolAnnotations(readOnlyHint=True, ...)`, enquanto `rebuild_model` —
que faz exatamente a mesma chamada — tem `readOnlyHint=False`.
**Correção aplicada:** anotação trocada para
`ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)`,
e o docstring agora documenta a mudança e por quê.
**Quebra de contrato:** sim, deliberada — um cliente MCP que decidia
auto-aprovar `validate_model` por ele ser "somente leitura" vai passar a
pedir confirmação, como já fazia para `rebuild_model`. Isso é a correção, não
uma regressão: a anotação antiga estava simplesmente errada.
**Teste:** suíte completa (`tests/test_contract.py`) não tem nenhuma
asserção codificada para o valor antigo de `validate_model` especificamente,
e passa sem alteração.

### F4 — [BAIXO] Política de caminho de escrita (`_resolve_write_path`) — amostragem não encontrou bypass
**Status:** NÃO INVESTIGADO EXAUSTIVAMENTE (amostrado).
**Evidência:** os 9 pontos de chamada de `_resolve_write_path` em
`server.py` cobrem `save_document`, `capture_standard_views`,
`capture_assembly_pose`, `restore_assembly_pose`, `export_document`,
`capture_viewport`, `export_flat_pattern_dxf`, `pack_and_go` e
`set_view_direction`/`get_view_state` (exportação de imagem). Em cada um dos
pontos inspecionados, o caminho resolvido (`abs_path`/`path`/`target`) é
usado para a chamada `SaveAs`/`SaveAs3`/`open(..., "w")` subsequente —
não encontrei um caminho bruto do chamador chegando a uma escrita de arquivo
sem passar pelo helper.
**Risco residual:** não testei exaustivamente *todas* as 105 ferramentas de
"escrita" do inventário — a maioria escreve apenas no documento SolidWorks
em memória (não em disco), então a política de caminho simplesmente não se
aplica a elas. `_resolve_macro_path` (allowlist separada para `run_macro`) foi
inspecionado e segue o mesmo padrão. Os testes de segurança de caminho
propostos no pacote (`..`, UNC, symlinks/junctions, TOCTOU) **exigem Windows
real** e estão em `LIVE_TESTS_REQUIRED.md`.

### F5 — [INFORMATIVO] Padrão `except Exception: pass` é extremamente comum (47 ocorrências)
**Status:** NÃO INVESTIGADO EXAUSTIVAMENTE (amostrado).
**Evidência:** `grep -c "except Exception:$" server.py` = 187 blocos
`except Exception:`, dos quais 47 seguidos imediatamente por `pass`.
Amostrei ~45 desses: a grande maioria protege operações de *melhor esforço*
já documentadas como tal nos comentários vizinhos (redesenhar a tela depois
de mudar uma propriedade visual, fechar um esboço, atualizar a árvore de
features para exibição, resolver componentes leves) — nenhuma delas descarta
o resultado de uma operação que o chamador depende para decidir
sucesso/falha.
**Risco residual:** não é uma auditoria linha-a-linha de todas as 187
ocorrências — isso excede o escopo de "correções pequenas e seguras" desta
rodada. Nenhuma instância examinada mascarava uma falha que o contrato da
ferramenta promete reportar.

### F6 — [INFORMATIVO] Contagem de ferramentas no changelog do README está desatualizada
**Status:** CONFIRMADO, correção cosmética já incluída em F1.
**Evidência:** a última entrada do changelog que registra uma contagem
corrida é "`### Novas em v5.18.0 (... 155 -> 157)`" (README.md, por volta da
linha 377); a versão atual (`pyproject.toml` = 5.21.5) tem 158 ferramentas
incondicionais. O pacote de auditoria (seção 7.3) já previa essa divergência
("141, 150, 157..."). A seção "Cobertura desta matriz" adicionada ao README
(F1) declara o número atual (158 + `execute_python` condicional) em vez de
depender do changelog histórico.

### F7 — Itens do pacote de auditoria classificados como REFUTADO ou SEM EVIDÊNCIA DE PROBLEMA
- **`execute_python` registrado incondicionalmente** (pacote, §4.3) —
  REFUTADO: confirmado em `server.py` que o `@mcp.tool` só existe dentro de
  `if _execute_python_enabled():`, então a ferramenta nem aparece no
  catálogo sem a env var (não é "registrada e depois bloqueada").
- **`run_macro` sem nenhuma restrição de caminho** (pacote, §4.4) —
  PARCIALMENTE REFUTADO: existe `SOLIDWORKS_MCP_MACRO_ROOTS`, uma allowlist
  de diretório **opcional e separada** da política de escrita. O pacote já
  previa isso corretamente como "allowlist opcional", então não é uma
  divergência — é a confirmação de que o controle existe mas não é
  obrigatório por padrão (decisão de produto documentada no próprio comentário
  do código, não um bug).
- **Itens de cobertura de categorias do SolidWorks** (pacote, documento 03 —
  engrenagem helicoidal/cônica, rolamentos normalizados, FEA, etc.) — não são
  bugs, são lacunas de funcionalidade já reconhecidas no próprio README
  ("O que este MCP NÃO faz") e no `.claude/CLAUDE.md` do projeto. Não tratados
  como "bug" nesta auditoria.

## 6. Testes executados

```
uv sync --group dev
uv run pytest tests/ -q -k "not run_" --ignore-glob="tests/run_*"
# 1339 passed, 7 deselected, 1 warning (pydantic, pré-existente)
```

- **1336** testes pré-existentes — **todos passavam antes e depois** das
  correções (nenhuma regressão).
- **3 novos** testes em `tests/test_timeout_lockup.py`, cobrindo o achado
  F2: (1) a chamada travada expira e é lembrada; (2) a chamada seguinte falha
  rápido com a mensagem nova; (3) o marcador some quando o trabalho real
  termina e uma chamada nova volta a funcionar.
- **7 deselecionados** = os `tests/run_*_live_test.py`, que exigem SolidWorks
  real e não foram executados (ver `LIVE_TESTS_REQUIRED.md`).

## 7. Limitações desta auditoria

- Sem acesso a uma instalação do SolidWorks: zero ferramentas receberam o
  status `LIVE-VERIFIED` nesta rodada — todo o status `OK`/`EXP` no CSV
  reflete o que o README já declarava (de testes ao vivo anteriores, com
  data), não uma reverificação ao vivo feita agora.
- `except Exception: pass` (F5) e a cobertura total de `_resolve_write_path`
  (F4) foram amostrados, não auditados ferramenta por ferramenta.
- Não implementei o contrato estruturado `success/partial/failed/indeterminate`
  proposto no pacote (P1-2) — mudaria o schema de retorno de ~105 ferramentas
  de escrita, incompatível com "correção pequena" sem alinhamento prévio do
  maintainer.
- Não implementei "contexto explícito de documento" (P1-1, troca de
  `ActiveDoc`) — exigiria desenho de API novo (sessão/handle), não uma
  correção local.

## 8. Git

```
Branch: ccr-339a8fb8-38asl1
Commit inicial: 668f3105fe49196720fba35fe4c0ef72416eebac (working tree limpo)
```

Ver `git diff --stat` no final do trabalho e o commit criado para esta
auditoria.
