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

## LT-09 — `apply_texture`: assinatura corrigida de `CreateTexture` + relevo/blend best-effort (15/10/2026)

**Por quê:** a correção presume que `IModelDocExtension::CreateTexture`
recebe `(FileName, Width, Height, Angle)` — documentado na API do
SolidWorks, mas **nunca confirmado contra uma instalação real** por este
projeto. Se a suposição estiver errada, a textura pode sair com proporção,
rotação ou granularidade diferentes do esperado.

**Passos:**
1. Criar uma peça descartável com uma face plana grande (ex. 200x100mm).
2. Chamar `apply_texture(texture_path="<imagem com padrão assimétrico, ex.
   um retângulo com uma seta>", u_scale=2.0, v_scale=8.0, angle=30.0,
   target="face", face_x=..., face_y=..., face_z=...)`.
3. Capturar a vista (`capture_standard_views`) e inspecionar visualmente:
   a textura deve aparecer **esticada ~4x mais no eixo V que no U** (não
   quadrada) e **rotacionada ~30°** em relação à orientação padrão.
4. Repetir com `blend_with_color=True` e `blend_with_color=False`, comparando
   visualmente se a cor do material por baixo influencia o resultado.
5. Repetir com `bump_texture_path` apontando para uma imagem em
   escala de cinza (ex. um padrão de ondas) e inspecionar visualmente se
   algum relevo aparece — **resultado esperado real é incerto**: o retorno
   da ferramenta (`bump.applied_via_property`) dirá se algum dos nomes de
   propriedade tentados existe nesta instalação; se vier `None`, não há
   relevo nenhum, apenas a textura de cor.

**Critério de aprovação:** proporção U/V e ângulo da textura na vista capturada
correspondem visualmente aos valores pedidos (não aos valores antigos —
comparar com uma textura aplicada pela UI do SolidWorks com os mesmos
números, usando "Editar Aparência › Mapeamento", como referência). Se não
corresponder, a suposição sobre a assinatura de `CreateTexture` está errada
e a função precisa ser corrigida de novo com a ordem real.

---

## LT-10 — Templates, System Options e vista em corte 3D (17/10/2026)

**Por quê:** `get_default_templates`/`set_default_templates`/
`create_document_from_template` reaproveitam índices e chamadas já
confirmados (8/9/10, `NewDocument`), então o risco ali é baixo. `set_section_view`/
`clear_section_view` usam nomes de método **chutados** -- é bem possível que
nenhum funcione nesta instalação.

**Passos:**
1. `get_default_templates()` — confirmar que os três caminhos retornados
   batem com SolidWorks > Opções > Propriedades do Sistema > Modelos
   Padrão na UI.
2. `set_default_templates(part="C:\caminho\MeuTemplate.prtdot")` com um
   template de teste — confirmar na UI que o campo mudou, e que
   `create_new_part()` em seguida usa esse template (ex.: confirmar uma
   propriedade customizada exclusiva desse template no documento criado).
3. `create_document_from_template(template_path="C:\caminho\Config.drwdot")`
   — confirmar que o documento criado tem a folha/configuração daquele
   template específico, não do default.
4. Numa peça aberta, `save_as_template(filepath="C:\temp\Teste.prtdot")` —
   abrir o arquivo resultante na UI do SolidWorks e confirmar que ele
   aparece como template (ícone de template, pode ser usado em "Novo
   Documento").
5. `run_command_by_id(command_id=...)` com um ID conhecido e inofensivo
   (ex. o de "Zoom to Fit") — confirmar que o comando correto executou.
6. `set_section_view(plane="right", offset=0)` numa peça com geometria
   assimétrica — observar se a vista 3D realmente corta o modelo. Se
   `applied` vier `false`, usar o Macro Recorder do SolidWorks (Ferramentas
   > Macro > Gravar) clicando no botão "Vista em Corte" da UI manualmente,
   abrir o `.swp` gerado e ler o nome do método COM realmente usado — depois
   atualizar `_SECTION_VIEW_ON_CANDIDATES`/`_SECTION_VIEW_OFF_CANDIDATES` em
   `server.py` com o nome real.

**Critério de aprovação:** cada efeito confirmado visualmente/na UI, não só
"a chamada não lançou exceção". Para `set_section_view`, se nenhum candidato
funcionar, isso é uma lacuna confirmada (ferramenta marcada `EXP` no README),
não um "talvez funcione".

---

## LT-11 — `set_sheet_scale` e `insert_model_items` (17/10/2026)

**Por quê:** `ISheet::SetScale` e `IDrawingDoc::InsertModelAnnotations2` (e,
dentro dela, os valores de bit de `_MODEL_ITEMS_FLAGS`) nunca foram
confirmados ao vivo neste repositório.

**Passos:**
1. Com um desenho de uma peça aberto, `set_sheet_scale(scale1=1, scale2=2)`
   — confirmar na UI (SolidWorks > Propriedades da Folha) que a escala
   mudou para 1:2, e que as vistas que seguiam a escala da folha
   redimensionaram.
2. `insert_model_items(view_name="<nome de uma vista>", dimensions=True)`
   numa peça que tenha cotas definidas no modelo 3D (via `add_sketch_dimension`
   por exemplo) — confirmar que as cotas aparecem na vista do desenho. Se
   nada aparecer, ou aparecer o tipo errado de anotação, o mapa de bits em
   `_MODEL_ITEMS_FLAGS` (server.py) está errado — descobrir o valor real
   gravando uma macro ao clicar em "Inserir > Itens do Modelo" manualmente
   e lendo o `ImportFlag` passado para `InsertModelAnnotations2` no `.swp`
   gerado.
3. Repetir o passo 2 com `notes=True`/`geometric_tolerances=True` etc.
   isoladamente, para confirmar cada bit individualmente em vez de só a
   combinação default.

**Critério de aprovação:** escala da folha e cotas inseridas conferidas
visualmente na UI do SolidWorks, não só pelo retorno `applied: true` da
ferramenta.

---

## Observação geral

Nenhum destes testes deve ser considerado "aprovado" só porque uma chamada
COM não lançou exceção. Para cada um, meça/confira o efeito real (geometria,
arquivo no disco, conteúdo do documento) antes de marcar como `LIVE-VERIFIED`
— ver `knowledge/verificacao_e_qa.md` do próprio projeto para o checklist
completo de verificação.
