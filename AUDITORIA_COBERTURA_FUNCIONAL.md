# AUDITORIA COMPLETA DE COBERTURA FUNCIONAL — MCP SolidWorks

Executada contra o checklist enviado pelo usuário (`checklist_auditoria_mcp_solidworks-1.txt`, v1.0), seguindo suas Fases 1–4. **Nenhum código foi alterado antes deste relatório.**

## A. RESUMO EXECUTIVO

- **Estado geral:** o MCP cobre bem a modelagem sólida clássica (esboço básico, extrusão/corte/revolução/sweep/loft, filete/chanfro/shell, padrões, furos, montagem/mates, chapa metálica, estrutura soldada, medição, propriedades, arquivo/exportação). **Não cobre nada** de superfícies (Categoria L), simulação/FEA (Categoria N) ou CAM/fabricação (Categoria P) — três categorias inteiras, 0% implementadas, por decisão de escopo do projeto (confirmado no `.claude/CLAUDE.md`: "Sem FEA/análise de tensão real... essa ferramenta não está exposta aqui"), não por descuido.
- **159 ferramentas MCP registradas** (158 incondicionais + `execute_python` condicional).
- **354 itens funcionais/qualitativos auditados** (categorias A–S do checklist; a Categoria T — fluxos ponta-a-ponta — é avaliada separadamente abaixo por não ter sido mapeada item-a-item pelo agente de exploração).
- **Distribuição de status** (ver tabela completa na Seção D): 102 IMPLEMENTADO E TESTADO (28,8%), 57 IMPLEMENTADO NÃO TESTADO, 63 PARCIAL, 17 EXPERIMENTAL/INSTÁVEL, 115 AUSENTE. **Cobertura potencial** (testado+não-testado) = 44,9%.
- **Principais riscos (P0, 7 itens):** nenhum é "o servidor cria geometria errada sem avisar" — são lacunas de **proteção contra sobrescrita acidental de arquivo**, **ausência de mecanismo de desfazer/undo**, **anotação `destructiveHint` que não bloqueia nada de fato**, e mensagens de erro genéricas demais em alguns casos de falha de corte/solver (ver Seção E).
- **Limitação desta auditoria:** feita 100% por leitura estática do código (este ambiente não tem SolidWorks instalado). "IMPLEMENTADO, NÃO TESTADO" significa exatamente isso — plausível pelo código, não confirmado ao vivo nesta rodada. Ver `LIVE_TESTS_REQUIRED.md` (já existente) para o que precisa de um operador com SolidWorks real.

## FASE 1 — INVENTÁRIO

- **Repositório:** `h4child/mcp-solidworks`. **Branch auditada:** `main`, commit no momento desta auditoria (ver `git log -1`).
- **Linguagem/arquitetura:** servidor MCP em Python (`server.py`, ~16.460 linhas), framework `mcp.server.fastmcp.FastMCP`. Ponto de entrada: `if __name__ == "__main__": mcp.run()`.
- **Comunicação com o SolidWorks:** COM via `win32com.client`/`pythoncom` (Windows-only). Objetos COM são apartment-threaded, então todo trabalho roda numa única `ThreadPoolExecutor(max_workers=1)` dedicada (`_run`, `server.py` ~linha 341), com `CoInitialize`/`CoUninitialize` na thread do executor.
- **Catálogo de ferramentas:** **158 `@mcp.tool` incondicionais + `execute_python`** (registrado só se `SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1`) = **159 no total**. Ver `TOOL_INVENTORY.csv` (gerado mecanicamente do código atual) para nome, categoria, schema, tipo leitura/escrita/destrutiva e referências de teste de cada uma.
- **Recursos/prompts MCP:** 3 `@mcp.resource`/`@mcp.prompt` (inclui `solidworks://tool-status`, que lê o próprio `README.md` em runtime — a matriz OK/EXP do README é literalmente o que a IA cliente vê ao vivo).
- **Testes existentes:** `tests/test_*.py` (unitários/contrato/mock — rodam sem SolidWorks, 1358 testes passando neste ambiente) e `tests/run_*_live_test.py` (exigem SolidWorks real — **não executados nesta auditoria**, este ambiente não tem a aplicação instalada).
- **Dependência de plataforma:** `pywin32` só instala em Windows (`sys_platform == 'win32'` no `pyproject.toml`); em qualquer outro SO, `tests/conftest.py` falseia os módulos COM só para permitir a suíte de testes correr (introspecção, não comportamento real).
- **Licenças/add-ins:** o código só referencia a API COM base do SolidWorks (`ISldWorks`/`IModelDoc2`/etc.). **Nenhuma referência** a add-ins licenciados separadamente (SolidWorks Simulation/CosmosWorks, CAMWorks/SolidWorks CAM, PhotoView360 como motor de render) foi encontrada — ver Categorias L, N, P e H15/H16 abaixo.
- **TODOs/experimental:** o próprio README mantém uma matriz "OK" (testado ao vivo) vs "EXP" (experimental/instável) para 137 das 159 ferramentas; 48 (as 47 já identificadas na auditoria de bugs anterior + `execute_python`) não têm status ao vivo registrado — ver `TOOL_INVENTORY.csv` coluna `status_readme`.

## RESUMO DA METODOLOGIA (Fase 2)

Um agente de exploração (`Explore`, leitura apenas) cruzou cada um dos itens do checklist (categorias A–S, 354 itens funcionais/qualitativos — a Categoria T de fluxos ponta-a-ponta é tratada separadamente na Fase 3) contra `server.py`, citando arquivo:linha de cada ferramenta ou mecanismo encontrado. Eu then classifiquei cada achado na taxonomia exigida (seção 19 do checklist), cruzando com `TOOL_INVENTORY.csv`/README para decidir "testado" vs "não testado" vs "experimental":

- **ENCONTRADO** + ferramenta com status `OK` no README (= já confirmado ao vivo antes desta sessão) → **IMPLEMENTADO E TESTADO**.
- **ENCONTRADO** + ferramenta com status `EXP` no README → **EXPERIMENTAL/INSTÁVEL**.
- **ENCONTRADO** + ferramenta sem status registrado no README → **IMPLEMENTADO, NÃO TESTADO**.
- **PARCIAL** (do agente) → **PARCIAL**.
- **NÃO ENCONTRADO** (do agente) → **AUSENTE**.

Nenhum item foi marcado **NÃO VERIFICÁVEL** (a leitura estática do código bastou para decidir presença/ausência em 100% dos casos) nem **NÃO APLICÁVEL** (mesmo FEA/CAM/Motion-física, que são decisões de escopo do projeto, são tecnicamente possíveis via API SolidWorks com os add-ins certos — por isso ficam como `AUSENTE`, não `NÃO APLICÁVEL`, com a ressalva de escopo registrada na Fase 4/prioridades).

## B. INVENTÁRIO DE FERRAMENTAS

Ver `TOOL_INVENTORY.csv` (159 linhas, já existente no repositório desta auditoria anterior, revalidado agora). Resumo:

| Métrica | Valor |
|---|---|
| Total de ferramentas | 159 (158 incondicionais + `execute_python` condicional) |
| Leitura (`readOnlyHint=True`) | 38 |
| Destrutivas (`destructiveHint=True`) | 16 |
| Escrita (demais) | 105 |
| Status `OK` no README (live-tested antes desta sessão) | 75 |
| Status `EXP` no README | 13 |
| Sem status no README (`NAO DOCUMENTADO`) | 47 + `execute_python` |

## C. MATRIZ COMPLETA

### CATEGORIA A — ESBOÇOS E GEOMETRIA 2D

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| A01 | criar esboço em plano/face | IMPLEMENTADO E TESTADO | create_sketch, create_sketch_on_face | 4604, 4629 | plano nominal ou face por coordenada | P1 |
| A02 | selecionar plano/face e validar seleção | IMPLEMENTADO E TESTADO | create_sketch_on_face / hole_wizard (_planar_face_at_point) | 4629, 6326 | valida via Select4/GetClosestPointOn, erro se não achar | P1 |
| A03 | criar ponto | PARCIAL | hole_wizard usa CreatePoint internamente | 6332 | não há tool dedicada "add_sketch_point" | P1 |
| A04 | criar linha | IMPLEMENTADO E TESTADO | draw_line | 4823 | - | P1 |
| A05 | linha de centro/construção | IMPLEMENTADO, NAO TESTADO | draw_centerline | 4869 | usada como eixo de revolve | P1 |
| A06 | retângulo por dois cantos | IMPLEMENTADO E TESTADO | draw_rectangle | 4975 | dois cantos x1y1/x2y2 | P1 |
| A07 | retângulo centralizado | AUSENTE | - | - | draw_rectangle só aceita 2 cantos, não "center rect" | P1 |
| A08 | paralelogramo por parâmetros | AUSENTE | - | - | - | P1 |
| A09 | círculo por centro+raio | IMPLEMENTADO E TESTADO | draw_circle | 4926 | - | P1 |
| A10 | arco por parâmetros | IMPLEMENTADO E TESTADO | draw_arc | 5066 | centro+raio+ângulos | P1 |
| A11 | arco de três pontos | AUSENTE | - | - | draw_arc é center-based, não 3-point | P1 |
| A12 | elipse | AUSENTE | - | - | nenhuma chamada CreateEllipse | P1 |
| A13 | polígono regular | IMPLEMENTADO E TESTADO | draw_polygon | 5259 | - | P1 |
| A14 | ranhura/slot reto | EXPERIMENTAL/INSTAVEL | função interna usada por create_knurl (CreateSketchSlot) | 15466 | não é tool pública própria para slot genérico; é uso interno | P1 |
| A15 | ranhura curva | AUSENTE | - | - | CreateSketchSlot encontrado só suporta variante usada internamente | P1 |
| A16 | spline/curva livre | IMPLEMENTADO, NAO TESTADO | draw_spline | 5122 | - | P1 |
| A17 | texto em esboço | AUSENTE | - | - | nenhuma chamada InsertSketchText/SketchText | P1 |
| A18 | offset de entidades | AUSENTE | - | - | nenhuma chamada SketchOffset/InsertOffsetCurve | P1 |
| A19 | aparar (trim) entidades | AUSENTE | - | - | "trim" só aparece em trim_extend_structural (weldment 3D), não sketch 2D | P1 |
| A20 | estender (extend) entidades | PARCIAL | trim_extend_structural | 6577 | é para membros de estrutura soldada (3D), não para entidades de esboço 2D | P1 |
| A21 | dividir (split) entidades | AUSENTE | - | - | split_body existe mas é para sólidos, não sketch | P1 |
| A22 | converter entidades de face/aresta para esboço | AUSENTE | - | - | nenhuma chamada ConvertEntities | P1 |
| A23 | filete de esboço | AUSENTE | - | - | fillet_edges é 3D (arestas sólidas), não sketch fillet | P1 |
| A24 | chanfro de esboço | AUSENTE | - | - | chamfer_edges é 3D | P1 |
| A25 | detectar/corrigir contorno aberto/inválido | PARCIAL | draw_profile verifica segmentos faltantes e levanta erro | 5347-5462 | detecta mas não corrige; não é verificação de contorno aberto em sketch arbitrário | P1 |
| A26 | detectar contorno sobreposto/autointersectado | AUSENTE | - | - | - | P1 |
| A27 | sair do esboço e validar estado | IMPLEMENTADO E TESTADO | close_sketch, get_sketch_status | 4690, 4719 | status só diz active/name, não fully-defined | P1 |
| A28 | editar/renomear/excluir esboço | AUSENTE | - | - | não há tool para renomear/excluir sketch (delete_feature pode genericamente apagar feature, incluindo sketch, mas não testado/documentado para isso) | P1 |

### CATEGORIA B — COTAS, RESTRIÇÕES, PARAMETRIZAÇÃO

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| B01 | cota linear | IMPLEMENTADO E TESTADO | add_sketch_dimension | 5466 | genérica, cobre linear | P1 |
| B02 | cota raio/diâmetro | PARCIAL | add_sketch_dimension (seleciona arco/círculo e cota) | 5466 | não há parâmetro explícito raio-vs-diâmetro, depende da entidade selecionada | P1 |
| B03 | cota ângulo | PARCIAL | add_sketch_dimension (se selecionar 2 segmentos) | 5466 | não testado/documentado explicitamente para ângulo | P1 |
| B04 | cota distância entre pontos | IMPLEMENTADO E TESTADO | add_sketch_dimension | 5466 | seleciona 2 pontos | P1 |
| B05 | relação horizontal | IMPLEMENTADO E TESTADO | add_sketch_relation("horizontal") | 5525-5544 | - | P1 |
| B06 | vertical | IMPLEMENTADO E TESTADO | add_sketch_relation("vertical") | 5525-5544 | - | P1 |
| B07 | coincidente | IMPLEMENTADO E TESTADO | add_sketch_relation("coincident") | 5525-5544 | - | P1 |
| B08 | paralela | IMPLEMENTADO E TESTADO | add_sketch_relation("parallel") | 5525-5544 | - | P1 |
| B09 | perpendicular | IMPLEMENTADO E TESTADO | add_sketch_relation("perpendicular") | 5525-5544 | - | P1 |
| B10 | tangente | IMPLEMENTADO E TESTADO | add_sketch_relation("tangent") | 5525-5544 | - | P1 |
| B11 | concêntrica | IMPLEMENTADO E TESTADO | add_sketch_relation("concentric") | 5525-5544 | - | P1 |
| B12 | igual | IMPLEMENTADO E TESTADO | add_sketch_relation("equal") | 5525-5544 | - | P1 |
| B13 | simetria | IMPLEMENTADO E TESTADO | add_sketch_relation("symmetric") | 5525-5544 | - | P1 |
| B14 | ponto médio | IMPLEMENTADO E TESTADO | add_sketch_relation("midpoint") | 5525-5544 | - | P1 |
| B15 | fixar/liberar entidade | PARCIAL | add_sketch_relation("fixed") | 5525-5544 | só fixar; não há "liberar"/delete relation dedicado | P1 |
| B16 | detectar esboço subdefinido | AUSENTE | - | - | - | P1 |
| B17 | totalmente definido | AUSENTE | - | - | get_sketch_status não reporta isso | P1 |
| B18 | sobredefinição/conflito | AUSENTE | - | - | - | P1 |
| B19 | editar cota existente | PARCIAL | add_sketch_dimension(value=...) cria+define valor numa única chamada | 5510-5516 | não há tool para localizar/editar cota já existente por nome | P1 |
| B20 | parâmetros/variáveis nomeadas | IMPLEMENTADO, NAO TESTADO | add_equation/list_equations cobrem "variáveis" via equations do SW | 14538, 14517 | - | P1 |
| B21 | equações entre cotas | IMPLEMENTADO, NAO TESTADO | add_equation, set_equation, delete_equation, list_equations | 14538, 14564, 14587, 14517 | - | P1 |
| B22 | alterar parâmetro e reconstruir | IMPLEMENTADO, NAO TESTADO | set_equation + rebuild_model | 14564, 14241 | - | P1 |
| B23 | preservar intenção de projeto | PARCIAL | relações+equações existem, mas nada garante robustez de intenção de projeto automaticamente | - | qualitativo | P1 |
| B24 | informar falha de solver | PARCIAL | add_equation(solve=True) e set_equation reportam erro se rebuild falhar; add_sketch_dimension levanta RuntimeError se AddDimension2 falhar | 14538, 5501-5505 | não há mensagem específica de "solver sketch" (DOF conflict) | P0 |

### CATEGORIA C — SÓLIDOS/ADIÇÃO DE MATERIAL

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| C01 | extrusão (boss) | IMPLEMENTADO E TESTADO | extrude_sketch | 5572 | - | P2 |
| C02 | extrusão cega por profundidade | IMPLEMENTADO E TESTADO | extrude_sketch(depth) | 5572 | end_cond=0 Blind | P2 |
| C03 | extrusão passante/até face/superfície | AUSENTE | - | - | extrude_sketch só suporta Blind/MidPlane; não há through-all/up-to-face para boss (cut_extrude tem through_all, extrude não) | P2 |
| C04 | extrusão simétrica/duas direções | IMPLEMENTADO E TESTADO | extrude_sketch(both_directions=True) | 5572, 5590 | MidPlane | P2 |
| C05 | revolução | IMPLEMENTADO E TESTADO | revolve_sketch | 5724 | - | P2 |
| C06 | revolução parcial/completa | IMPLEMENTADO E TESTADO | revolve_sketch(angle) | 5724, 5735 | - | P2 |
| C07 | sweep (boss) | IMPLEMENTADO E TESTADO | sweep_sketch | 5777 | - | P2 |
| C08 | loft (boss) | IMPLEMENTADO E TESTADO | loft_sketches | 5813 | - | P2 |
| C09 | loft múltiplos perfis | IMPLEMENTADO E TESTADO | loft_sketches(sketch_names: list) | 5813 | aceita lista de >2 sketches | P2 |
| C10 | loft com curvas-guia | AUSENTE | - | - | loft_sketches não aceita guide curves | P2 |
| C11 | boundary/contorno feature | AUSENTE | - | - | nenhuma chamada InsertProtrusionBoundary | P2 |
| C12 | extrusão fina (thin) | AUSENTE | - | - | parâmetro IsThin sempre False nas chamadas FeatureExtrusion2/FeatureRevolve2 | P2 |
| C13 | nervura/rib | EXPERIMENTAL/INSTAVEL | add_rib | 7954 | - | P2 |
| C14 | criar múltiplos corpos | IMPLEMENTADO E TESTADO | extrude_sketch(merge=False) | 5572-5595 | merge=False mantém corpo separado | P2 |
| C15 | controlar unir vs manter separado | IMPLEMENTADO E TESTADO | extrude_sketch(merge=...), combine_bodies | 5572, 8363 | - | P2 |
| C16 | validar sólido fechado/válido | IMPLEMENTADO, NAO TESTADO | validate_model | 14268 | - | P2 |
| C17 | tratar perfil inválido | IMPLEMENTADO E TESTADO | extrude_sketch/cut_extrude/revolve_sketch levantam RuntimeError se feat is None; draw_profile valida segmentos | 5596-5600, 5678-5682 | - | P2 |
| C18 | reconstruir árvore após operação | IMPLEMENTADO, NAO TESTADO | rebuild_model | 14241 | - | P2 |

### CATEGORIA D — CORTES/FUROS/REMOÇÃO

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| D01 | corte por extrusão | IMPLEMENTADO E TESTADO | cut_extrude | 5621 | - | P2 |
| D02 | corte cego | IMPLEMENTADO E TESTADO | cut_extrude(depth) | 5621, 5666-5668 | - | P2 |
| D03 | corte passante | IMPLEMENTADO E TESTADO | cut_extrude(through_all=True) | 5621, 5663-5665 | - | P2 |
| D04 | corte até face/superfície | AUSENTE | - | - | end_cond só suporta Blind/ThroughAll/MidPlane, sem UpToSurface/UpToFace | P2 |
| D05 | corte simétrico/duas direções | IMPLEMENTADO E TESTADO | cut_extrude(both_directions=True) | 5621, 5667 | e workaround documentado para through_all+both_directions | P2 |
| D06 | corte por revolução | IMPLEMENTADO E TESTADO | revolve_sketch(cut=True) | 5724, 5749 | - | P2 |
| D07 | corte por sweep | AUSENTE | - | - | sweep_sketch só cria boss (InsertProtrusionSwept4), não há cut sweep | P2 |
| D08 | corte por loft | AUSENTE | - | - | loft_sketches só boss | P2 |
| D09 | corte por superfície/plano | AUSENTE | - | - | categoria L inexistente | P2 |
| D10 | furo simples | IMPLEMENTADO E TESTADO | hole_wizard(hole_type="simple") | 6172, 6224 | - | P2 |
| D11 | hole wizard | IMPLEMENTADO E TESTADO | hole_wizard | 6172 | - | P2 |
| D12 | furo passante | IMPLEMENTADO E TESTADO | hole_wizard(depth=0 → through) | 6172, 6195, 6236 | - | P2 |
| D13 | furo cego | IMPLEMENTADO E TESTADO | hole_wizard(depth>0) | 6172, 6236 | - | P2 |
| D14 | furo escareado | IMPLEMENTADO E TESTADO | hole_wizard(hole_type="countersink") | 6172, 6226 | - | P2 |
| D15 | furo counterbore | IMPLEMENTADO E TESTADO | hole_wizard(hole_type="counterbore") | 6172, 6225 | - | P2 |
| D16 | furo roscado | IMPLEMENTADO E TESTADO | hole_wizard(hole_type="tapped") | 6172, 6227 | - | P2 |
| D17 | rosca cosmética | EXPERIMENTAL/INSTAVEL | add_cosmetic_thread | 7607 | - | P2 |
| D18 | rosca modelada (hélice real) | EXPERIMENTAL/INSTAVEL | add_thread_feature (usa create_helix) | 7520, 7415 | - | P2 |
| D19 | rasgos/canais | PARCIAL | cut_extrude + draw_profile/draw_rectangle cobrem geometria genérica de canal | 5621 | sem tool dedicada "slot cut" | P2 |
| D20 | bolsões/cavidades | IMPLEMENTADO E TESTADO | cut_extrude (blind) cobre pocket | 5621 | - | P2 |
| D21 | remoção múltiplas regiões | PARCIAL | draw_profile permite múltiplos loops na mesma sketch (múltiplas chamadas) + cut_extrude | 5390-5392 | - | P2 |
| D22 | corte em múltiplos corpos | PARCIAL | cut_extrude usa FeatureCut4 com UseFeatScope/UseAutoSelect ligados; não há seleção explícita de corpos-alvo | 5670-5677 | - | P2 |
| D23 | validar profundidade/direção | IMPLEMENTADO E TESTADO | cut_extrude mede actual_depth e compara (verified) | 5707-5718 | - | P2 |
| D24 | detectar corte que não intersecta | PARCIAL | feat is None → RuntimeError genérico | 5678-5682 | não é mensagem específica "não intersecta" | P0 |
| D25 | validar volume antes/depois | PARCIAL | measure_body pode ser chamado antes/depois manualmente | 13948 | não é automático dentro das tools de corte | P0 |

### CATEGORIA E — MODIFICAÇÃO DE SÓLIDOS

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| E01 | filete de aresta | IMPLEMENTADO E TESTADO | fillet_edges | 5863 | - | P1 |
| E02 | filete raio constante | IMPLEMENTADO E TESTADO | fillet_edges(radius) | 5863 | - | P1 |
| E03 | filete variável | AUSENTE | - | - | só raio constante | P1 |
| E04 | filete de face | AUSENTE | - | - | - | P1 |
| E05 | chanfro distância/ângulo | IMPLEMENTADO E TESTADO | chamfer_edges(distance, angle) | 5912 | - | P1 |
| E06 | chanfro duas distâncias | AUSENTE | - | - | chamfer_edges só distância+ângulo | P1 |
| E07 | shell/casca | IMPLEMENTADO E TESTADO | shell_body | 5971 | - | P1 |
| E08 | draft/inclinação | AUSENTE | - | - | nenhuma chamada InsertFeatureDraft | P1 |
| E09 | thicken/espessamento de superfície | AUSENTE | - | - | - | P1 |
| E10 | combinar/unir corpos | EXPERIMENTAL/INSTAVEL | combine_bodies(operation="add") | 8363, 8379 | - | P1 |
| E11 | subtrair corpo | EXPERIMENTAL/INSTAVEL | combine_bodies(operation="subtract") | 8363, 8379 | - | P1 |
| E12 | intersectar corpos | EXPERIMENTAL/INSTAVEL | combine_bodies(operation="common") | 8363, 8379 | - | P1 |
| E13 | dividir corpo (split_body) | EXPERIMENTAL/INSTAVEL | split_body | 8675 | - | P1 |
| E14 | salvar corpos separados em arquivo | PARCIAL | save_document combinado com split_body manualmente; não há "save bodies as separate files" dedicado | 2431, 8675 | - | P1 |
| E15 | mover/copiar corpo | IMPLEMENTADO E TESTADO | move_copy_body | 8214 | - | P1 |
| E16 | excluir/substituir face | AUSENTE | - | - | sem DeleteFace2/ReplaceFace | P1 |
| E17 | reparar geometria importada | AUSENTE | - | - | sem FeatureManager.InsertFixModelFeatures ou similar | P1 |
| E18 | plano de referência | IMPLEMENTADO E TESTADO | create_reference_plane | 8473 | - | P1 |
| E19 | eixo de referência | EXPERIMENTAL/INSTAVEL | create_reference_axis | 8602 | - | P1 |
| E20 | ponto de referência | AUSENTE | - | - | sem tool "create_reference_point" | P1 |
| E21 | sistema de coordenadas | AUSENTE | - | - | sem CreateCoordinateSystem/tool dedicada | P1 |
| E22 | geometria de referência a partir de face/aresta | PARCIAL | create_reference_plane/create_reference_axis aceitam "reference" por nome/face (não totalmente confirmado) | 8473, 8602 | - | P1 |
| E23 | detectar falha de reconstrução | IMPLEMENTADO, NAO TESTADO | rebuild_model/validate_model reportam erro | 14241, 14268 | - | P0 |

### CATEGORIA F — PADRÕES/ESPELHAMENTO

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| F01 | padrão linear 1 direção | IMPLEMENTADO E TESTADO | linear_pattern | 6053 | - | P2 |
| F02 | padrão linear 2 direções | PARCIAL | linear_pattern(direction="x"/"y"/"z") — só 1 direção por chamada; 2 direções exigiria 2 chamadas/feature próprio | 6053 | - | P2 |
| F03 | padrão circular | IMPLEMENTADO E TESTADO | circular_pattern | 6126 | - | P2 |
| F04 | espelhar feature | IMPLEMENTADO E TESTADO | mirror_feature | 8113 | - | P2 |
| F05 | espelhar corpo | IMPLEMENTADO E TESTADO | mirror_body | 8162 | - | P2 |
| F06 | espelhar em plano de referência | IMPLEMENTADO E TESTADO | mirror_feature/mirror_body aceitam plano (incl. create_reference_plane) | 8113, 8162 | - | P2 |
| F07 | padrão dirigido por curva | AUSENTE | - | - | sem FeatureCurveDrivenPattern | P2 |
| F08 | padrão dirigido por esboço | AUSENTE | - | - | sem SketchDrivenPattern | P2 |
| F09 | padrão de componentes em montagem | EXPERIMENTAL/INSTAVEL | create_assembly_pattern | 11219 | - | P2 |
| F10 | controlar quantidade/espaçamento/direção/eixo | IMPLEMENTADO E TESTADO | linear_pattern/circular_pattern params (spacing, count, direction, axis) | 6053, 6126 | - | P2 |
| F11 | suprimir instâncias | PARCIAL | suppress_component existe para componentes de montagem; não confirmado controle de instância específica dentro de linear/circular_pattern | 3312 | - | P2 |
| F12 | editar padrão existente | AUSENTE | - | - | sem tool para editar pattern já criado (só delete_feature e recriar) | P2 |
| F13 | detectar falha de instância/interferência | PARCIAL | feat is None → erro genérico em linear/circular_pattern | 6053-6126 | sem relatório específico de instâncias que falharam | P2 |

### CATEGORIA G — MATERIAIS FÍSICOS/PROPRIEDADES

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| G01 | consultar material atual | PARCIAL | get_custom_properties não lê material; lookup_material_properties lê biblioteca, não o material ATUAL do doc | 13795, 11415 | falta leitura do material já aplicado (_native_material_name é interna, usada só dentro de set_material) | P2 |
| G02 | atribuir material biblioteca | IMPLEMENTADO E TESTADO | set_material | 11494 | - | P2 |
| G03 | atribuir material personalizado | PARCIAL | set_material(database=caminho .sldmat customizado) | 11494, 11512 | aceita qualquer .sldmat, mas não cria material novo | P2 |
| G04 | criar/editar propriedades de material | AUSENTE | - | - | só leitura (lookup_material_properties), sem escrita de propriedades de material customizado | P2 |
| G05 | atribuir material diferente por corpo/componente | PARCIAL | set_material só atua no documento/part ativo (doc_type==1) | 11509-11510 | sem per-body | P2 |
| G06 | ler densidade | IMPLEMENTADO, NAO TESTADO | lookup_material_properties, measure_body (via mass/volume) | 11415, 13948 | - | P2 |
| G07 | ler propriedades mecânicas | IMPLEMENTADO, NAO TESTADO | lookup_material_properties (elastic modulus, Poisson, yield, tensile) | 11415-11422 | - | P2 |
| G08 | calcular massa | IMPLEMENTADO, NAO TESTADO | measure_body | 13948 | - | P2 |
| G09 | calcular volume | IMPLEMENTADO, NAO TESTADO | measure_body | 13948 | - | P2 |
| G10 | calcular área superfície | IMPLEMENTADO, NAO TESTADO | measure_body | 13948 | - | P2 |
| G11 | calcular centro de massa | IMPLEMENTADO, NAO TESTADO | measure_body | 13948 | - | P2 |
| G12 | calcular momento de inércia | AUSENTE | - | - | measure_body não retorna tensor de inércia | P2 |
| G13 | atualizar propriedades após mudança | PARCIAL | set_material chama ForceRebuild3; measure_body pode ser rechamado manualmente | 11556 | - | P2 |
| G14 | distinguir material físico de aparência | IMPLEMENTADO E TESTADO | set_material (físico) vs set_appearance/apply_metal_finish (visual) são tools distintas, docstring de apply_metal_finish avisa "Use set_material as well when mass/BOM material data is required" | 11494, 12069 | - | P2 |

### CATEGORIA H — APARÊNCIA/COR/TEXTURA/RENDER

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| H01 | consultar aparência atual | IMPLEMENTADO, NAO TESTADO | get_appearance_properties | 11775 | - | P3 |
| H02 | aplicar cor | IMPLEMENTADO E TESTADO | set_appearance(red,green,blue) | 11670 | - | P3 |
| H03 | aplicar aparência à peça/corpo/face | IMPLEMENTADO E TESTADO | set_appearance/set_appearance_properties(target="body"/"face") | 11670, 11811 | - | P3 |
| H04 | remover aparência | PARCIAL | remove_texture remove textura; não há "remove_appearance/color" dedicado (poderia reaplicar default) | 12023 | - | P3 |
| H05 | aço escovado | IMPLEMENTADO, NAO TESTADO | apply_metal_finish("brushed_steel") | 12056, 12078 | - | P3 |
| H06 | metal polido/cromado | IMPLEMENTADO, NAO TESTADO | apply_metal_finish("polished_steel"/"chrome") | 12056, 12076-12077 | - | P3 |
| H07 | metal fundido | PARCIAL | apply_metal_finish("cast_iron") | 12056, 12079 | "cast iron" aproxima mas não é genérico "metal fundido" | P3 |
| H08 | plástico/borracha/madeira/pintura | PARCIAL | apply_p2m_appearance(appearance_path=<.p2m da biblioteca SW>) | 12218 | genérico — depende do .p2m fornecido pelo usuário/caller, não presets dedicados | P3 |
| H09 | controlar brilho/reflexão/aspereza | IMPLEMENTADO, NAO TESTADO | set_appearance_properties(ambient,diffuse,specular,shininess) | 11811 | - | P3 |
| H10 | transparência | IMPLEMENTADO, NAO TESTADO | set_appearance_properties(transparency) | 11811-11815 | - | P3 |
| H11 | mapa de textura | IMPLEMENTADO, NAO TESTADO | apply_texture(texture_path) | 11916 | - | P3 |
| H12 | escala/orientação de textura | IMPLEMENTADO, NAO TESTADO | apply_texture(u_scale,v_scale,angle) | 11916-11921 | - | P3 |
| H13 | decalque/logotipo | PARCIAL | apply_texture(target="face") serve como decal de imagem numa face | 11916, 11989 | não é "decal" nativo do SW (DecalFeatureData), é textura de material | P3 |
| H14 | gravar/relevar texto na geometria (feature real) | EXPERIMENTAL/INSTAVEL | create_knurl usa Wrap feature com swWrapSketchType_Engrave (geometria real, não textura) | 7690, 7826 | - | P3 |
| H15 | configurar cena/iluminação/câmera | AUSENTE | - | - | - | P3 |
| H16 | gerar renderização/imagem | PARCIAL | capture_viewport só captura o viewport atual (screenshot), não PhotoView360 render | 13998 | - | P3 |
| H17 | persistência da aparência após salvar | PARCIAL | save_document salva o doc inteiro incluindo aparências nativas (set_appearance/apply_metal_finish usam canais nativos, não texturas externas) | 2431, 12099-12101 | não testado explicitamente round-trip | P3 |

### CATEGORIA I — MONTAGENS/RELAÇÕES MECÂNICAS

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| I01 | criar montagem | IMPLEMENTADO E TESTADO | create_new_assembly | 2182 | - | P2 |
| I02 | inserir peça | IMPLEMENTADO E TESTADO | insert_component | 2503 | - | P2 |
| I03 | inserir submontagem | IMPLEMENTADO E TESTADO | insert_component (aceita .sldasm também) | 2503 | - | P2 |
| I04 | inserir múltiplas instâncias | PARCIAL | insert_component chamado múltiplas vezes; sem parâmetro count | 2503 | - | P2 |
| I05 | criar peça no contexto da montagem | AUSENTE | - | - | sem InsertNewPart in-context | P2 |
| I06 | fixar/liberar componente | IMPLEMENTADO, NAO TESTADO | fix_component, float_component | 3272, 3285 | - | P2 |
| I07 | posicionar por transformação | IMPLEMENTADO, NAO TESTADO | set_component_transform | 3092 | - | P2 |
| I08 | mate coincidente/concêntrico/paralelo/perpendicular/distância/ângulo/tangente/largura-simetria | EXPERIMENTAL/INSTAVEL | add_mate/add_mate_by_name (mate_type cobre esses); add_advanced_mate cobre largura/simetria | 3598, 15730, 10732 | - | P2 |
| I09 | relação engrenagem | EXPERIMENTAL/INSTAVEL | add_advanced_mate (gear) ou mate_type específico | 10732 | - | P2 |
| I10 | cremalheira-pinhão | IMPLEMENTADO E TESTADO | add_rack_pinion_mate | 11068 | - | P2 |
| I11 | parafuso | IMPLEMENTADO E TESTADO | add_screw_mate | 10964 | - | P2 |
| I12 | came/seguidor | IMPLEMENTADO E TESTADO | add_cam_follower_mate | 10850 | - | P2 |
| I13 | editar/remover mate | PARCIAL | delete_mate existe; "editar" mate não encontrado (só delete+recriar) | 10697 | - | P2 |
| I14 | detectar componente subdefinido | AUSENTE | - | - | - | P2 |
| I15 | substituir componente preservando referências | AUSENTE | - | - | sem ReplaceComponents2 | P2 |
| I16 | suprimir/retomar componente | IMPLEMENTADO, NAO TESTADO | suppress_component/unsuppress_component | 3312, 3330 | - | P2 |
| I17 | padrão de componentes | EXPERIMENTAL/INSTAVEL | create_assembly_pattern | 11219 | - | P2 |
| I18 | vista explodida | EXPERIMENTAL/INSTAVEL | create_exploded_view | 10604 | - | P2 |
| I19 | interferência estática | IMPLEMENTADO E TESTADO | interference_check, interference_report | 11168, 15856 | - | P2 |
| I20 | medir folga entre componentes | PARCIAL | measure_body mede corpo único; interference_check detecta interferência mas "folga/clearance" positiva não confirmada | 13948, 11168 | - | P2 |
| I21 | gerar BOM | IMPLEMENTADO, NAO TESTADO | extract_assembly_bom, insert_bom_table | 2901, 10460 | - | P2 |
| I22 | atualizar referência após mudança de arquivo | PARCIAL | get_document_dependencies lista dependências; sem "update reference"/ReplaceReferencedDocument explícito | 14325 | - | P2 |
| I23 | salvar e reabrir montagem | IMPLEMENTADO E TESTADO | save_document, open_document | 2431, 2387 | - | P2 |

### CATEGORIA J — CHAPA METÁLICA

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| J01 | base de chapa com espessura | IMPLEMENTADO E TESTADO | create_base_flange | 6976 | - | P2 |
| J02 | definir espessura/raio de dobra | PARCIAL | create_base_flange/add_sheet_metal_bend recebem thickness/radius como parâmetros | 6976, 7070 | - | P2 |
| J03 | flange de borda | IMPLEMENTADO E TESTADO | add_sheet_metal_edge_flange | 7141 | - | P2 |
| J04 | flange de base | IMPLEMENTADO E TESTADO | create_base_flange | 6976 | - | P2 |
| J05 | flange de contorno | AUSENTE | - | - | sem "contour flange" (InsertContourFlange) | P2 |
| J06 | dobra por esboço | IMPLEMENTADO E TESTADO | add_sheet_metal_bend | 7070 | - | P2 |
| J07 | aba/hem | AUSENTE | - | - | sem InsertHem | P2 |
| J08 | alívio de canto | AUSENTE | - | - | sem corner relief | P2 |
| J09 | recortes | AUSENTE | - | - | - | P2 |
| J10 | fator K/tabela de dobra | AUSENTE | - | - | nenhuma referência a KFactor/BendTable | P2 |
| J11 | desdobrar | IMPLEMENTADO E TESTADO | flatten_sheet_metal(state="flat") | 7317 | - | P2 |
| J12 | redobrar | IMPLEMENTADO E TESTADO | flatten_sheet_metal(state="folded") | 7317 | - | P2 |
| J13 | gerar planificação | IMPLEMENTADO E TESTADO | flatten_sheet_metal | 7317 | - | P2 |
| J14 | validar dimensões planificação | PARCIAL | nenhuma validação dedicada; export_flat_pattern_dxf apenas exporta | 14610 | - | P2 |
| J15 | exportar DXF/DWG | IMPLEMENTADO E TESTADO | export_flat_pattern_dxf, export_document(.dxf/.dwg) | 14610, 4542 | - | P2 |
| J16 | preservar parâmetros ao editar | PARCIAL | não testado; depende do SW nativo | - | - | P2 |

### CATEGORIA K — ESTRUTURAS SOLDADAS/PERFIS

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| K01 | estrutura a partir de esboço 2D/3D | IMPLEMENTADO E TESTADO | create_weldment_profile (junto com create_3d_sketch/draw_line_3d) | 6447, 6889, 6921 | - | P2 |
| K02 | selecionar perfil de biblioteca | IMPLEMENTADO, NAO TESTADO | create_weldment_profile (profile library path) | 6447 | - | P2 |
| K03 | tamanho/orientação do perfil | PARCIAL | create_weldment_profile params | 6447 | - | P2 |
| K04 | tubos/barras/vigas | IMPLEMENTADO, NAO TESTADO | create_tube_part, create_weldment_profile | 15633, 6447 | - | P2 |
| K05 | aparar/prolongar membros | IMPLEMENTADO, NAO TESTADO | trim_extend_structural | 6577 | - | P2 |
| K06 | resolver encontros/cantos | PARCIAL | trim_extend_structural cobre trim/extend em juntas, mas não corner-treatment nativo (gusset/end_cap separados) | 6577 | - | P2 |
| K07 | placa de extremidade | EXPERIMENTAL/INSTAVEL | add_end_cap | 6771 | - | P2 |
| K08 | reforços | IMPLEMENTADO, NAO TESTADO | add_gusset | 6652 | - | P2 |
| K09 | soldas | IMPLEMENTADO E TESTADO | add_weld_symbol (anotação de desenho) | 10231 | é símbolo de desenho, não feature de solda 3D real | P2 |
| K10 | lista de corte | PARCIAL | insert_cut_list_table (desenho) | 10558 | sem tool para extrair/gerar cut list programaticamente fora do desenho | P2 |
| K11 | extrair comprimentos/quantidades | PARCIAL | extract_assembly_bom pode listar componentes/quantidades de montagem; não é cut-list de membros de weldment dentro de uma peça única | 2901 | - | P2 |
| K12 | associar material por membro | AUSENTE | - | - | set_material é por documento, não por corpo/membro de weldment | P2 |
| K13 | atualizar lista de corte | AUSENTE | - | - | - | P2 |
| K14 | exportar dados da lista | PARCIAL | extract_assembly_bom/insert_cut_list_table exportam dados, mas não especificamente cut-list de weldment | 2901, 10558 | - | P2 |

### CATEGORIA L — SUPERFÍCIES/GEOMETRIA COMPLEXA

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| L01 | superfície plana | AUSENTE | - | - | Nenhuma chamada InsertSurface*/Knit/ThickenSurface/SurfaceFill/etc. em todo server.py. Categoria inteiramente ausente. | P3 |
| L02 | superfície extrudada | AUSENTE | - | - | idem | P3 |
| L03 | superfície revolvida | AUSENTE | - | - | idem | P3 |
| L04 | superfície varrida (sweep) | AUSENTE | - | - | idem | P3 |
| L05 | superfície loft | AUSENTE | - | - | idem | P3 |
| L06 | superfície boundary | AUSENTE | - | - | idem | P3 |
| L07 | superfície preenchida (filled) | AUSENTE | - | - | idem | P3 |
| L08 | superfície offset | AUSENTE | - | - | idem | P3 |
| L09 | estender superfície | AUSENTE | - | - | idem | P3 |
| L10 | recortar superfície (trim surface) | AUSENTE | - | - | idem | P3 |
| L11 | costurar superfícies (knit) | AUSENTE | - | - | idem | P3 |
| L12 | converter superfícies em sólido | AUSENTE | - | - | idem | P3 |
| L13 | superfície média | AUSENTE | - | - | idem | P3 |
| L14 | curvas-guia (superfície) | AUSENTE | - | - | idem | P3 |
| L15 | diagnosticar lacunas/arestas abertas | AUSENTE | - | - | idem | P3 |
| L16 | importar/reparar IGES/STEP (superfície) | AUSENTE | - | - | open_document/export_document lidam com transferência de arquivo, não reparo de superfície | P3 |
| L17 | validar continuidade geométrica | AUSENTE | - | - | idem | P3 |

### CATEGORIA M — MOVIMENTO/CINEMÁTICA

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| M01 | criar estudo de movimento | IMPLEMENTADO E TESTADO | create_motion_study | 4445 | - | P3 |
| M02 | duração/intervalo | PARCIAL | create_motion_study tem params mas não confirmado duration/timeline explícito | 4445 | - | P3 |
| M03 | motor rotativo | AUSENTE | - | - | sem AddSimulationMotor/rotary motor | P3 |
| M04 | motor linear | AUSENTE | - | - | - | P3 |
| M05 | velocidade/aceleração (entrada de motor) | AUSENTE | - | - | - | P3 |
| M06 | simular eixos/engrenagens/mecanismos | PARCIAL | mates mecânicos (gear/rack/cam/screw) existem para geometria, mas não execução de simulação de movimento | 10732-11168 | - | P3 |
| M07 | mates como vínculo cinemático | EXPERIMENTAL/INSTAVEL | add_mate/add_advanced_mate/etc. usados como vínculos | 3598-11168 | - | P3 |
| M08 | gravidade | AUSENTE | - | - | - | P3 |
| M09 | molas/amortecedores | AUSENTE | - | - | - | P3 |
| M10 | detectar colisão durante movimento | AUSENTE | - | - | interference_check é estático, não durante animação | P3 |
| M11 | medir deslocamento/velocidade/aceleração (resultado Motion) | AUSENTE | - | - | - | P3 |
| M12 | gráficos de resultado | AUSENTE | - | - | - | P3 |
| M13 | exportar animação/vídeo | AUSENTE | - | - | - | P3 |
| M14 | distinguir animação de simulação física | PARCIAL | capture_assembly_pose/restore_assembly_pose fazem pose estática; create_motion_study cria estudo mas sem motor/física real | 4156, 4254, 4445 | - | P3 |
| M15 | dependência de licença Motion | AUSENTE | - | - | sem verificação de licença Motion | P3 |
| M16 | validar se estudo foi calculado | PARCIAL | list_motion_studies lista estudos existentes | 4425 | não confirma se foi calculado/rodado | P3 |

### CATEGORIA N — SIMULAÇÃO/ANÁLISE (FEA)

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| N01 | disponibilidade de licença simulação | AUSENTE | - | - | Nenhuma referência à API de Simulation/CosmosWorks. Categoria inteiramente ausente. | P3 |
| N02 | estudo estático | AUSENTE | - | - | idem | P3 |
| N03 | material para análise | AUSENTE | - | - | idem | P3 |
| N04 | força | AUSENTE | - | - | idem | P3 |
| N05 | pressão | AUSENTE | - | - | idem | P3 |
| N06 | torque | AUSENTE | - | - | idem | P3 |
| N07 | gravidade (FEA) | AUSENTE | - | - | idem | P3 |
| N08 | apoios/restrições | AUSENTE | - | - | idem | P3 |
| N09 | contatos | AUSENTE | - | - | idem | P3 |
| N10 | malha | AUSENTE | - | - | idem | P3 |
| N11 | parâmetros de malha | AUSENTE | - | - | idem | P3 |
| N12 | executar solver | AUSENTE | - | - | idem | P3 |
| N13 | ler tensão | AUSENTE | - | - | idem | P3 |
| N14 | ler deslocamento | AUSENTE | - | - | idem | P3 |
| N15 | ler deformação | AUSENTE | - | - | idem | P3 |
| N16 | ler fator de segurança | AUSENTE | - | - | idem | P3 |
| N17 | convergência/qualidade malha | AUSENTE | - | - | idem | P3 |
| N18 | análise de frequência natural | AUSENTE | - | - | idem | P3 |
| N19 | análise de flambagem | AUSENTE | - | - | idem | P3 |
| N20 | análise de fadiga | AUSENTE | - | - | idem | P3 |
| N21 | análise térmica | AUSENTE | - | - | idem | P3 |
| N22 | análise não linear | AUSENTE | - | - | idem | P3 |
| N23 | exportar imagem/relatório (FEA) | AUSENTE | - | - | idem | P3 |
| N24 | distinguir resultado real de ilustração | AUSENTE | - | - | idem | P3 |

### CATEGORIA O — DESENHOS TÉCNICOS 2D

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| O01 | criar desenho | IMPLEMENTADO E TESTADO | create_new_drawing | 2197 | - | P2 |
| O02 | folha/tamanho/escala | PARCIAL | create_new_drawing só cria do template default; insert_drawing_view controla escala por VIEW | 2197, 2221-2241 | sem tool explícita "set_sheet_size" | P2 |
| O03 | vista frontal/superior/lateral/isométrica/projetada/auxiliar | IMPLEMENTADO E TESTADO | insert_drawing_view, insert_auxiliary_view | 2221, 9981 | - | P2 |
| O04 | seção/corte | IMPLEMENTADO, NAO TESTADO | insert_section_view | 9731 | - | P2 |
| O05 | detalhe | IMPLEMENTADO, NAO TESTADO | insert_detail_view | 9792 | - | P2 |
| O06 | vista explodida montagem (em desenho) | PARCIAL | create_exploded_view é feito na montagem (3D), não confirmado inserção direta em folha de desenho | 10604 | - | P2 |
| O07 | cota automática | PARCIAL | add_drawing_dimension insere manualmente; dimension_by_entity_ids semi-automática por IDs | 10027, 9537 | - | P2 |
| O08 | cota manual | IMPLEMENTADO, NAO TESTADO | add_drawing_dimension | 10027 | - | P2 |
| O09 | tolerância dimensional | PARCIAL | não encontrada tool dedicada para tolerância em cota (±) | 10339 | - | P2 |
| O10 | GD&T | IMPLEMENTADO, NAO TESTADO | add_gdt_symbol | 10339 | - | P2 |
| O11 | acabamento superficial (símbolo) | IMPLEMENTADO, NAO TESTADO | add_surface_finish | 10284 | - | P2 |
| O12 | símbolo de solda | IMPLEMENTADO E TESTADO | add_weld_symbol | 10231 | - | P2 |
| O13 | linha de centro | IMPLEMENTADO, NAO TESTADO | add_centerline (em desenho) | 10188 | - | P2 |
| O14 | balão+BOM | IMPLEMENTADO, NAO TESTADO | add_balloon + insert_bom_table | 10408, 10460 | - | P2 |
| O15 | notas/texto | IMPLEMENTADO E TESTADO | add_drawing_annotation | 10139 | - | P2 |
| O16 | carimbo (title block) | AUSENTE | - | - | vem do template, sem tool de edição de title block | P2 |
| O17 | projeção/unidades | PARCIAL | set_units controla unidades globais do doc, não é por-folha | 14923 | - | P2 |
| O18 | atualizar vistas | PARCIAL | rebuild_model força atualização geral; EditRebuild3 usado internamente em insert_drawing_view | 14241, 2315 | - | P2 |
| O19 | exportar PDF | IMPLEMENTADO E TESTADO | export_document(.pdf) | 4527, 4551 | - | P2 |
| O20 | exportar DWG/DXF | IMPLEMENTADO E TESTADO | export_document(.dwg/.dxf), export_flat_pattern_dxf | 4527, 14610 | - | P2 |

### CATEGORIA P — CAM/FABRICAÇÃO/IMPRESSÃO 3D

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| P01 | integração CAM instalada | AUSENTE | - | - | - | P3 |
| P02 | peça bruta/estoque | AUSENTE | - | - | - | P3 |
| P03 | fresamento | AUSENTE | - | - | - | P3 |
| P04 | torneamento | AUSENTE | - | - | - | P3 |
| P05 | ferramenta/parâmetros corte | AUSENTE | - | - | - | P3 |
| P06 | trajetória | AUSENTE | - | - | - | P3 |
| P07 | simular remoção | AUSENTE | - | - | - | P3 |
| P08 | colisão ferramenta | AUSENTE | - | - | - | P3 |
| P09 | gerar G-code | AUSENTE | - | - | - | P3 |
| P10 | exportar STL/3MF | IMPLEMENTADO E TESTADO | export_document(.stl/.3mf) | 4527, 4542-4543 | só exportação de geometria, sem contexto CAM | P3 |
| P11 | validar unidades/malha exportada | PARCIAL | export_document verifica size_bytes>0/arquivo existe, não valida malha STL | 4588-4593 | - | P3 |

### CATEGORIA Q — INSPEÇÃO/MEDIÇÃO/ÁRVORE

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| Q01 | listar documentos abertos | IMPLEMENTADO E TESTADO | list_open_documents | 2478 | - | P1 |
| Q02 | documento ativo/tipo | IMPLEMENTADO E TESTADO | get_document_info | 2462 | - | P1 |
| Q03 | listar features | IMPLEMENTADO E TESTADO | list_features | 13627 | - | P1 |
| Q04 | consultar feature (tipo/nome/parâmetro/estado) | PARCIAL | list_features retorna lista; não confirmado leitura detalhada de parâmetro por feature individual | 13627 | - | P1 |
| Q05 | corpos sólidos/superfície | PARCIAL | GetBodies2 usado internamente (combine_bodies, move_copy_body); sem tool "list_bodies" dedicada | 8392-8403 | - | P1 |
| Q06 | faces/arestas/vértices | IMPLEMENTADO, NAO TESTADO | list_faces (faces); get_view_entities para linhas/arcos em desenho | 14091, 9391 | vértices isolados não confirmados | P1 |
| Q07 | selecionar por tipo/critério | IMPLEMENTADO, NAO TESTADO | get_selection, list_faces(criteria) | 14039, 14091 | - | P1 |
| Q08 | medir distância/ângulo/raio-diâmetro/área-volume | IMPLEMENTADO, NAO TESTADO | measure_body (área/volume), get_view_dimensions (ângulo/raio em desenho) | 13948, 9458 | - | P1 |
| Q09 | massa/centro de massa | IMPLEMENTADO, NAO TESTADO | measure_body | 13948 | - | P1 |
| Q10 | bounding box | IMPLEMENTADO, NAO TESTADO | measure_body (bounding box incluso) | 13948 | - | P1 |
| Q11 | propriedades customizadas | IMPLEMENTADO E TESTADO | get_custom_properties, set_custom_property | 13795, 13809 | - | P1 |
| Q12 | configurações/supressão | IMPLEMENTADO E TESTADO | list_configurations, switch_configuration, suppress_component | 14457, 13585, 3312 | - | P1 |
| Q13 | feature com falha | PARCIAL | validate_model/rebuild_model reportam erro geral; não lista qual feature especificamente falhou | 14268, 14241 | - | P1 |
| Q14 | avisos/erros reconstrução | IMPLEMENTADO, NAO TESTADO | rebuild_model/validate_model | 14241, 14268 | - | P1 |
| Q15 | comparar geometria antes/depois | AUSENTE | - | - | sem tool de diff geométrico | P1 |
| Q16 | validar unidades | IMPLEMENTADO E TESTADO | set_units, parâmetro unit universal (to_meters/_unit_factor) | 14923 | - | P1 |

### CATEGORIA R — ARQUIVOS/CONFIGURAÇÕES/EXPORTAÇÃO

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| R01 | criar peça/montagem/desenho | IMPLEMENTADO E TESTADO | create_new_part, create_new_assembly, create_new_drawing | 2162, 2182, 2197 | - | P2 |
| R02 | abrir arquivo | IMPLEMENTADO E TESTADO | open_document | 2387 | - | P2 |
| R03 | salvar | IMPLEMENTADO E TESTADO | save_document | 2431 | - | P2 |
| R04 | salvar como (sem sobrescrever original) | PARCIAL | save_document(filepath=novo_caminho) | 2431, 2431-2461 | não confirma proteção explícita contra sobrescrever arquivo já existente | P2 |
| R05 | reconstruir | IMPLEMENTADO, NAO TESTADO | rebuild_model | 14241 | - | P2 |
| R06 | fechar com controle de não-salvo | IMPLEMENTADO E TESTADO | close_document(save=bool) | 2411 | - | P2 |
| R07 | listar/alternar configuração | IMPLEMENTADO E TESTADO | list_configurations, switch_configuration | 14457, 13585 | - | P2 |
| R08 | criar/renomear configuração | PARCIAL | create_configuration, delete_configuration | 13549, 14481 | sem "renomear" explícito | P2 |
| R09 | suprimir feature por configuração | AUSENTE | - | - | delete_feature deleta, não suprime por config específica | P2 |
| R10 | exportar STEP/IGES/Parasolid/STL-3MF/PDF/DWG-DXF | IMPLEMENTADO E TESTADO | export_document | 4527 | cobertura ampla, único tool genérico | P2 |
| R11 | importar formatos | PARCIAL | open_document aceita qualquer caminho resolvido pelo SW | 2387 | não testado/documentado explicitamente para import STEP/IGES | P2 |
| R12 | validar arquivo exportado | IMPLEMENTADO E TESTADO | export_document verifica os.path.exists e size_bytes | 4588-4593 | - | P2 |
| R13 | tratar nome/caminho inválido | IMPLEMENTADO, NAO TESTADO | _resolve_write_path, FileNotFoundError em vários tools | 4540, 2248 | - | P2 |
| R14 | registrar erro import/export | IMPLEMENTADO E TESTADO | logging (log.warning/log.error) em export_document/open_document | 4573, 4577 | - | P2 |
| R15 | evitar sobrescrita acidental | AUSENTE | - | - | sem checagem de "arquivo já existe, confirmar" | P0 |

### CATEGORIA S — AUTOMAÇÃO/SEGURANÇA/CONFIABILIDADE (qualitativo)

| ID | Item | Status | Ferramenta(s) | server.py | Observação/Limitação | Prioridade |
|---|---|---|---|---|---|---|
| S01 | nomes/descrições claras | IMPLEMENTADO, NAO TESTADO | ~159 tools com nomes descritivos + docstrings extensas | - | - | P2 |
| S02 | parâmetros tipados com unidades | IMPLEMENTADO, NAO TESTADO | `unit: Optional[str]`, to_meters()/_unit_factor() disseminados | ex. 5572, 5621 | - | P2 |
| S03 | erros COM convertidos em mensagem clara | IMPLEMENTADO, NAO TESTADO | padrão except Exception as exc: raise RuntimeError(...) disseminado | ex. 11757, 12119 | - | P2 |
| S04 | sucesso/falha real relatado | IMPLEMENTADO, NAO TESTADO | padrão "measured, not echoed" (actual_depth, verified) | 5707-5718 | - | P2 |
| S05 | validação de argumento | IMPLEMENTADO, NAO TESTADO | ValueError disseminado | ex. 5584, 11684 | - | P2 |
| S06 | tratamento seleção inválida | IMPLEMENTADO, NAO TESTADO | RuntimeError "No face/entity found" | ex. 6328, 5484 | - | P2 |
| S07 | tratamento documento errado/inativo | IMPLEMENTADO, NAO TESTADO | _active_doc()/_active_drawing() + checagem _doc_type | 2246-2247, 11509-11510 | - | P2 |
| S08 | verificação de referência obsoleta pós-reconstrução | PARCIAL | get_persistent_reference/resolve_persistent_reference existem | 14374, 14417 | - | P0 |
| S09 | controle de estado do doc ativo | IMPLEMENTADO E TESTADO | get_document_info, list_open_documents, _active_doc | 2462, 2478 | - | P2 |
| S10 | operação destrutiva protegida | PARCIAL | ToolAnnotations(destructiveHint=...) existe mas não bloqueia execução | - | - | P0 |
| S11 | estratégia de desfazer | AUSENTE | - | - | sem tool "undo" | P0 |
| S12 | logs sem segredo | PARCIAL | logging usado; sem segredos no domínio | - | - | P1 |
| S13 | timeout/chamada travada | IMPLEMENTADO, NAO TESTADO | _timeout_budget_for + _run com asyncio.wait_for e stuck-call detection | 283, 341-386 | - | P2 |
| S14 | thread COM correta | IMPLEMENTADO, NAO TESTADO | CoInitialize em thread dedicada do executor | 300-301 | - | P2 |
| S15 | liberação de recurso COM | PARCIAL | _cleanup_com_thread chamado no shutdown | 318 | - | P1 |
| S16 | não reportar sucesso se SW falhou | IMPLEMENTADO, NAO TESTADO | verificação pós-chamada extensiva (actual_depth, density check, file exists) | 11563-11571, 4588-4593 | - | P2 |
| S17 | testes automatizados existem | IMPLEMENTADO, NAO TESTADO | tests/test_*.py | tests/test_bugfixes.py etc | - | P2 |
| S18 | testes de integração real | IMPLEMENTADO, NAO TESTADO | tests/run_*_live_test.py | tests/run_gear_live_test.py etc | - | P2 |
| S19 | documentação de instalação | IMPLEMENTADO, NAO TESTADO | README.md | README.md:27 | - | P2 |
| S20 | separação OK vs experimental | PARCIAL | README tem matriz OK/EXP, mas não é flag formal nas tools | - | - | P1 |
## D. COBERTURA POR CATEGORIA

Fórmulas (secção 25 do checklist):
- **Cobertura comprovada** = IMPLEMENTADO E TESTADO / total aplicável e verificável × 100
- **Cobertura potencial** = (IMPLEMENTADO E TESTADO + IMPLEMENTADO NÃO TESTADO) / total aplicável e verificável × 100
- Nesta auditoria, 0 itens foram classificados NÃO APLICÁVEL e 0 como NÃO VERIFICÁVEL — toda a análise foi estática (sem SolidWorks), mas suficiente para confirmar presença/ausência de código em 100% dos itens. "NÃO VERIFICÁVEL" aqui significaria apenas "não dá para saber nem pelo código", que não ocorreu.

| Categoria | Total | Testado | Não testado | Parcial | Experimental | Ausente | Comprovada | Potencial |
|---|---|---|---|---|---|---|---|---|
| A — ESBOÇOS E GEOMETRIA 2D | 28 | 8 | 2 | 3 | 1 | 14 | 29% | 36% |
| B — COTAS, RESTRIÇÕES, PARAMETRIZAÇÃO | 24 | 12 | 3 | 6 | 0 | 3 | 50% | 62% |
| C — SÓLIDOS/ADIÇÃO DE MATERIAL | 18 | 11 | 2 | 0 | 1 | 4 | 61% | 72% |
| D — CORTES/FUROS/REMOÇÃO | 25 | 14 | 0 | 5 | 2 | 4 | 56% | 56% |
| E — MODIFICAÇÃO DE SÓLIDOS | 23 | 6 | 1 | 2 | 5 | 9 | 26% | 30% |
| F — PADRÕES/ESPELHAMENTO | 13 | 6 | 0 | 3 | 1 | 3 | 46% | 46% |
| G — MATERIAIS FÍSICOS/PROPRIEDADES | 14 | 2 | 6 | 4 | 0 | 2 | 14% | 57% |
| H — APARÊNCIA/COR/TEXTURA/RENDER | 17 | 2 | 7 | 6 | 1 | 1 | 12% | 53% |
| I — MONTAGENS/RELAÇÕES MECÂNICAS | 23 | 8 | 4 | 4 | 4 | 3 | 35% | 52% |
| J — CHAPA METÁLICA | 16 | 8 | 0 | 3 | 0 | 5 | 50% | 50% |
| K — ESTRUTURAS SOLDADAS/PERFIS | 14 | 2 | 4 | 5 | 1 | 2 | 14% | 43% |
| L — SUPERFÍCIES/GEOMETRIA COMPLEXA | 17 | 0 | 0 | 0 | 0 | 17 | 0% | 0% |
| M — MOVIMENTO/CINEMÁTICA | 16 | 1 | 0 | 4 | 1 | 10 | 6% | 6% |
| N — SIMULAÇÃO/ANÁLISE | 24 | 0 | 0 | 0 | 0 | 24 | 0% | 0% |
| O — DESENHOS TÉCNICOS 2D | 20 | 6 | 7 | 6 | 0 | 1 | 30% | 65% |
| P — CAM/FABRICAÇÃO/IMPRESSÃO 3D | 11 | 1 | 0 | 1 | 0 | 9 | 9% | 9% |
| Q — INSPEÇÃO/MEDIÇÃO/ÁRVORE | 16 | 6 | 6 | 3 | 0 | 1 | 38% | 75% |
| R — ARQUIVOS/CONFIGURAÇÕES/EXPORTAÇÃO | 15 | 8 | 2 | 3 | 0 | 2 | 53% | 67% |
| S — AUTOMAÇÃO/SEGURANÇA/CONFIABILIDADE | 20 | 1 | 13 | 5 | 0 | 1 | 5% | 70% |
| **TOTAL GERAL** | **354** | **102** | **57** | **63** | **17** | **115** | **28.8%** | **44.9%** |
### CATEGORIA T — FLUXOS COMPLETOS DE PONTA A PONTA

Não submetida ao agente de exploração (que mapeou A–S); avaliada por composição manual das ferramentas já confirmadas acima. Nenhum destes fluxos foi executado — nem automaticamente (sem SolidWorks) nem manualmente — então "IMPLEMENTADO" aqui significa só "as ferramentas necessárias existem e parecem comporáveis", nunca "testado ponta a ponta".

| ID | Fluxo | Status | Ferramentas envolvidas | Observação |
|---|---|---|---|---|
| T01 | Bloco dimensionado + furos + chanfros + material + salvar | IMPLEMENTADO, NÃO TESTADO | draw_rectangle, extrude_sketch, hole_wizard, chamfer_edges, set_material, save_document | composição direta, sem bloqueio conhecido |
| T02 | Eixo por revolução + rasgos/roscas + medir + exportar STEP | IMPLEMENTADO, NÃO TESTADO | revolve_sketch, cut_extrude, add_cosmetic_thread/add_thread_feature, measure_body, export_document | rosca modelada depende de create_helix (EXP) |
| T03 | Flange com padrão circular de furos + verificar diâmetro/posição | IMPLEMENTADO, NÃO TESTADO | extrude_sketch, hole_wizard, circular_pattern, measure_body/list_faces | - |
| T04 | Peça oca: casca + nervuras + filetes | PARCIAL | shell_body, add_rib (EXP), fillet_edges | add_rib é experimental |
| T05 | Conjunto eixo+rolamentos+engrenagens+carcaça | PARCIAL | create_spur_gear (EXP), insert_component, add_mate (EXP) | sem biblioteca de rolamentos normalizados dedicada (usar geometria genérica) |
| T06 | Posicionar com mates + detectar interferência | PARCIAL | insert_component, add_mate (EXP), interference_check | add_mate é experimental |
| T07 | Mecanismo com movimento + verificar colisões | AUSENTE (parte física) | create_motion_study, mates | sem motor/física real nem colisão durante movimento (ver M03-M10) |
| T08 | Chapa dobrada + desdobrar + exportar DXF | IMPLEMENTADO, NÃO TESTADO | create_base_flange, flatten_sheet_metal, export_flat_pattern_dxf | - |
| T09 | Estrutura soldada com perfis + cut list | PARCIAL | create_weldment_profile, trim_extend_structural, insert_cut_list_table | cut list só existe dentro de um desenho, não como dado extraível diretamente de uma peça |
| T10 | Desenho técnico: vistas + cotas + exportar PDF | IMPLEMENTADO, NÃO TESTADO | create_new_drawing, insert_drawing_view, add_drawing_dimension, export_document | - |
| T11 | Material físico + aparência visual separadamente | IMPLEMENTADO, NÃO TESTADO | set_material, set_appearance | distinção já documentada no docstring de apply_metal_finish |
| T12 | Alterar dimensão anterior + verificar reconstrução de dependentes | IMPLEMENTADO, NÃO TESTADO | set_equation, rebuild_model, validate_model | - |
| T13 | Forçar entrada inválida + confirmar mensagem útil | IMPLEMENTADO (testado por unitário, não ao vivo) | validação ValueError disseminada | `tests/test_bugfixes.py::test_argument_validation_comes_before_the_first_com_call` cobre isso sem SolidWorks |
| T14 | Salvar, fechar, reabrir — validar persistência | IMPLEMENTADO, NÃO TESTADO | save_document, close_document, open_document | - |
| T15 | Criar modelo de especificação textual + verificar cada requisito geométrico | IMPLEMENTADO (via composição pela IA cliente), NÃO TESTADO | toda a modelagem + measure_body | depende inteiramente de orquestração correta pela IA chamadora, não de uma ferramenta única |
| T16 | Sequência de múltiplas ferramentas sem perder documento/seleção | PARCIAL/risco conhecido | _active_doc() global, sem sessão de documento explícita | risco já identificado na auditoria anterior (AUDIT_REPORT.md) — troca de documento ativo pelo usuário no meio de uma sequência pode aplicar operação no documento errado, sem aviso |

## E. LACUNAS PRIORITÁRIAS

### P0 — Risco de corrupção/perda de dados, segurança ou sucesso falso (7 itens)

| ID | Item | Status | Observação |
|---|---|---|---|
| R15 | Evitar sobrescrita acidental | AUSENTE | `save_document`/`export_document` com `filepath` sobrescrevem sem checar se o destino já existe e tem conteúdo diferente |
| S11 | Estratégia de desfazer | AUSENTE | nenhuma ferramenta de "undo" — a única recuperação é `delete_feature` manual |
| S10 | Operação destrutiva protegida | PARCIAL | `ToolAnnotations(destructiveHint=True)` é só metadado informativo para o cliente MCP; o servidor não bloqueia nem pede confirmação adicional |
| D24 | Detectar corte que não intersecta a peça | PARCIAL | erro genérico (`feat is None`), não uma mensagem específica que ajude a IA a diagnosticar |
| D25 | Validar volume antes/depois do corte | PARCIAL | possível manualmente (`measure_body` antes e depois), mas não é automático dentro de `cut_extrude` |
| B24 | Informar falha de solver (conflito de restrições) | PARCIAL | erro genérico do `AddDimensionValue2`/relação, sem distinguir "conflito de restrições" de outras falhas |
| S08 | Verificação de referência obsoleta pós-reconstrução | PARCIAL | `get_persistent_reference`/`resolve_persistent_reference` existem mas não são usados internamente por outras ferramentas para auto-detectar referência inválida |

**Nenhum destes é "a geometria sai errada silenciosamente"** — o padrão geral do projeto (confirmado em `S04`, `S16`) já é medir e comparar antes de reportar sucesso. O risco real aqui é operacional: perda de arquivo por sobrescrita, impossibilidade de desfazer, e mensagens de erro que não ajudam a IA chamadora a se recuperar.

### P1 — Fundamentos que bloqueiam muitos fluxos (43 itens)

Concentrados em: geometria de esboço 2D básica faltante (ponto, elipse, offset/trim/extend/split/convert-entities, filete/chanfro de esboço, texto em esboço — Categoria A), detecção de estado do esboço (sub/sobre-definido — Categoria B), alguns fundamentos de modificação de sólidos (draft, thicken, ponto/sistema de coordenadas de referência, excluir/substituir face — Categoria E) e consulta de árvore (comparar geometria antes/depois, detalhe de feature individual — Categoria Q). Lista completa de IDs: A03, A07, A08, A11, A12, A15, A17–A26, A28, B02, B03, B15–B19, B23, E03, E04, E06, E08, E09, E14, E16, E17, E20–E22, Q04, Q05, Q13, Q15, S12, S15, S20.

### P2 — Capacidades importantes (56 itens)

Lacunas pontuais dentro de categorias já majoritariamente cobertas: extrusão/corte até face (C03, D04), corte por sweep/loft (D07, D08), padrão em 2 direções/dirigido por curva-esboço (F02, F07, F08), leitura/escrita avançada de material (G01, G03–G05, G12), montagem (criar peça in-context, substituir componente, detectar subdefinição — I05, I14, I15), chapa metálica avançada (flange de contorno, hem, alívio de canto, fator K — J05, J07–J10), lista de corte de weldment fora do desenho (K10–K14), desenho técnico (tamanho de folha, tolerância dimensional, carimbo — O02, O09, O16), e configuração/import (suprimir por config, import formatos — R09, R11). Lista completa de IDs no arquivo (seção C) sob prioridade P2.

### P3 — Avançadas/especializadas, fora do escopo atual do projeto (72 itens)

- **Categoria L (Superfícies) — 17 itens, 0% implementado.** Nenhuma chamada de API de superfície (`InsertSurface*`, `Knit`, `ThickenSurface`) existe. Tecnicamente possível com a API COM padrão (não exige add-in), mas é uma frente de trabalho nova inteira.
- **Categoria M (Motion) — 16 itens, só a casca.** Existe criar/listar estudo e usar mates como vínculo; **não existe** motor, gravidade, mola, colisão durante movimento, nem leitura de resultado — a parte "física" do Motion não está implementada.
- **Categoria N (Simulação/FEA) — 24 itens, 0% implementado.** Exige a API separada do SolidWorks Simulation (licença/add-in adicional) — já documentado como fora de escopo no próprio `.claude/CLAUDE.md` do projeto.
- **Categoria P (CAM) — 11 itens, praticamente 0%.** Exige SolidWorks CAM/CAMWorks (add-in licenciado separado) — nunca referenciado no código.
- **Categoria H (Aparência avançada) — itens de render/cena/iluminação/câmera (H15, H16).** PhotoView360/render fotorrealista não é a mesma coisa que `capture_viewport` (screenshot do viewport atual).

## F. TESTES REALIZADOS

**Nenhum teste ao vivo contra SolidWorks real foi executado nesta auditoria** — este ambiente não tem a aplicação instalada (confirmado: `tests/conftest.py` precisa falsear `win32com`/`pythoncom` para a suíte nem importar).

O único "teste executado" desta rodada foi:

```
ambiente: container Linux sem SolidWorks
comando: uv run pytest tests/ -q -k "not run_" --ignore-glob="tests/run_*"
resultado: 1358 passed, 7 deselected (os tests/run_*_live_test.py, que exigem SolidWorks real)
```

Isto confirma que a suíte unitária/contrato existente continua passando — **não confirma nenhuma das 159 ferramentas contra o SolidWorks real**. Os 102 itens classificados "IMPLEMENTADO E TESTADO" nesta auditoria herdam esse status do `README.md`, que registra testes ao vivo feitos em sessões anteriores (com data e, em vários casos, o script `tests/run_*_live_test.py` usado) — não foram re-confirmados agora.

## G. PLANO DE IMPLEMENTAÇÃO INCREMENTAL

Dado o tamanho (115 itens AUSENTE + 63 PARCIAL = 178 lacunas candidatas), **não vou implementar tudo de uma vez** — o próprio checklist pede isso ("uma alteração por tarefa... evitar refatoração massiva") e o volume de ferramentas COM novas sem acesso a SolidWorks real para verificar cada uma é um risco real de qualidade (como já aconteceu e foi corrigido no `BUGS_AND_FIXES.md` BUG-03). Proposta de sequência, menor risco primeiro:

**Etapa 1 — P0, correções pequenas e seguras (sem SolidWorks necessário):**
- R15 (sobrescrita acidental): `save_document`/`export_document` passam a aceitar `overwrite: bool = True` e, quando `False`, falham se o destino já existe. Teste unitário com `tmp_path`.
- S10/D24/D25/B24 (mensagens de erro): melhorar o texto de erro nos pontos já identificados (arquivo:linha na matriz) para nomear a causa provável, sem mudar o fluxo de sucesso. Testes unitários com mocks (padrão já usado em `tests/test_bugfixes.py`).
- **Critério de aceitação:** suíte completa continua verde; nenhum contrato de retorno muda para quem já usa a ferramenta corretamente.

**Etapa 2 — P1 fundamentos de esboço 2D (Categoria A/B), um item por vez:**
- `draw_ellipse`, ponto de esboço dedicado, `draw_rectangle` com variante centralizada, `get_sketch_status` reportando sub/sobre-definição (via `ISketch::GetSketchRelationsCount`/status DOF, a confirmar a API exata antes de implementar).
- Cada ferramenta nova: implementação + teste unitário de validação de argumento + entrada no `TOOL_INVENTORY.csv`/README como "não documentado/não testado ao vivo" (nunca "OK" sem verificação real) + item correspondente em `LIVE_TESTS_REQUIRED.md`.
- **Critério de aceitação:** mesmo padrão das ferramentas existentes (anotação MCP correta, erro antes de qualquer chamada COM, medição/verificação pós-operação quando aplicável).

**Etapa 3 — P1 restante (Categoria E/Q) e P2 de maior valor (a definir com o usuário):**
- Depende de qual frente o usuário mais usa (montagem? chapa metálica? desenho técnico?) — proponho que você **escolha a ordem** em vez de eu decidir sozinho, dado o volume.

**Etapas 4+ — P3 (Superfícies/Motion/FEA/CAM):**
- Não recomendo começar sem confirmação explícita: são frentes de trabalho GRANDES (uma categoria inteira cada), algumas (FEA, CAM) exigem licenças/add-ins que este projeto nunca usou e que eu não posso confirmar que o usuário possui.

**Eu não vou implementar nada disto ainda — aguardando sua confirmação de prioridade, exatamente como o checklist pede ("Não comece a modificar o código... até obter autorização explícita do usuário").**

## H. CONCLUSÃO

1. **O que a IA já consegue fazer com evidência (testado ao vivo antes desta sessão):** modelagem sólida básica completa (esboço simples, extrusão/corte/revolução/sweep/loft, filete/chanfro/shell, padrões lineares/circulares, furos via Hole Wizard, montagem com inserção/mates básicos/interferência, chapa metálica básica, medição/propriedades, exportação ampla). 102 dos 354 itens auditados.
2. **O que existe no código mas não foi validado ao vivo:** equações/parâmetros, grande parte da leitura de propriedades/material, boa parte do desenho técnico 2D, toda a seção "S" (automação/confiabilidade — timeout, validação, logging). 57 itens.
3. **O que está parcial/experimental:** `add_mate`, `combine_bodies`, `split_body`, `create_reference_axis`, `add_rib`, roscas (cosmética/modelada), vista explodida, padrão de componentes — todas marcadas EXP no próprio README por motivo documentado. 17 itens EXP + 63 PARCIAL.
4. **O que está ausente:** geometria de esboço avançada (elipse, offset/trim/extend, texto), draft/thicken, sistema de coordenadas, superfícies (categoria inteira), física de movimento, FEA, CAM. 115 itens.
5. **Cinco próximas ações de maior valor (minha recomendação, sujeita à sua prioridade):**
   1. R15 — proteção contra sobrescrita acidental (P0, risco real de perda de arquivo, correção pequena).
   2. S10/D24/D25/B24 — mensagens de erro específicas em vez de genéricas (P0, melhora diagnóstico sem mudar comportamento).
   3. Elipse + ponto de esboço dedicado (A12/A03) — dois primitivos de esboço básicos faltantes, alta frequência de uso provável.
   4. Detecção de esboço sub/sobre-definido (B16–B18) — fundamento que falta para qualquer fluxo que dependa de "o esboço está pronto para extrudar?".
   5. Decidir com você: a próxima frente grande (Superfícies, Motion físico, ou nenhuma — ficar com modelagem sólida/montagem/chapa/desenho, que já é a maior parte do volume de uso real segundo os relatórios de projeto existentes no repositório).

Nenhuma destas ações foi implementada ainda. Aguardando sua decisão de prioridade antes de tocar no código.
