# NOVAS AÇÕES IMPLEMENTADAS — rodada de cross-reference com o catálogo de 1007 itens

Este documento registra **apenas** o que foi adicionado nesta rodada: 47 novas
ferramentas MCP, cruzando o catálogo `catalogo_mcp_solidworks_mais_de_1000_acoes.txt`
(1007 itens, 36 seções) contra `server.py` (167 ferramentas antes desta rodada,
214 depois), usando `AUDITORIA_COBERTURA_FUNCIONAL.md` e `TOOL_INVENTORY.csv`
como ponto de partida e relendo o código atual onde a auditoria estava
desatualizada.

**Nada aqui foi testado contra um SolidWorks real.** Este ambiente é Linux sem
SolidWorks instalado. Pela própria taxonomia deste repositório, todo item
abaixo é **IMPLEMENTADO, NÃO TESTADO** na melhor das hipóteses — nunca
"testado" ou "OK". Vários itens têm a chamada COM subjacente marcada
explicitamente como **palpite de boa-fé não verificado** (ver seção própria
abaixo); esses precisam de confirmação ao vivo antes de qualquer uso em
produção.

Testes: `uv run pytest tests/ -x -q -k "not run_" --ignore-glob="tests/run_*"`
→ **1779 passed, 13 deselected** (os `run_*_live_test.py`, que exigem
SolidWorks real e não foram executados).

## Tabela — ferramentas adicionadas

| ID(s) do catálogo | Ferramenta | server.py | Descrição | Categoria (dentro/fora de escopo) | Precisa verificação ao vivo? |
|---|---|---|---|---|---|
| 0049 | `draw_rectangle_centered` | 6121 | Retângulo de esboço centrado (CreateCenterRectangle) | Dentro — esboço 2D | Não (API bem conhecida) |
| 0053 | `draw_arc_3point` | 6202 | Arco de esboço por 3 pontos (Create3PointArc) | Dentro — esboço 2D | Não |
| 0055 | `draw_ellipse` | 6251 | Elipse de esboço (CreateEllipse) | Dentro — esboço 2D | Não |
| 0061 | `add_sketch_point` | 6315 | Ponto de esboço dedicado (CreatePoint) | Dentro — esboço 2D | Não |
| — (gap citado na tarefa, sem ID numerado no catálogo) | `add_sketch_text` | 6359 | Texto em esboço (InsertSketchText) | Dentro — esboço 2D | Não |
| 0070 | `offset_sketch_entities` | 6407 | Offset de entidades de esboço (SketchOffset2) | Dentro — esboço 2D | **Sim** |
| 0069 | `convert_sketch_entities` | 6466 | Converter aresta/face 3D em geometria de esboço (SketchConvertEntities) | Dentro — esboço 2D | Não (confiança razoável) |
| 0078/0079 | `get_sketch_contours` | 6515 | Detectar contorno aberto/fechado (read-only, GetSketchContours) | Dentro — esboço 2D | Não (degrada honestamente se indisponível) |
| 0093 | `remove_sketch_relation` | 6608 | Remover relação geométrica de esboço | Dentro — relações | Não |
| 0094 | `query_sketch_relations` | 6642 | Contar relações de um esboço (read-only, GetSketchRelationsCount) | Dentro — relações | Não (degrada honestamente) |
| 0105 | `set_dimension_driven` | 6702 | Alternar cota dirigida (driven) vs. dirigente (driving) | Dentro — cotas | Não |
| 0153 | `create_reference_point` | 7145 | Ponto de referência (geometria de referência) | Dentro — features | **Sim** |
| C03 (0117, gap) | `extrude_up_to_entity` | 7208 | Ressalto extrudado "até face/superfície" (FeatureExtrusion2 end-cond 4) | Dentro — features | Não (reusa assinatura já confirmada de extrude_sketch) |
| D04 (0118, gap) | `cut_up_to_entity` | 7259 | Corte extrudado "até face/superfície" (FeatureCut4 end-cond 4) | Dentro — features | Não (idem cut_extrude) |
| 0172 | `chamfer_edges_two_distances` | 7308 | Chanfro por duas distâncias (InsertFeatureChamfer type 0) | Dentro — filetes/chanfros | **Sim** |
| 0262 | `list_bodies` | 7367 | Listar corpos sólidos com inventário básico (read-only) | Dentro — multicorpos | Não |
| 0287 | `get_body_bounding_box` | 7418 | Bounding box de um corpo nomeado (read-only, GetBodyBox) | Dentro — multicorpos | Não |
| gap (categoria 8, sem ID exato) | `delete_body` | 7467 | Excluir um corpo nomeado | Dentro — multicorpos | Não |
| 0301 | `replace_component` | 12810 | Substituir componente de montagem (ReplaceComponents2) | Dentro — montagem | **Sim** |
| 0321 | `count_component_instances` | 12864 | Contar instâncias agrupadas por arquivo+configuração (read-only) | Dentro — montagem | Não |
| 0832/I14 | `detect_components_without_mates` | 12948 | Componentes sem mate referenciado (read-only, best-effort) | Dentro — montagem | **Sim** (travessia MateEntities é best-effort) |
| 0338/0340/0345/0346/0347 | `add_mechanical_mate` | 13016 | Mates mecânicos: slot, path, linear_coupler, universal_joint, hinge | Dentro — mates | **Sim** (códigos swMateType_e são palpite; o próprio tool lê `.Type` de volta e reporta `type_confirmed`) |
| 0381 | `add_exploded_step` | 12360 | Adicionar uma etapa a uma vista explodida já existente | Dentro — montagem/explosão | Não |
| 0384 | `collapse_exploded_view` | 12423 | Recolher vista explodida | Dentro — montagem/explosão | Não |
| 0376 | `set_component_suppression_by_configuration` | 12450 | Suprimir/retomar componente em UMA configuração nomeada | Dentro — configurações | Não |
| 0390 | `compare_bom_configurations` | 12529 | Comparar lista de peças entre duas configurações (read-only) | Dentro — BOM/configurações | Não |
| 0392/0649 | `detect_duplicate_bom_items` | 12587 | Detectar mesmo PartNumber/Description em arquivos diferentes (read-only) | Dentro — BOM | Não |
| 0402 | `check_minimum_clearance` | 13495 | Distância mínima entre duas faces (read-only, IMeasure) | Dentro — interferência/folga (geométrico) | Não |
| 0403/0404 | `check_axis_alignment` | 13546 | Ângulo + distância entre eixos de dois cilindros (read-only) | Dentro — interferência/folga (geométrico) | Não (reusa extração de CylinderParams já confirmada em add_mate) |
| O02 (gap) | `set_sheet_size` | 13633 | Tamanho/formato de folha de desenho (SetupSheet5) | Dentro — desenho técnico | **Sim** |
| 0476/0477/0478 | `add_dimension_tolerance` | 13702 | Tolerância bilateral/simétrica/nenhuma numa cota (IDimTolerance) | Dentro — GD&T | **Sim** |
| 0484 | `add_datum_feature_symbol` | 13759 | Símbolo de datum (letra com triângulo) (InsertDatumTag2) | Dentro — GD&T | **Sim** |
| 0547/G01 | `get_applied_material` | 13799 | Ler o material REALMENTE aplicado (read-only) | Dentro — materiais | Não |
| 0574 | `compare_mass_properties` | 13830 | Comparar massa medida vs. esperada com tolerância (read-only) | Dentro — materiais/propriedades | Não |
| 0627 | `bulk_set_custom_properties` | 13869 | Definir várias propriedades personalizadas em uma chamada | Dentro — propriedades | Não |
| 0646 | `export_bom_to_csv` | 3468 | Exportar BOM de montagem direto para CSV (sem desenho) | Dentro — BOM | Não |
| 0648/0663 | `validate_bom_completeness` | 3523 | Validar propriedades obrigatórias por linha de BOM (read-only) | Dentro — BOM | Não |
| 0659 | `create_fastener_list` | 3564 | Lista de fixadores por heurística de palavra-chave (read-only) | Dentro — BOM (heurística, não autoritativa) | Não (é heurística por natureza, documentado) |
| 0725/0726 | `verify_bearing_fit` | 13930 | Folga/interferência diametral + alinhamento eixo-mancal (read-only) | Dentro — mecânico especializado (geométrico) | Não |
| 0405/0728 | `verify_bolt_hole_fit` | 13988 | Folga diametral parafuso-furo + checagem de diâmetro nominal (read-only) | Dentro — mecânico especializado (geométrico) | Não |
| 0731-0739 | `import_model` | 2589 | Importar STEP/IGES/Parasolid/STL/SAT com checagem de corpo/bbox/escala | Dentro — interoperabilidade | Não (reusa _open_doc6 já confirmado) |
| 0750 | `export_selected_bodies` | 2673 | Exportar só os corpos nomeados de uma peça multicorpo | Dentro — interoperabilidade | **Sim** (depende de exportação respeitar visibilidade) |
| 0795 | `detect_unused_sketches` | 17141 | Esboços provavelmente não utilizados (read-only, heurística por IFeature.Visible) | Dentro — auditoria de árvore | Não (heurística documentada) |
| 0814 | `generate_model_snapshot` | 17191 | Snapshot serializável de árvore+propriedades+equações+massa | Dentro — auditoria de árvore | Não |
| 0815 | `compare_model_snapshots` | 17275 | Diff entre dois snapshots (pura leitura de arquivo, sem COM) | Dentro — auditoria de árvore | Não |
| 0847 (composto) | `audit_assembly` | 19126 | Bateria de checagens de montagem em uma chamada (read-only) | Dentro — auditoria de montagem | Não (compõe helpers já descritos acima) |
| 0852 | `generate_assembly_checklist` | 19221 | Checklist de aprovação pré-entrega (read-only, pass/fail por item) | Dentro — auditoria de montagem | Não |

**Total: 47 ferramentas.** Nenhuma duplica uma ferramenta já existente —
cada uma foi conferida com `grep` contra `server.py` e contra
`TOOL_INVENTORY.csv` antes de ser escrita.

## Seção especial — "precisa de verificação ao vivo prioritária"

Estas 11 ferramentas usam uma chamada COM reconstruída a partir do
conhecimento geral da API do SolidWorks, **não confirmada neste ambiente**
(sem SolidWorks instalado). Cada uma tem um aviso explícito no próprio
docstring (`NEEDS LIVE VERIFICATION` ou equivalente) e está coberta pelo teste
`tests/test_new_tools_round2.py::test_best_effort_com_calls_are_flagged_in_their_own_docstring`,
que falha se o aviso for removido sem revisão. Ordem sugerida de prioridade
para o primeiro teste ao vivo (mais simples/seguro primeiro):

1. `offset_sketch_entities` — `ISketchManager::SketchOffset2`, ordem de
   argumentos reconstruída.
2. `chamfer_edges_two_distances` — reusa `InsertFeatureChamfer` já confirmado
   em `chamfer_edges`, só muda o `Type` (0 em vez de 1); risco baixo.
3. `create_reference_point` — `IModelDoc2::InsertPoint2`, por analogia com
   `InsertAxis2` (já usado em `create_reference_axis`, que é EXP).
4. `export_selected_bodies` — depende do exportador STEP/STL realmente
   respeitar `IBody2.Visible`; se não respeitar, o resultado é "exportou
   tudo mesmo assim" (detectável comparando volume/bbox do arquivo exportado
   com `measure_body` do corpo esperado).
5. `set_sheet_size` — `ISheet::SetupSheet5`, ordem de argumentos
   reconstruída.
6. `add_dimension_tolerance` — `IDimTolerance::Type`/`SetMaxValue2`/
   `SetMinValue2`, códigos `swTolType_e` reconstruídos (só `none=0` é
   confiável).
7. `add_datum_feature_symbol` — `IDrawingDoc::InsertDatumTag2`, por analogia
   com `InsertGtol` (já confirmado em `add_gdt_symbol`).
8. `replace_component` — `IAssemblyDoc::ReplaceComponents2`, ordem de
   argumentos reconstruída.
9. `add_mechanical_mate` — códigos `swMateType_e` para slot/hinge/
   universal_joint/path/linear_coupler são palpite; **o próprio tool já
   mitiga isso** lendo `IMate2::Type` de volta e reportando
   `type_confirmed: false` quando o código não bate, em vez de confiar
   cegamente.
10. `detect_components_without_mates` — travessia
    `GetSpecificFeature2 -> Mates -> MateEntities -> ReferenceComponent` é
    best-effort; reporta `coverage_unknown` para mates que não conseguiu ler
    em vez de fingir cobertura total.

## O que foi deliberadamente NÃO implementado

### Fora de escopo por decisão do projeto (`.claude/CLAUDE.md`)
- **Superfícies** (seção 5 do catálogo, itens 0180-0201): nenhuma ferramenta
  de superfície plana/extrudada/revolvida/varrida/loft/boundary/preenchida/
  offset/costurar/converter-em-sólido foi adicionada. Confirmado: "Sem
  ferramentas de superfície" é decisão de escopo documentada.
- **Simulação/FEA** (seção 19, itens 0664-0697): nada adicionado. Já fora de
  escopo (exige SolidWorks Simulation, add-in licenciado separado).
- **CAM/fabricação** (categoria P do checklist anterior / parte da seção 21):
  nada adicionado. Exige SolidWorks CAM/CAMWorks.
- **Movimento físico real** (seção 12, itens 0410-0425: motor, gravidade,
  mola/amortecedor, simulação de movimento, resultados de velocidade/
  aceleração): fora de escopo — é Motion/Simulation, não um check geométrico
  estático. `check_minimum_clearance`/`check_axis_alignment` cobrem a parte
  puramente geométrica que PERMANECE dentro de escopo.

### Já coberto por uma ferramenta existente (não duplicado)
- Mate de largura (`width`) e mate de engrenagem (`gear`) — já cobertos por
  `add_advanced_mate` (mate_type='width'/'gear'), confirmado por leitura de
  código antes de escrever `add_mechanical_mate`.
- Mate de parafuso (`screw`) e cremalheira-pinhão (`rack_pinion`) — já
  existem como `add_screw_mate` e `add_rack_pinion_mate`.
- Importação de arquivo nativo (.sldprt/.sldasm/.slddrw) — já coberta por
  `open_document`; `import_model` cobre especificamente os formatos neutros
  (STEP/IGES/Parasolid/STL/SAT) com validação extra, não substitui
  `open_document`.
- "Criar configuração de montagem" — `create_configuration` já é genérico
  o bastante para peça OU montagem; não foi duplicado.

### Item de processo/documentação, não uma ferramenta discreta
- Seções 25 (robustez COM), 26 (protocolo MCP/segurança), 27 (templates de
  implementação), 28 (matriz de testes), 29 (arquitetura), 30 (matriz de
  rastreabilidade), 31 (modelos de ferramentas), 32-35 (configurações/
  templates/plano de implementação conceituais) e 36 (como auditar) são
  guias de processo e arquitetura, não ações individuais codificáveis. Nada
  foi "convertido em ferramenta" a partir delas, conforme instrução da
  tarefa. Onde um item concreto e codificável aparecia dentro dessas seções
  (ex.: "0894 implementar timeout", "0898 implementar dry-run"), ele já
  existe no código-base (timeout budget por ferramenta, `overwrite`
  booleano em `export_bom_to_csv`/`export_selected_bodies`) e não foi
  reimplementado.

### Investigado e deliberadamente deixado de fora (sem API confiável conhecida)
- **Detecção de esboço sub/sobre-definido (B16-B18 do checklist anterior)**:
  investigado, sem API COM confiável e conhecida para ler o status de DOF
  (subdefinido/totalmente definido/sobredefinido) de um esboço de forma
  limpa. `query_sketch_relations` cobre a contagem de relações (parte do
  problema), mas o status de DOF em si não foi implementado para evitar
  fabricar um resultado enganoso.
- **Padrão por tabela** (0137): não implementado — exigiria uma API de
  tabela de padrão (`PatternTableFeatureData` ou similar) sobre a qual não
  havia confiança suficiente para uma implementação honesta nesta rodada.
- **Draft (inclinação de molde)**, **thicken de superfície**, **sistema de
  coordenadas dedicado**: não implementados nesta rodada — ficaram de fora
  por orçamento de tempo/confiança, não por decisão de escopo; continuam
  como lacunas conhecidas (ver `AUDITORIA_COBERTURA_FUNCIONAL.md`
  categoria E).
- **Mirror de componentes de montagem** ("espelhar componentes", 0370):
  investigado; a API correspondente (`IAssemblyDoc` mirror-component
  feature) é bem mais complexa que um espelhamento de feature/corpo em
  peça (exige plano espelho + lista de componentes + opção
  derivado-vs-independente) e não foi implementada com confiança suficiente
  nesta rodada. `create_assembly_pattern` (já existente, EXP) cobre padrão
  linear/circular de componentes, não espelhamento.
- **Sketch fillet/chamfer 2D dedicados** (0062/0063): investigados; o nome
  exato do método COM (`CreateFillet`/`CreateChamfer` em `ISketchManager`)
  não pôde ser confirmado com confiança suficiente para justificar uma
  implementação nesta rodada (ao contrário de `chamfer_edges_two_distances`,
  que reusa uma assinatura JÁ confirmada em `chamfer_edges`). Preferiu-se
  não implementar a inventar uma assinatura adicional de baixa confiança
  além das 11 já listadas.
- **Trim/extend genéricos de entidade de esboço** (0064-0067): parcialmente
  coberto (`SketchManager.SketchTrim` seria o caminho), mas a variante
  "extend" especificamente não tem um método COM dedicado conhecido com
  confiança — `trim_sketch_entity`/`extend_sketch_entity` foram cogitados e
  descartados desta rodada em favor de `offset_sketch_entities` e
  `convert_sketch_entities`, que têm maior confiança de implementação.
- **Move/copy/rotate/scale de entidades de esboço** (0074-0077): investigado;
  nenhum nome de método COM pôde ser recordado com confiança suficiente
  (ao contrário de `SketchOffset2`/`SketchConvertEntities`/padrão de
  esboço). Preferiu-se não implementar a fabricar uma chamada sem nenhuma
  base.
- **Padrão de esboço** (0072/0073, `CreateLinearSketchStepAndRepeat`/
  `CreateCircularSketchStepAndRepeat`): cogitado para esta rodada mas
  cortado por orçamento de tempo/escopo (47 ferramentas já entregues);
  continua como lacuna conhecida para uma próxima rodada.

## Convenções seguidas

Toda ferramenta nova segue exatamente os padrões já estabelecidos em
`server.py`:
- `@mcp.tool(annotations=ToolAnnotations(readOnlyHint=..., destructiveHint=...,
  idempotentHint=..., openWorldHint=False))` com os três hints decididos
  caso a caso (leitura vs. escrita vs. destrutiva).
- Despacho `async def tool(...) -> dict: def _impl(): ... ; return await
  _run(_impl)` — nenhuma chamada COM roda fora da thread dedicada.
- Validação de argumento com `ValueError`/`FileNotFoundError`/`FileExistsError`
  ANTES de qualquer chamada COM, onde fazia sentido (10 ferramentas cobertas
  explicitamente por `tests/test_new_tools_round2.py::
  test_argument_validation_comes_before_the_first_com_call`).
- Reaproveitamento de helpers já existentes em vez de duplicar lógica:
  `_sketch_create`/`_sketch_verdict`/`_sketch_segment_geometry` (ferramentas
  de esboço), `_capture_picked_components`/`_report_component_movement`/
  `_mechanism_pose_reminder` (mates), `_walk_assembly`/`_component_model_doc`/
  `_read_custom_properties` (BOM — via o novo helper partilhado
  `_bom_rows_with_properties`), `_measure_model_doc`, `_entity_direction_local`-
  style (extração de `CylinderParams`), `_resolve_write_path` (toda
  ferramenta de escrita em arquivo).
- `TOOL_INVENTORY.csv` e `tests/tool_names.json`/`manifest.json` atualizados
  na mesma rodada de commits, nunca depois.

## Git

Trabalho dividido em 11 commits lógicos (um por lote temático), cada um
rodando a suíte de testes completa antes do commit seguinte:

1. Esboço/relações (11 ferramentas)
2. Features sólidas/chanfro/multicorpos (7 ferramentas)
3. Montagem/mates mecânicos (4 ferramentas)
4. Vista explodida/configuração/BOM compare (5 ferramentas)
5. Folga/alinhamento, folha/GD&T, material/propriedades (8 ferramentas)
6. BOM export/validação/fixadores (3 ferramentas)
7. Verificação de ajuste mecânico (2 ferramentas)
8. Importação/exportação (2 ferramentas)
9. Auditoria de árvore/snapshot (3 ferramentas)
10. Auditoria de montagem/checklist (2 ferramentas)
11. Testes de contrato + `TOOL_INVENTORY.csv`

Branch: `ccr-4fb8d1c2-rq9rjj`, enviado para `origin` ao final da rodada.
