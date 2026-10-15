# LIVE_TESTS_REQUIRED.md

Testes que só podem ser validados com o SolidWorks real rodando. Nenhum foi
executado nesta auditoria (ambiente sem SolidWorks). Use documentos
**descartáveis** — não reutilize projetos reais. Depois de cada bloco, feche
o documento sem salvar (a menos que o próprio teste peça para salvar).

Pré-condição comum: `python -c "import server"` roda com
`SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON` **não definido** (confirma que
`execute_python` não aparece no catálogo — ver LT-08) e, para o resto, com o
MCP conectado a uma instância real do SolidWorks 2025 PT-BR (a versão que o
README já documenta como testada).

---

## LT-01 — Confirmar que a correção do timeout/lockup (BUG-01) se comporta igual com COM real

**Por quê isto não foi coberto pelos testes automatizados:** `tests/test_timeout_lockup.py`
prova a lógica do executor com uma função Python qualquer; não prova que uma
chamada COM real travada (ex.: um diálogo modal do SolidWorks esperando um
clique) produz o mesmo comportamento observável.

**Passos:**
1. Conectar ao SolidWorks (`connect_solidworks`).
2. Criar uma peça nova descartável (`create_new_part`).
3. Forçar uma situação que trave a UI do SolidWorks com um diálogo modal
   (ex.: iniciar uma operação que a API sabidamente abre uma caixa de
   diálogo sem os parâmetros certos — ou, mais simples, abrir manualmente
   qualquer diálogo do SolidWorks por fora do MCP, como "Salvar Como" na UI,
   e deixá-lo aberto).
4. Chamar qualquer ferramenta de leitura simples (`get_document_info`) pelo
   MCP enquanto o diálogo está aberto.
5. Observar o erro de timeout.
6. Chamar `get_document_info` de novo imediatamente.

**Resultado esperado:** o passo 6 deve falhar **imediatamente** (não esperar
~15s de novo) com uma mensagem citando `get_document_info` do passo 4 como a
chamada travada.

**Critério de aprovação:** mensagem do passo 6 contém "ainda bloqueado numa
chamada anterior" (ou o texto equivalente em `server.py`) e retorna em menos
de 1 segundo. Fechar o diálogo manualmente e confirmar que uma terceira
chamada volta a funcionar normalmente.

---

## LT-02 — Confirmar OK/EXP para as 47 ferramentas sem status na matriz do README (achado F1)

**Por quê:** `TOOL_INVENTORY.csv` lista 47 ferramentas (`extract_assembly_bom`,
`list_equations`, `add_equation`, `set_equation`, `delete_equation`,
`get_persistent_reference`, `resolve_persistent_reference`,
`list_display_states`, `create_display_state`, `apply_display_state`,
`rename_display_state`, `delete_display_state`, `get_appearance_properties`,
`set_appearance_properties`, `apply_texture`, `remove_texture`,
`apply_metal_finish`, `list_p2m_appearances`, `apply_p2m_appearance`,
`capture_viewport`, `get_selection`, `list_faces`, `rebuild_model`,
`validate_model`, `get_document_dependencies`, `list_configurations`,
`delete_configuration`, `export_flat_pattern_dxf`, `pack_and_go`,
`create_profile_part`, `create_tube_part`, `add_mate_by_name`,
`interference_report`, `set_view_direction`, `get_component_transform`,
`set_component_transform`, `capture_standard_views`, `list_sketch_entities`,
`draw_centerline`, `draw_spline`, `create_gear`, `get_drawing_layout`,
`get_view_entities`, `get_view_dimensions`, `dimension_by_entity_ids`,
`verify_drawing`, `delete_mate`) sem status ao vivo confirmado nesta
instalação.

**Passos (por ferramenta, ou em lote por categoria):**
1. Criar peça/montagem/desenho descartável conforme a categoria da
   ferramenta.
2. Chamar a ferramenta com argumentos válidos mínimos.
3. Verificar o retorno: sucesso sem exceção **não** é suficiente — confirme
   o efeito esperado (ex.: para `list_faces`, confira a contagem contra o
   que você vê na árvore; para `apply_texture`, confira visualmente a
   textura aplicada).

**Critério de aprovação:** para cada ferramenta, decidir `OK` (criou/leu o
recurso corretamente e o efeito foi confirmado) ou `EXP` (API retornou
`None`/erro, ou o efeito não correspondeu ao pedido) e atualizar a seção
"Cobertura desta matriz" do `README.md` movendo o nome da lista de
"sem status" para a seção OK/EXP correta da categoria.

---

## LT-03 — `_resolve_write_path` contra symlink/junction/UNC/TOCTOU (pacote §4.5)

**Por quê:** `os.path.abspath()` não resolve links simbólicos nem junctions;
isso só é observável no sistema de arquivos real do Windows de produção.

**Passos:**
1. Configurar `SOLIDWORKS_MCP_PROJECT_ROOTS=C:\projetos\permitido`.
2. Criar uma junction/symlink dentro de `C:\projetos\permitido` apontando
   para fora da raiz (ex.: `C:\projetos\permitido\escape -> C:\fora`).
3. Chamar `save_document(filepath="C:\\projetos\\permitido\\escape\\arquivo.sldprt")`.
4. Repetir com um caminho UNC (`\\servidor\compartilhamento\...`) e com `..`
   embutido (`C:\projetos\permitido\..\fora\arquivo.sldprt`).

**Resultado esperado:** todos devem ser recusados com `PermissionError`,
**exceto** se a junction/UNC resolver para dentro da própria raiz permitida.

**Critério de aprovação:** nenhuma escrita acontece fora da raiz configurada
em nenhum dos casos acima.

---

## LT-04 — Documento ativo errado (pacote §2.4)

**Passos:**
1. Pelo MCP, abrir/criar a Peça A e começar uma operação (ex.: criar um
   esboço, sem fechá-lo).
2. Na UI do SolidWorks (fora do MCP), abrir ou ativar a Peça B.
3. Chamar, pelo MCP, uma ferramenta que opera no "documento ativo" (ex.
   `extrude_sketch`) sem reabrir A explicitamente.

**Resultado esperado atual (sem a correção do pacote P1-1, não implementada
nesta rodada):** a operação provavelmente é aplicada à Peça B, silenciosamente.

**Critério de aprovação (se/quando P1-1 for implementado):** a chamada deve
falhar informando que o documento ativo não é o esperado, em vez de
modificar B.

---

## LT-05 — Referência de seleção após reconstrução (pacote §2.5)

**Passos:**
1. Selecionar uma face por geometria (ex. via `hole_wizard` ou
   `fillet_edges`).
2. Rodar uma operação anterior na árvore que muda a topologia (ex. editar
   uma extrusão de forma que a face mude de posição/índice).
3. Tentar reutilizar a referência anterior numa nova chamada.

**Critério de aprovação:** a ferramenta deve falhar de forma clara
("referência inválida após reconstrução") em vez de aplicar a operação a
outra face por engano.

---

## LT-06 — Reconexão após reinício do SolidWorks

**Passos:**
1. Conectar via `connect_solidworks`.
2. Fechar o processo do SolidWorks inteiro (Gerenciador de Tarefas).
3. Chamar qualquer ferramenta.
4. Chamar `connect_solidworks` de novo (deve reabrir o SolidWorks).
5. Chamar uma ferramenta de leitura e depois uma de escrita.

**Critério de aprovação:** o passo 3 falha com erro claro de conexão
perdida (não trava indefinidamente); o passo 4 reconecta e os passos 5
funcionam num documento novo, sem reutilizar um objeto COM da sessão morta.

---

## LT-07 — Engrenagem: geometria matemática × caminho COM real

**Já parcialmente coberto:** `gear_geometry.py`/`tests/test_gear_geometry.py`
provam a matemática do involuto (113 testes), não o caminho COM.

**Passos:** `python tests/run_gear_live_test.py` (script já existe no
repositório) contra uma instância real.

**Critério de aprovação:** o script já define isso — a ferramenta deve
devolver `verified: true` e `teeth_present: true`, e o volume real do sólido
deve bater com o volume analítico dentro da tolerância que o script usa.

---

## LT-08 — `execute_python` ausente do catálogo por padrão (verificação rápida, baixo risco)

**Passos:**
1. Sem definir `SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON`, listar as ferramentas
   do servidor MCP (via qualquer cliente MCP, não só pytest).
2. Confirmar que `execute_python` não aparece na lista.
3. Definir `SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1`, reiniciar o servidor,
   repetir — agora deve aparecer.

**Critério de aprovação:** comportamento condicional confirmado em um
cliente MCP real, não só por inspeção de código (que já foi feita e está no
`AUDIT_REPORT.md`, achado F7).

---

## Observação geral

Nenhum destes testes deve ser considerado "aprovado" só porque uma chamada
COM não lançou exceção. Para cada um, meça/confira o efeito real (geometria,
arquivo no disco, conteúdo do documento) antes de marcar como `LIVE-VERIFIED`
— ver `knowledge/verificacao_e_qa.md` do próprio projeto para o checklist
completo de verificação.
