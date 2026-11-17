# SolidWorks MCP Server

Servidor MCP em Python que controla o SolidWorks via COM (`win32com`), escrito
com o SDK oficial (`mcp`, usando `FastMCP`). **167 ferramentas incondicionais
+ `execute_python` condicional** (v5.22.0) -- ver `docs/audits/TOOL_INVENTORY.csv` para a
lista completa e `docs/audits/AUDITORIA_COBERTURA_FUNCIONAL.md` para o que cada categoria
do SolidWorks tem ou não cobertura.

## Auditorias e relatórios deste repositório

- [`docs/audits/AUDIT_REPORT.md`](docs/audits/AUDIT_REPORT.md) -- auditoria de arquitetura/segurança
  (timeout do executor COM, política de caminhos, anotações MCP), achados
  classificados confirmado/refutado/pendente.
- [`docs/audits/AUDITORIA_COBERTURA_FUNCIONAL.md`](docs/audits/AUDITORIA_COBERTURA_FUNCIONAL.md) --
  cobertura funcional contra 354 capacidades do SolidWorks (esboço, sólidos,
  montagem, chapa, estruturas, superfícies, movimento, FEA, CAM, desenho
  técnico...): o que está testado, implementado-não-testado, parcial,
  experimental ou ausente, por categoria, com prioridade de lacunas.
- [`docs/audits/BUGS_AND_FIXES.md`](docs/audits/BUGS_AND_FIXES.md) -- bugs confirmados e corrigidos,
  com reprodução, causa e teste de regressão.
- [`docs/audits/LIVE_TESTS_REQUIRED.md`](docs/audits/LIVE_TESTS_REQUIRED.md) -- passos exatos para
  um operador com SolidWorks real confirmar o que a auditoria não pôde
  (nenhum destes ambientes de auditoria tinha SolidWorks instalado).
- [`docs/audits/TOOL_INVENTORY.csv`](docs/audits/TOOL_INVENTORY.csv) -- inventário mecânico das 168
  ferramentas (categoria, schema, tipo leitura/escrita/destrutiva, status
  OK/EXP/não documentado, referências de teste).

## Para quem so quer usar

Instale o `.mcpb` com dois cliques. O passo a passo completo, sem jargao, esta
em [COMO_USAR.md](COMO_USAR.md).

Requisitos: Windows, SolidWorks 2022+ instalado e licenciado, e Claude Desktop.

## Para quem vai desenvolver

### Política de ferramentas

O catálogo MCP mantém somente operações CAD reutilizáveis e geradores
paramétricos de categorias de componentes. Receitas de produtos completos —
por exemplo, um pistão automotivo completo, um ventilador ou um redutor
específico — não são ferramentas públicas: elas devem viver em scripts de
exemplo/workflows que combinem as operações do catálogo. Assim, novos projetos
reutilizam as mesmas ferramentas mudando parâmetros, sem transformar cada
modelo em uma nova entrada permanente do MCP.

```bash
pip install -r requirements.txt
python -m pytest
```

Configuracao manual em `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:\\caminho\\para\\mcp-solidworks\\server.py"]
    }
  }
}
```

## Gerar o pacote distribuivel

O `manifest.json` declara `server.type: "uv"`, entao o Claude Desktop resolve
Python e dependencias sozinho a partir do `pyproject.toml` -- necessario porque
`pywin32` e `pydantic` sao extensoes compiladas e nao podem ser empacotadas de
forma portatil.

```bash
npx @anthropic-ai/mcpb pack . solidworks-mcp-5.22.0.mcpb
```

O nome do pacote e **escopado**: `npx mcpb pack` falha com 404 (`mcpb` nao
existe no registro npm), o que este README mandava fazer ate a v5.18.0.
O `pack` valida o `manifest.json` antes de empacotar e respeita o
`.mcpbignore` -- confira na listagem que `gear_geometry.py`, `alfa_drawing.py` e
os onze arquivos de `.claude/knowledge/` entraram: o `server.py` importa os dois
primeiros e expoe os outros como resources, entao um pacote sem eles instala e
quebra em uso.

Abra o SolidWorks (opcional -- o servidor consegue abrir sozinho) e peca para o
Claude "conectar ao SolidWorks".

## Seguranca para uso publico

Este servidor foi projetado para automacao local confiavel. Ele controla uma
sessao real do SolidWorks pelo COM do Windows, entao ferramentas como salvar,
fechar, suprimir componentes e alterar geometrias modificam documentos abertos.

A ferramenta `execute_python` fica desativada por padrao porque executa Python
com acesso aos objetos COM `sw` e `doc`. Para usar somente em depuracao local
confiavel, defina explicitamente:

```powershell
$env:SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON = "1"
```

Nao publique modelos CAD privados, pacotes `.mcpb`, logs ou resultados de teste
gerados localmente. O `.gitignore` ja exclui esses artefatos por padrao.

## Status de verificacao (testado ao vivo no SolidWorks 2025, PT-BR)

A matriz abaixo registra o estado validado no SolidWorks 2025 PT-BR. Legenda:
- OK  = criou o recurso com sucesso no teste ao vivo.
- EXP = experimental: a assinatura COM esta correta mas o recurso depende de
        selecao/estado especifico ou de uma parte da API que se comporta de
        forma inconsistente nesta versao; pode exigir ajuste manual.

### Conexao / Documentos / Utilidades -- OK
`connect_solidworks`, `get_solidworks_info`, `create_new_part`,
`create_new_assembly`, `create_new_drawing`, `open_document`, `close_document`,
`save_document`, `get_document_info`, `list_open_documents`, `set_units`,
`set_view`, `zoom_to_fit`, `zoom_to_area`, `get_view_state`

`execute_python` e uma ferramenta de depuracao privilegiada. Ela existe no
catalogo, mas permanece bloqueada ate
`SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1` ser definido.

> Correcao importante: os templates padrao sao resolvidos pelos indices
> corretos (`swDefaultTemplatePart/Assembly/Drawing` = 8/9/10). Antes, o desenho
> era criado silenciosamente como uma peca.

### Esbocos e desenho 2D -- OK
`create_sketch`, `create_sketch_on_face`, `close_sketch`, `get_sketch_status`,
`create_3d_sketch`, `draw_line`, `draw_line_3d`, `draw_circle`, `draw_rectangle`,
`draw_arc`, `draw_polygon`, `add_sketch_dimension`, `add_sketch_relation`

`draw_profile` (v5.18.0) e o caminho para qualquer **curva calculada** --
involuto, lei de came, dente de polia HTD, trocoide, aerofolio: desenha a
cadeia inteira com o motor de inferencia **desligado** (`SetAddToDB`) numa
unica chamada e **confere a posicao de cada vertice** lida de volta do
SolidWorks. Marcada EXP pelo mesmo motivo que `create_spur_gear` (o caminho
COM nao foi rodado nesta instalacao); a diferenca e que agora uma geometria
deslocada aparece em `displaced_points` em vez de virar peca errada.

> Correcao de premissa (v5.18.0): o risco do snap de esboco **nao** e "peca
> grande, feature pequena". O motor de inferencia junta um ponto novo ao ponto
> **vizinho que ja existe** quando os dois estao a poucos pixels -- logo o que
> decide e o **espacamento entre pontos consecutivos do proprio perfil**. Um
> flanco involuto amostrado a 0,3 mm colapsa numa engrenagem de 44 mm, onde
> nao ha peca grande nenhuma. Ver `.claude/knowledge/verificacao_e_qa.md`.

### Features 3D -- OK
`extrude_sketch`, `cut_extrude`, `revolve_sketch`, `sweep_sketch`,
`loft_sketches`, `fillet_edges`, `chamfer_edges`, `shell_body`,
`linear_pattern`, `circular_pattern`, `hole_wizard`, `list_features`,
`delete_feature`

`cut_part_end` (v5.21.0, medido ao vivo): corte de extremidade / mitra. Fatia uma
peca de tubo/perfil com um plano (`normal` + `offset` ou `point`, em coordenadas
da peca; `origin` permite dar o plano em coordenadas da montagem) e remove o lado
para onde a normal aponta, deixando a ponta como UMA face plana inclinada que
assenta face a face contra outro membro (banzo x perna, corrimao x guarda-corpo).
Corta com um retangulo gigante no plano global que contem a normal, extrudado
simetrico por toda a peca. A resposta traz volume antes/depois, caixa antes/depois,
`removed_mm3`, `end_on_plane` (ponto mais distante ao longo da normal lido do
solido) e `cut_face_area_mm2`. Limitacao: a normal tem de ser perpendicular a um
eixo global (sem angulo composto); para mitrar uma ponta rente a uma parede o
tubo bruto precisa de sobra de comprimento antes do corte.

Adicionada em 17/08/2026: ate esta versao nao havia forma de apagar uma
feature de peca por nome (so existia `delete_component` para assembly).
`delete_feature` seleciona por `SelectByID2(nome, "BODYFEATURE", ...)` e
chama `DeleteSelection2`; por padrao tambem remove features dependentes
(ex.: um weld member construido sobre um esboco 3D) para nao esbarrar no
dialogo de confirmacao do SolidWorks, que travaria a chamada COM.

### Operacoes de corpo / referencia
`mirror_feature` (OK), `mirror_body` (OK), `move_copy_body` (OK),
`create_reference_plane` (OK), `set_appearance` (OK),
`set_material` (OK -- retestado ao vivo em 13/10/2026 com `tests/run_measure_body_live_test.py`:
`set_material("AISI 1020")` aplicou densidade 7900 kg/m3, e um bloco de
100x50x20 mm mediu massa/volume/densidade com 0,0000% de erro contra o
calculo analitico e contra `lookup_material_properties`. O travamento em
1000 kg/m3 (agua) relatado em 05/10/2026 nao se repetiu nesta reinstalacao
e a causa raiz daquela falha continua sem explicacao -- pode ter sido
estado da instalacao/licenca, nao o codigo. `measure_body` continua
devolvendo `density_kg_m3` e avisando se a massa aparecer em densidade de
agua, que agora e um sinal de regressao a observar, nao o estado esperado;
`lookup_material_properties` segue como a fonte independente pra conferir
density contra),
`create_reference_axis` (EXP -- InsertAxis2 depende de selecao valida),
`combine_bodies` (EXP -- corpos precisam se tocar/interseccionar),
`split_body` (EXP -- fluxo Pre/PostSplitBody), `add_rib` (EXP)

### Engrenagens
`create_spur_gear` (EXP -- ver a ressalva abaixo)

A geometria do dente e **testada**: `gear_geometry.py` e Python puro e
`tests/test_gear_geometry.py` tem 113 testes que conferem o involuto contra a
sua propria definicao, a espessura pi*m/2 na circunferencia primitiva (a
condicao de engrenamento), a tangencia do raio de pe, o fechamento do contorno
e a prova de que ele nao pode se autointersectar. O que **nao** foi rodado
nesta instalacao e o caminho COM: `SetAddToDB(True)` + umas 500 chamadas de
`CreateLine2` num unico esboco. Por isso EXP, e nao OK.

A propria ferramenta mede o resultado: compara o volume real do solido com o
volume analitico do contorno e devolve `verified`, mais um `teeth_present` que
existe so pra pegar o disco liso. Ou seja, se o caminho COM falhar nesta
instalacao, a falha vem reportada em vez de silenciosa -- que era exatamente o
problema. Para confirmar ao vivo: `python tests/run_gear_live_test.py`.

### Montagem
`insert_component` (OK -- posicao conferida: devolve `actual_position` lido do
SolidWorks, `deviation` e `verified`), `verify_assembly_positions` (OK),
`capture_assembly_pose` (OK), `restore_assembly_pose` (OK),
`list_components` (OK), `fix_component`,
`float_component`, `delete_component`, `suppress_component`,
`unsuppress_component`, `list_mates`, `interference_check` (OK),
`add_mate` (EXP -- devolve a posicao dos dois componentes antes/depois, os
graus de liberdade que a mate NAO trava, e `geometry_check` com o angulo
medido entre as duas faces; `align` deixou de forcar `swMateAlignALIGNED`),
`add_advanced_mate` (EXP), `add_cam_follower_mate` (OK),
`add_screw_mate` (OK), `add_rack_pinion_mate` (OK),
`list_motion_studies` (OK), `create_motion_study` (OK),
`create_assembly_pattern` (EXP -- precisa de referencia de direcao),
`create_exploded_view` (EXP), `extract_assembly_data` (OK)

Adicionada em 02/10/2026 para fluxos de extracao tipo BOM (ex.: pipelines de IA
que precisam de quantidade, material, peso e dimensoes por peca): sem ela, um
cliente precisa de `list_components` mais um `get_custom_properties` e um
`measure_body` **por componente**. `extract_assembly_data` percorre os
componentes de topo da montagem ativa, abre o `IModelDoc2` de cada um
(`IComponent2.GetModelDoc2` -- chega como metodo ligado sob dynamic IDispatch
e ja resolvido sob a typelib gerada; o valor pre-chamada e usado como
fallback se a chamada em si falhar com membro-nao-encontrado) e le
propriedades/massa no mesmo nivel padrao de `get_custom_properties`
(config vazio = nivel do documento, nao a configuracao do componente na
montagem), numa unica chamada. Tambem devolve `bom`: os mesmos dados
agrupados por arquivo de origem com `quantity` computada, pronto para um
backend montar a BOM/lista de corte de verdade. Falha por componente (sem
material, suprimido, sem corpo) fica no `errors` daquele item em vez de
derrubar a chamada inteira -- a logica interna de leitura de propriedades e
medicao e a mesma de `get_custom_properties`/`measure_body` (extraida para
`_read_custom_properties`/`_measure_model_doc` para as duas ferramentas nao
divergirem com o tempo).

### Propriedades / Medicao / Exportacao / Configuracoes -- OK
`get_custom_properties`, `set_custom_property`, `measure_body`,
`export_document`, `create_configuration`,
`switch_configuration` (resolve nomes localizados, ex. "Valor predeterminado")

### Leitura de estado (tela, arvore, montagem) -- OK
Adicionado e validado ao vivo em 16/08/2026: `get_view_state` reporta o zoom
(`Scale2`), o codigo bruto de modo de exibicao, a matriz de rotacao 3x3 da
camera e -- quando ela bate exatamente com uma das 9 vistas nomeadas de
`set_view` -- esse nome em `closest_named_view` (`None` quando o usuario girou
o modelo livremente; testado ao vivo com uma rotacao manual para confirmar que
nao ha falso-positivo). `list_features` passou a reportar `suppressed` e
`error_code` por recurso, e `list_components` passou a reportar `visible`
(independente de `suppressed`) via `IComponent2.GetVisibility`, confirmado por
um teste de ida-e-volta ocultar/exibir. Nenhum destes numeros foi assumido: os
valores de referencia das 9 orientacoes nomeadas e o mapeamento 0/1 de
visibilidade foram capturados ao vivo antes de escrever o codigo.

### Macros VBA -- OK
Adicionado em 16/08/2026 apos comparar este servidor com outros MCPs de
SolidWorks publicos (todos expoem execucao de macro; este nao expunha).
`list_macro_methods` inspeciona um `.swp`/`.swb`/`.dll` e lista os pontos de
entrada **sem executar nada**, separando os procedimentos sem argumentos (os
unicos que `RunMacro2` consegue chamar) dos que exigem parametros.
`run_macro` executa um procedimento; com `module`/`procedure` vazios ele
resolve sozinho quando existe exatamente um ponto de entrada, e caso contrario
lista as opcoes em vez de adivinhar.

Os filtros do `swMacroMethods_e` foram descobertos ao vivo, nao deduzidos dos
nomes do enum: `1` = sem argumentos, `2` = com argumentos, e `0` -- que parece
significar "todos" -- retorna `None`.

> **Seguranca:** `run_macro` executa o VBA que estiver dentro do arquivo, com
> os privilegios da sessao do SolidWorks, sem sandbox. Use apenas macros
> confiaveis, e prefira `list_macro_methods` antes para ver o conteudo.

### Perfis estruturais (weldments) -- OK, exceto add_end_cap
Validado ao vivo (SolidWorks 2025) com um membro em L + um membro cruzando-o,
ambos em tubo quadrado ISO: `create_3d_sketch`, `draw_line_3d`,
`create_weldment_profile`, `add_gusset`, `trim_extend_structural`. O caminho da
biblioteca de perfis e resolvido em
`C:\ProgramData\SOLIDWORKS\...\weldment profiles\<padrao>\<tipo>.sldlfp`
(o tamanho e uma *configuracao* dentro do arquivo, descoberta automaticamente
por `create_weldment_profile` a partir do nome pedido).

`add_gusset` funciona de forma confiavel no caso classico (uma face de viga +
uma face de chapa/outra viga que se encontram numa aresta real); um canto
chanfrado (miter) de uma unica peca em L e um caso mais ambiguo e pode
precisar de um par de faces escolhido com mais cuidado.

`add_end_cap` (EXP -- **defeito confirmado do SolidWorks 2025**): apos corrigir
um bug real do lado do MCP (a face certa era identificada mas depois
descartada e re-selecionada por ray-casting a partir do ponto original, que
cai exatamente na borda do vazio interno de um perfil oco -- agora a face
identificada e selecionada diretamente), `InsertEndCapFeature3` ainda retorna
`None` em toda uma matriz de tentativas: os tres valores do enum de direcao,
com/sem tratamento de canto, com uma ou duas faces selecionadas, com o
documento salvo, e ate com os valores exatos do exemplo oficial da API da
SolidWorks copiados literalmente. Mesmo padrao ja documentado abaixo para
`create_helix` (`InsertHelix`).

### Chapa metalica -- OK
Validado ao vivo (SolidWorks 2025): `create_base_flange` (o parametro PCBA
exige um VARIANT de dispatch, nao bool -- corrigido), `add_sheet_metal_bend`
(usa Insert Bends num corpo previamente casqueado com `shell_body`),
`add_sheet_metal_edge_flange` (requer objetos de aresta, resolvidos a partir
de um ponto conhecido na aresta), `flatten_sheet_metal` (alterna
flat/folded).

### Roscas / usinagem -- parcial
`add_cosmetic_thread` (EXP), `add_thread_feature` (delega para rosca cosmetica --
roscas 3D reais NAO sao expostas pela API do SolidWorks),
`create_helix` (EXP -- `InsertHelix` retorna None via COM nesta versao),
`create_knurl` (EXP -- Wrap+Deboss; desde a v5.18.0 desenha as celulas com a
inferencia desligada e **confere cada vertice**: a celula tem 0,2 mm de largura
com vertices a 0,1 mm, tres vezes mais apertado que o dente de engrenagem que
colapsava, e a unica checagem era "o Wrap nao voltou None" -- uma celula
achatada gravava nada e reportava sucesso. A PROFUNDIDADE gravada continua sem
medicao: o Wrap nao expoe valor pra ler de volta, e o retorno diz isso).

### Desenho tecnico detalhado -- parcial
`add_drawing_annotation` (OK), `insert_drawing_view` (OK -- validado ao vivo
inserindo uma vista isometrica de uma peca de weldment; a nota anterior sobre
`CreateDrawViewFromModelView3` retornar `None` estava desatualizada),
`add_weld_symbol` (OK -- validado ao vivo colocando um simbolo de solda de
filete numa aresta da vista inserida). As demais sao EXP e dependem do
gerenciador de propriedades ou de uma vista de modelo com estado especifico:
`insert_section_view`, `insert_detail_view`, `insert_broken_view`,
`insert_auxiliary_view`, `add_drawing_dimension`, `add_centerline`,
`add_surface_finish`, `add_gdt_symbol`, `add_balloon`, `insert_bom_table`,
`insert_cut_list_table`.

### Cobertura desta matriz (auditoria de 15/10/2026, atualizada)
Este servidor registra 167 ferramentas incondicionais + `execute_python`
(condicional). A matriz OK/EXP acima -- a mesma lida ao vivo pelo resource
`solidworks://tool-status` -- cobre 137 delas. As ferramentas abaixo não
têm status OK/EXP registrado aqui (não é o mesmo que "falhou": a maioria
(`list_*`, `get_*`, `delete_*`, `set_*` simples) tem alta chance de funcionar
pelo mesmo padrão COM de ferramentas vizinhas já marcadas OK, mas isso não foi
testado ao vivo e não deve ser assumido pela IA que lê este resource):
`extract_assembly_bom`, `get_component_transform`, `set_component_transform`,
`capture_standard_views`, `list_sketch_entities`, `draw_centerline`,
`draw_spline`, `create_gear`, `get_drawing_layout`, `get_view_entities`,
`get_view_dimensions`, `dimension_by_entity_ids`, `verify_drawing`,
`delete_mate`, `get_appearance_properties`, `set_appearance_properties`,
`apply_texture`, `remove_texture`, `apply_metal_finish`,
`list_p2m_appearances`, `apply_p2m_appearance`, `capture_viewport`,
`get_selection`, `list_faces`, `rebuild_model`, `validate_model`,
`get_document_dependencies`, `get_persistent_reference`,
`resolve_persistent_reference`, `list_configurations`,
`delete_configuration`, `list_equations`, `add_equation`, `set_equation`,
`delete_equation`, `export_flat_pattern_dxf`, `pack_and_go`,
`list_display_states`, `create_display_state`, `apply_display_state`,
`rename_display_state`, `delete_display_state`, `create_profile_part`,
`create_tube_part`, `add_mate_by_name`, `interference_report`,
`set_view_direction`, `get_default_templates`, `set_default_templates`,
`create_document_from_template`, `save_as_template`, `run_command_by_id`,
`set_section_view`, `clear_section_view` (as últimas 7, adicionadas em
17/10/2026 -- ver seção abaixo).

### Templates, System Options e vista em corte 3D (17/10/2026, não verificado ao vivo)
Sete ferramentas novas:
- `get_default_templates`/`set_default_templates` -- leem/gravam os System
  Options "Default Templates" (part/assembly/drawing) via
  `GetUserPreferenceStringValue`/`SetUserPreferenceStringValue` nos índices
  8/9/10 -- os mesmos índices que `_find_template` já usa ao vivo (confirmado)
  para `create_new_part`/`create_new_assembly`/`create_new_drawing`.
- `create_document_from_template` -- cria um documento a partir de um arquivo
  de template específico (não o default configurado), via `NewDocument` com o
  mesmo formato de chamada já usado por `create_new_part` etc.
- `save_as_template` -- salva o documento ativo como `.prtdot`/`.asmdot`/
  `.drwdot`, validando que a extensão bate com o tipo do documento antes de
  chamar `SaveAs` (reaproveita o mesmo mecanismo de `save_document`).
- `run_command_by_id` -- escape hatch genérico para `ISldWorks::RunCommand`
  (executa qualquer comando de UI pelo ID numérico `swCommands_e`). Marcado
  `openWorldHint=True`: o efeito depende inteiramente do ID passado.
- `set_section_view`/`clear_section_view` -- mostram/escondem o modelo
  "cortado" por um plano **só na vista 3D** (não remove material de verdade,
  ao contrário de `cut_extrude`). **O nome exato do método COM para isto não
  está documentado neste repositório nem foi confirmado ao vivo** -- a
  ferramenta tenta uma lista de nomes plausíveis (`SectionViewOn3`/`2`/sem
  sufixo) e o retorno (`applied_via_method`) diz exatamente qual funcionou,
  ou `null` se nenhum funcionou (nesse caso, usar `run_command_by_id` com o
  ID correto do comando "Section View" nesta versão do SolidWorks).

**Nenhuma destas 7 ferramentas foi testada contra um SolidWorks real** -- ver
`docs/audits/LIVE_TESTS_REQUIRED.md`, item LT-10.

### Desenho técnico 2D: escala de folha, cota automática e carimbo (17/10/2026, não verificado ao vivo)
Fechando lacunas do `docs/audits/AUDITORIA_COBERTURA_FUNCIONAL.md` categoria O:

- `set_sheet_scale` -- grava a escala de uma folha (`ISheet::SetScale`). A
  leitura (`get_drawing_layout`) já existia e é confirmada ao vivo; o
  caminho de escrita é novo e não verificado.
- `insert_model_items` -- equivalente a "Inserir > Itens do Modelo" do
  SolidWorks: traz cotas/anotações já definidas no modelo 3D para a vista de
  desenho, em vez de cotar à mão com `add_drawing_dimension`. Os valores de
  bit de `_MODEL_ITEMS_FLAGS` (quais tipos de anotação cada flag liga) **não
  estão documentados neste repositório nem foram confirmados ao vivo**.
- **Carimbo (title block):** não foi criada uma ferramenta nova -- o carimbo
  padrão do SolidWorks é texto vinculado a propriedades customizadas (ex.
  `$PRPSHEET:"Title"`), e `set_custom_property`/`get_custom_properties` já
  aceitam um parâmetro `config`; para uma folha de desenho, passar o NOME DA
  FOLHA nesse parâmetro (ex. `set_custom_property("Title", "Suporte X",
  config="Folha1")`) grava a propriedade no escopo da folha, que é o que o
  carimbo costuma exibir. Isso não foi confirmado ao vivo especificamente
  para desenhos.
- **Tolerância dimensional (± numa cota já existente) -- avaliada e NÃO
  implementada.** Precisaria de três etapas encadeadas e nenhuma delas tem
  precedente confirmado neste código: selecionar uma cota já existente por
  ponto (nenhuma ferramenta aqui faz isso -- `add_drawing_dimension` só
  cria cotas novas), obter o `IDisplayDimension` dela, e então chamar um
  método de tolerância cujo nome não é citado em nenhum lugar deste
  repositório. Encadear três suposições não confirmadas é diferente de
  arriscar uma só (como em `set_section_view`) -- preferi não implementar
  isso a entregar uma ferramenta com alta chance de falhar nas três etapas.
  Ver `docs/audits/LIVE_TESTS_REQUIRED.md` LT-10 para como descobrir a API real via
  Macro Recorder, se isso for prioridade depois.

**Nenhuma destas ferramentas foi testada contra um SolidWorks real.**

### `apply_texture`: mapeamento U/V real + relevo/blend best-effort (15/10/2026, não verificado ao vivo)
`apply_texture` chamava `extension.CreateTexture(path, scale, angle,
bool(blend_with_color))`. A assinatura documentada de
`IModelDocExtension::CreateTexture` é `(FileName, Width, Height, Angle)` --
ou seja, o `angle` do chamador ia para o slot de `Height` e um booleano
0.0/1.0 ia para o slot de `Angle`, então a rotação pedida nunca era
realmente enviada. Corrigido, e a correção abriu espaço para expor
`u_scale`/`v_scale` (mapeamento U/V independente por eixo) em vez de um
único `scale` para os dois eixos -- `scale` continua funcionando (aplica o
mesmo valor aos dois eixos, comportamento idêntico ao de antes).

`blend_with_color` e o novo `bump_texture_path` (textura de relevo) não
fazem parte da assinatura de `CreateTexture` -- são, no máximo, propriedades
no objeto que `CreateTexture` devolve, e nenhum nome de propriedade está
documentado neste repositório nem foi confirmado ao vivo. Em vez de
inventar um nome e declarar sucesso, cada um tenta uma lista curta de
nomes plausíveis (`BlendColor`/`Blend`/`UseColor` e
`BumpMapFileName`/`BumpFileName`/`BumpTextureFileName`) e o retorno informa
exatamente qual funcionou (`applied_via_property`) ou `None` se nenhum
funcionou -- nunca afirma "aplicado" sem confirmação. `bump_texture_path`
não é relevo PBR real (sem normal map); é uma segunda imagem aplicada pelo
mesmo mecanismo de textura de cor, a aproximação mais próxima que esta API
expõe.

**Esta correção/extensão não foi testada contra um SolidWorks real** --
ver `docs/audits/LIVE_TESTS_REQUIRED.md`, item LT-09.

### `list_p2m_appearances`: `pbr_extended` (15/10/2026, não verificado ao vivo)
Cada aparência agora traz um bloco `pbr_extended` com `roughness`,
`reflectivity` e `bump_map_path`, cada um como
`{"value": ..., "source_property": ...}`. Quando o objeto COM de material
de renderização desta instalação não expõe esse canal, `source_property` e
`value` vêm `None` -- a resposta honesta para "materiais PBR avançados",
sem inventar um valor padrão. Nenhum nome de propriedade usado aqui
(`Roughness`/`Smoothness`/`Blurriness`, `Reflectivity`/`ReflectionAmount`/
`Reflection`, `BumpMapFileName`/`BumpFileName`/`BumpTextureFileName`) está
documentado neste repositório nem foi confirmado ao vivo.

Ver `docs/audits/TOOL_INVENTORY.csv` (gerado nesta auditoria) para a lista completa das
167+1 ferramentas com categoria, schema, tipo leitura/escrita/destrutiva e
referências de teste unitário/live -- derivado mecanicamente do código atual,
não copiado de uma análise anterior.

## Historico de versoes

O que entrou em cada versao (ferramentas novas, correcoes, o que o teste ao
vivo mostrou) esta em [`CHANGELOG.md`](CHANGELOG.md).

## Notas de implementacao

### extract_assembly_data nao via o material nativo (corrigido em 02/10/2026)
Num teste de ponta a ponta com o backend do Alfa Detail (POST real do
resultado de `extract_assembly_data` contra a API rodando), toda peca que
tinha material atribuido via `set_material` chegava como "sem material" do
outro lado. Causa: `set_material` grava no slot nativo de material do
SolidWorks (`SetMaterialPropertyName2`), que e **separado** do
`CustomPropertyManager` -- `extract_assembly_data` so lia propriedades
customizadas, entao nunca via esse material. `extract_assembly_data` agora
le tambem o material nativo via `GetMaterialPropertyName2` (o parametro de
saida `Database` precisa de um VARIANT real por referencia, mesma familia de
quirk ja documentada para `OpenDoc6`/`ActivateDoc3`/`Save3` abaixo) e injeta
como propriedade `"Material"` -- mas **so como fallback**, se o modelo ja
tiver uma propriedade customizada `Material` explicita, essa continua
valendo. Cobre os dois casos que o proprio PDF do Alfa Detail preve:
"Material: get_custom_properties OU set_material".

### OpenDoc6/ActivateDoc3 com erro de tipo COM (corrigido em 02/10/2026)
`open_document` e, por consequencia, `insert_component` (que reabre o
componente e reativa a montagem internamente antes de inserir) passaram a
falhar com `Tipo nao correspondente` (DISP_E_TYPEMISMATCH) nesta instalacao.
Causa: `_open_doc6`/`_activate_doc3` passavam inteiros simples (`0, 0`) para
os parametros de saida (`Errors`/`Warnings`) de `OpenDoc6`/`ActivateDoc3`. Sob
o binding COM atual desta maquina isso nao e aceito -- precisa de VARIANTs
reais por referencia (`VT_BYREF | VT_I4`), exatamente o padrao que
`_save_doc3` ja usava para `Save3`. `AddComponent5` (usado por
`insert_component`) nunca teve bug proprio: o erro aparecia por causa da
chamada anterior a `_open_doc6`/`_activate_doc3` dentro do mesmo fluxo,
nao da propria insercao. As duas funcoes agora tentam o VARIANT primeiro e
caem para inteiros simples (`except TypeError`) se o binding ativo rejeitar
o VARIANT, cobrindo os dois modos de binding como `_save_doc3` ja fazia.

### Reconexao apos o SolidWorks reiniciar (corrigido em 16/08/2026)
A checagem de conexao viva era `_ = app.RevisionNumber`. Com a typelib
*gerada* (o caso do Python 3.14 aqui), `RevisionNumber` e um **metodo**, entao
esse acesso so montava o objeto de metodo do lado do Python e nunca chegava a
cruzar a fronteira COM -- um proxy de uma instancia do SolidWorks ja encerrada
passava na checagem como se estivesse saudavel. O resultado era que, depois de
fechar e reabrir o SolidWorks, *todas* as chamadas falhavam com "O servidor RPC
nao esta disponivel", inclusive `connect_solidworks`, porque o caminho de
reconexao nunca era alcancado. Agora `_com_is_alive()` invoca a chamada de
fato, e o mesmo padrao de no-op foi corrigido no rebuild de `shell_body`.

### Arquitetura
- Todas as chamadas COM rodam em uma unica thread dedicada (COM/STA).
- Timeout por ferramenta (`_timeout_budget_for`: 15s leitura simples, 60s feature,
  300s montagem, 900s exportacao em lote, 120s padrao); uma chamada que estoura
  o prazo e lembrada e as seguintes falham na hora citando-a; limpeza
  automatica no shutdown.
- Late binding (dispatch dinamico) -- igual ao ambiente do Claude Desktop.

### Camada de metodologia (design-sandbox): instructions, resource e prompt
As 157 ferramentas sao primitivas; sozinhas nao ensinam a IA a projetar bem.
O servidor expoe as outras duas primitivas do protocolo MCP para cobrir essa
lacuna, codificando o metodo ja validado nas replicas de engenharia deste
projeto (ver `docs/reports/RELATORIO_TESTES.md`) em vez de depender de disciplina manual
a cada conversa:
- `instructions` do `FastMCP(...)`: injetado automaticamente pelo cliente MCP
  ao conectar. Resume o loop planejar -> isolar -> construir incremental ->
  validar (`measure_body`/`validate_model`) -> inspecionar visualmente
  (`capture_standard_views`) antes de avancar.
- Resource `solidworks://tool-status`: reexpoe a secao "Status de
  verificacao" deste README (fonte unica, lida em tempo real -- sem
  duplicar o conteudo) para a IA saber quais ferramentas sao OK vs. EXP
  antes de depender de uma delas.
- Prompt `design_from_reference(part_description, key_dimensions,
  reference_source)`: roteiro passo a passo para projetar uma peca nova a
  partir de uma referencia real, formalizando o processo usado nas 5
  replicas de engenharia (bucha, rolamento, polia, clevis, helice).

### Base de conhecimento de engenharia (`.claude/`) -- adicionada em 03/10/2026
`.claude/CLAUDE.md` + `.claude/knowledge/*.md` sao a convencao de projeto do
Claude Code -- invisiveis para um cliente que so fala MCP (o caso normal de
uso deste servidor: Claude Desktop com a extensao instalada). Os mesmos
arquivos agora sao expostos tambem como resources
`solidworks://knowledge/index` + `solidworks://knowledge/<topico>` (materiais,
tolerancias-e-ajustes, gdt, elementos-de-maquina, chapa-metalica,
soldas-e-perfis-estruturais, processos-de-fabricacao, verificacao-e-qa,
roteiro-projetista, montagens-mecanicas-reais -- este adicionado em
04/10/2026 --, engrenagens -- adicionado em 06/10/2026) -- mesma fonte, lida
ao vivo, para os dois clientes
nunca divergirem. Cobrem o que a IA precisa saber pra projetar como um
projetista/engenheiro de verdade (nao so "como chamar a ferramenta"):
material certo por aplicacao, ajuste ISO entre furo e eixo, GD&T, dimensao
de parafuso/rolamento/chaveta padronizado, regras de chapa dobrada e de
weldment, DFM por processo de fabricacao, e um checklist de verificacao
antes de entregar a peca -- incluindo o que fazer quando pedem confirmacao
de resistencia (nao ha FEA neste MCP; ver `verificacao-e-qa`).

### Descoberta do SolidWorks
- `_find_solidworks_exe` le `HKLM\SOFTWARE\SolidWorks\SOLIDWORKS <ano>\Setup\
  SolidWorks Folder` e cobre o layout novo de pastas (`...\SOLIDWORKS\` sem ano,
  e `SOLIDWORKS (2)` para instalacoes lado a lado).

### Planos localizados
- Nomes de planos padrao sao resolvidos pela posicao na arvore (1o/2o/3o
  `RefPlane`), nao por texto em ingles.

### Unidades
- Distancias/raios sao convertidos para metros antes de qualquer chamada COM.

### Proximos passos para "producao completa"
As ferramentas EXP sao os proximos alvos.

Alvos da lista original, ja concluidos (validados ao vivo, hoje OK):
1. ~~`insert_drawing_view`~~ (desbloqueia toda a prancha de desenho -- categoria E).
2. ~~`create_weldment_profile`~~ (estruturas de plataformas/tanques).
3. ~~`create_base_flange`~~ (tanques de chapa).

Alvos atuais (v5.12.0), por impacto:
1. `set_material` -- densidade nao aplicada (afeta peso de toda BOM). Proximo
   passo: aplicar material a mao (Editar Material) e ver se a densidade muda;
   se nao mudar, o defeito e da instalacao. Contorno: `lookup_material_properties`.
2. Desenho tecnico EXP -- `add_drawing_dimension`, `insert_section_view`,
   `insert_detail_view`, `add_balloon`, `insert_bom_table`,
   `insert_cut_list_table`.
3. `add_end_cap` e `create_helix` -- retornam `None` via COM no SolidWorks 2025
   (defeito do lado do SolidWorks; investigar alternativa).
Recomenda-se validar cada fluxo gravando uma macro VBA na versao alvo e
espelhando a sequencia exata de selecao/chamada COM.
