# SolidWorks MCP Server

Servidor MCP em Python que controla o SolidWorks via COM (`win32com`), escrito
com o SDK oficial (`mcp`, usando `FastMCP`). **163 ferramentas** (v5.21.0).

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
npx @anthropic-ai/mcpb pack . solidworks-mcp-5.18.0.mcpb
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
`set_material` (EXP -- a densidade continua em 1000 kg/m3 (agua): confirmado
ao vivo em 05/10/2026, mesmo com o `.sldmat` resolvido, cinco ordens de
argumentos testadas e nome da configuracao ativa; o nome tambem le de volta
vazio. Desde a v5.12.0 a ferramenta verifica pela DENSIDADE e falha com erro
explicito em vez de reportar sucesso; `measure_body` devolve `density_kg_m3` e
avisa quando a massa esta em densidade de agua; `lookup_material_properties`
le densidade/modulo/escoamento direto do `.sldmat` (peso = volume x
densidade) como caminho correto enquanto a causa raiz -- no SolidWorks ou na
instalacao, nao no codigo -- nao e encontrada),
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

### Novas em v5.19.0 (caixa de engrenagens: o que o teste ao vivo mostrou)

Testado ao vivo no SolidWorks 2025 (API 33.4) montando uma caixa de dois
estagios (relatorio: `RELATORIO_CAIXA_ENGRENAGENS.md`).

- **`create_spur_gear` ganhou o que a caixa precisava** (e `create_gear`, que
  delega a ele, repassa): `phase` gira o contorno dentado em torno do eixo — o
  motor fica em `phase = 0` (dente sobre +X) e a engrenagem conduzida, do lado
  +X dele, usa `phase = 180 + 180/dentes` para ter um vao de frente para cada
  ponta de dente; sem isso duas engrenagens de dentes pares batem dente contra
  dente. `keyway_width`/`keyway_depth` abrem o rasgo de chaveta (lado +Y) com o
  mesmo primitivo a prova de snap, e o volume esperado da verificacao ja o
  desconta.
- **`draw_spline` corrigido na API 33.4**: `CreateSpline3` le o array como
  triplas (x, y, z), nao pares. Com pares, 3 pontos viravam uma reta e 10 pontos
  um emaranhado de 139 mm. Agora envia `x, y, 0`.
- Aprendizado do teste ao vivo: um contorno so fecha se as extremidades forem
  numericamente **identicas** (arcos por seno/cosseno diferem do spline
  arredondado em ~5e-8 m, acima da resolucao de 1e-8 m, e corte/boss recusam).
  O `create_spur_gear` ja evita isso, pois desenha um unico contorno com a
  inferencia desligada.

### Novas em v5.18.0 (a classe inteira, nao uma forma: 155 -> 157)

A v5.17.0 consertou **uma** forma. A analise do que mais sofria da mesma falha
mostrou que o problema era outro, e maior.

**1. O criterio documentado do snap estava errado.** `verificacao_e_qa.md` dizia
"peca grande, feature pequena". Mas a engrenagem saia lisa numa peca de
**44 mm** -- nao havia peca grande ali. O que o motor de inferencia junta e um
ponto novo que cai a poucos pixels de um ponto **que ja esta no esboco**: o que
decide e o **espacamento entre pontos consecutivos do perfil**, nao o tamanho da
feature nem o da peca. Dente involuto amostrado a 0,3 mm num pinhao de 44 mm =
0,7% da vista, dentro do raio de snap em qualquer zoom normal. Isso reclassifica
todo perfil que seja **curva amostrada**, nao so engrenagem.

**2. `draw_profile`** -- o primitivo que faltava. Recebe os pontos de uma curva
calculada e desenha a cadeia inteira com a inferencia desligada, numa chamada
(antes: 200 chamadas de `draw_line` pelo motor de inferencia, ou `draw_spline`,
que verificava so as duas pontas). Depois **le cada vertice de volta** e compara:
`max_point_deviation`, `displaced_points` com indice, `verified`. Perfil com
segmento faltando nao volta, levanta erro -- perfil incompleto e perfil aberto.
`create_spur_gear` foi refatorado para usar o mesmo primitivo, e agora **recusa**
extrudar um perfil deslocado em vez de so avisar.

**3. Tres falhas silenciosas achadas na auditoria, todas da mesma familia:**

- **`create_knurl` estava quebrado** (ou a um zoom de estar): celulas de 0,2 mm
  com vertices a 0,1 mm desenhadas direto pelo `SketchManager`, e a unica
  verificacao era `InsertWrapFeature2 != None`. Uma celula achatada gravava nada
  e a ferramenta dizia sucesso. Agora desenha pelo primitivo e confere vertice
  por vertice.
- **`draw_spline` verificava so as pontas** -- justamente os dois pontos que o
  snap tem menos chance de mover. Um ponto de controle **interior** deslocado
  deixava as duas pontas certas e a curva deformada voltava `verified: true`.
  Agora le os pontos de controle de volta (`interior_points`); quando o
  SolidWorks nao os expoe, o retorno diz que o interior esta **nao verificado**,
  em vez de deixar as pontas passarem por prova.
- **Canaletas de anel do `create_automotive_piston`:** dois circulos a 3 mm um do
  outro, ambos pelo motor de inferencia. `draw_circle` ja media o proprio raio;
  ninguem lia. Agora le, e levanta erro se a canaleta saiu rasa ou colapsada.

**4. `create_gear` virou delegacao.** Um `create_gear` entrou na `main` em
paralelo com este trabalho, construindo a engrenagem do jeito que se modela a
mao: blank no diametro de topo, UM vao de dente involuto cortado, e
`circular_pattern` desse corte. A aritmetica do involuto nele estava **certa** --
nao era bug de matematica. O que faltava eram as duas coisas desta versao: os
flancos iam por `CreateSpline3`, ou seja pelo motor de inferencia (12 amostras
num flanco de 4,5 mm = pontos a 0,4 mm, dentro do raio de snap), e a unica
verificacao era "o pattern nao voltou None" -- um vao achatado, repetido 20
vezes, e um blank, e nada olhava. O nome continua (quem ja chamava `create_gear`
nao quebra, e os argumentos e as chaves de retorno antigas seguem iguais), mas
agora ele chama `create_spur_gear`. `tooth_gap_feature` volta `None`: nao existe
corte-semente pra nomear, porque os dentes nao sao feitos cortando um.

**5. Posicao das pecas na montagem.** `create_automotive_piston_assembly` nao
tem mates por projeto -- os cinco offsets sao a unica coisa que posiciona as
pecas. O `insert_component` ja devolvia `actual_position`/`deviation`/`verified`
de cada uma, lido do SolidWorks; nada lia. Agora cada colocacao e conferida, a
ferramenta levanta erro se alguma peca nao chegou onde foi posta, e o retorno
traz `component_positions` com a pose medida de todas. O `position_check` tambem
diz o que esperar do `verify_assembly_positions` aqui: M01 nos cinco (montagem
deliberadamente sem mates) e M03 em `connecting_rod`/`wrist_pin`/`bearing`, que
sao modeladas no lugar e compartilham a origem de insercao de proposito.

### Novas em v5.17.0 (a engrenagem tem dentes, 154 -> 155)

**O sintoma:** toda engrenagem modelada aqui saia lisa. Um cilindro com furo.

**Nao era um bug, era a aritmetica.** Um flanco involuto precisa de uma duzia
de pontos a decimos de milimetro um do outro. Toda chamada `draw_*` passa pelo
motor de inferencia do SolidWorks, cujo raio de snap e medido em **pixels de
tela** (ver "O esboco e onde o erro nasce" em
`.claude/knowledge/verificacao_e_qa.md`), e dois pontos em escala de dente, um
do lado do outro, e exatamente o que esse motor junta -- **movendo** o ponto,
sem erro nenhum. O perfil do vao de dente chegava degenerado ao `cut_extrude`,
e um vao achatado repetido 20 vezes pelo `circular_pattern` e um disco de novo.
Nenhuma das 154 ferramentas avisava: `validate_model` passava, `measure_body`
devolvia uma massa plausivel, e a peca era entregue como engrenagem.

**O conserto:** `create_spur_gear`. O contorno **inteiro** -- todos os dentes,
fechado -- e calculado analiticamente em `gear_geometry.py` (ISO 53, altura
cheia: adendo 1,0*m, dedendo 1,25*m, 20 graus, sem correcao de perfil) e
desenhado como **um** perfil com o motor de inferencia desligado
(`SetAddToDB`), depois extrudado uma vez. Sem corte, sem padrao, nada pra
snapar. Inclui raio de pe tangente (0,38*m, o raio de ponta do cremalheira da
ISO 53), furo de eixo opcional e a distancia entre centros pro par engrenar.

**E ele se mede.** A secao transversal exata do contorno e conhecida, entao o
volume real do solido extrudado e comparado com ela: `verified` so vem `true`
quando fecham dentro de `volume_tolerance`, e `teeth_present` e uma checagem
separada e mais grosseira de que o material dos vaos saiu mesmo do blank --
essa e a que pega "saiu lisa". Nao da pra uma engrenagem sair lisa e ser
reportada como pronta.

Limites declarados na docstring: so engrenagem cilindrica de dentes retos,
externa, sem correcao de perfil. Helicoidal, interna, conica, coroa/rosca sem
fim e chanfro de topo ficam de fora -- e, como sempre, nao ha FEA aqui pra
conferir resistencia do dente.

### Novas em v5.16.0 (movimento: a mate de mecanismo conferida)

A correcao de alinhamento da v5.14.0 **estava incompleta**, e isso explica o
sintoma: modelagem estatica saia bem, movimento saia torto.

**1. O `add_advanced_mate` ainda forcava `ALIGNED`.** A v5.14.0 trocou o
literal `0` do segundo parametro do `AddMate5` (`swMateAlign_e`) por um `align`
configuravel **no `add_mate`**, e deixou o literal `0` identico no
`add_advanced_mate`. Acontece que `add_advanced_mate` e justamente a ferramenta
com que um mecanismo e construido: e dela que saem `distance`, `angle`, `gear`,
`width`, `symmetric` e `lock`. Montagem estatica usa `add_mate` (corrigido);
mecanismo usa `add_advanced_mate` (nao corrigido) — por isso o movimento saia
torto e o modelo parado nao. Agora ele tem o mesmo `align`
('aligned' | 'anti_aligned' | **'closest'**, default).

**2. Nenhuma das 4 mates de mecanismo dizia onde deixou as pecas.** Uma mate
nao recebe uma posicao: recebe uma *relacao*, e a posicao e a consequencia.
Quando ela resolve, o SolidWorks arrasta o que ainda esta livre ate a
configuracao permitida — correto, e exatamente onde nasce o "pedi movimento e
saiu fora de posicao". `add_advanced_mate`, `add_cam_follower_mate`,
`add_screw_mate` e `add_rack_pinion_mate` agora devolvem `components` com
`position_before`, `position_after` e `moved.distance` de cada componente pego.
As mates que acoplam dois graus de liberdade (`gear`, parafuso, pinhao) fixam so
a **razao** entre eles e deixam os dois livres: criar a mate desliza a peca ate
onde o acoplamento fecha, e esperar que ela fique onde estava e o erro.

**3. Criar/ativar motion study pode mover a montagem, sem registro.** O
MotionManager troca a montagem para o estado do estudo, e o SolidWorks nao
guarda a pose anterior em lugar nenhum — e a forma mais provavel de perder uma
pose de mecanismo que deu trabalho. `create_motion_study` agora mede:
`components_moved` (pior primeiro) e `pose_preserved`. O fluxo certo e
`capture_assembly_pose(filepath=...)` **antes**, e `restore_assembly_pose` se o
relatorio mostrar movimento indesejado.

Todas as quatro trazem `mechanism_note`: nenhuma mate segura um grau de
liberdade livre, porque o grau livre **e** o movimento — quem torna a pose
reproduzivel e `capture_assembly_pose`/`restore_assembly_pose`, nao outra mate.

### Novas em v5.15.0 (geometria de peca conferida, 153 -> 154)

A v5.14.0 consertou a camada de **montagem**. A de **peca** continuava 100%
eco: auditoria com AST de todo o `server.py` achou **153 chaves de coordenada
ou dimensao em 86 ferramentas, 95 delas eco** (o valor devolvido vinha dos
parametros), contra 9 que liam algo de volta — e cinco dessas nove eram so o
titulo do documento. O desvio nasce no esboco, e naquele momento ninguem podia
olhar.

**1. O snap de esboco e em espaco de TELA, e move a geometria.** Toda chamada
`Create*` do `ISketchManager` passa pelo motor de inferencia do SolidWorks, cujo
raio de snap e uma distancia em pixels. Medido ao vivo numa peca de
1,74 x 1,80 x 1,50 m: um retangulo de 40 x 20 mm foi **recusado** no zoom que
enquadrava a peca inteira (`CreateCornerRectangle` devolveu `null`), enquanto
`CreateLine`, que nao passa por esse caminho, funcionou no mesmo zoom; a mesma
chamada de retangulo funcionou depois de aproximar o zoom. Essa e a falha
*ruidosa*. A *silenciosa* e pior: quando o snap apenas **desloca** um ponto para
um vertice vizinho, a chamada tem sucesso e devolve um segmento valido no lugar
errado.

- As 8 primitivas (`draw_line`, `draw_centerline`, `draw_circle`,
  `draw_rectangle`, `draw_arc`, `draw_polygon`, `draw_spline`, `draw_line_3d`)
  agora **leem o segmento criado de volta** e devolvem `actual`, `deviation`,
  `verified` e **`snapped`** — a geometria foi criada e o SolidWorks a colocou
  em outro lugar. O `draw_rectangle` devolvia `width = abs(x2 - x1)`: aritmetica
  nos proprios argumentos, zero contato com o modelo. Agora ha
  `actual_width`/`actual_height`/`actual_corners`, medidos dos quatro segmentos.
- A recusa ruidosa deixou de mentir. A mensagem era "Is a sketch active?" com o
  esboco aberto o tempo todo; agora o esboco e checado **primeiro**, a recusa e
  reportada como o que e, e ha **uma tentativa de retry com zoom** na area alvo
  (reportada em `zoom_retry`).
- `list_sketch_entities` (nova, somente leitura) e o equivalente do
  `get_component_transform` no nivel de peca: tipo, pontos, centro, raio e se e
  geometria de construcao, de cada segmento. Sem ela nao havia **como** perguntar
  onde a geometria de esboco foi parar — `GetSketchSegments` aparecia uma unica
  vez em 12.300 linhas, enterrada dentro do `create_weldment_profile`.

**2. A primeira peca da montagem e ancorada, e ninguem avisava.** O SolidWorks
fixa o primeiro componente inserido. Uma peca fixa **nunca se move**: toda mate
contra ela e resolvida movendo *a outra*. O `insert_component` nunca lia
`IsFixed`, entao a IA matava a peca principal esperando que ela se alinhasse,
via a outra se mover, e concluia que a peca principal "nao muda de posicao".
Agora o retorno traz `fixed` e, quando verdadeiro, o aviso com a consequencia e
o `float_component` como saida.

**3. O SolidWorks reduz dimensao que nao cabe, sem erro.** Todas as ferramentas
de feature checavam `if feat is None` e nada mais — a falha ruidosa coberta, o
desvio silencioso descoberto. `fillet_edges`, `chamfer_edges`, `shell_body`,
`extrude_sketch` e `cut_extrude` agora leem a dimensao de volta
(`actual_radius`, `actual_distance`/`actual_angle`, `actual_thickness`,
`actual_depth`) com `measurement_method` dizendo por qual rota foi lida.

**4. `move_copy_body` devolvia a translacao pedida.** Agora mede a caixa
envolvente antes e depois (`measured_center_shift`). So **verifica** no caso sem
ambiguidade (translacao pura, `copy=False`, sem rotacao); com rotacao ou copia a
caixa muda de forma e `verified` vem `None` com `verification_skipped` dizendo
por que, em vez de um alarme falso.

**5. `joint_centers` do pistao prometia garantia que nao tinha.** Era aritmetica
nos argumentos publicada sob uma chave chamada `contract`, afirmando que os dois
mancais e o pino realmente estavam ali. Agora traz `measured: false` e
`design_intent` dizendo que foi calculado, nao medido.

### Novas em v5.14.0 (posicao de montagem conferida + mecanismos, 150 -> 153)

Tres bugs distintos de posicionamento, e o caso de montagem com movimento.

**1. Peca multi-corpo era inserida fora do lugar.** A correcao de origem do
`insert_component` (que compensa o `AddComponent5`, que posiciona o CENTRO da
caixa envolvente no ponto pedido, nao a origem) media a caixa de
`bodies[0]` apenas. Toda estrutura soldada e todo perfil estrutural tem mais
de um corpo, e a caixa do primeiro corpo descreve so um pedaco da peca: a
peca saia deslocada pela diferenca entre as duas caixas. Agora a caixa e a
uniao de **todos** os corpos solidos.

**2. O alinhamento da mate era forcado em toda chamada.** O segundo parametro
do `AddMate5` e `swMateAlign_e` (ALIGNED=0, ANTI_ALIGNED=1, CLOSEST=2) e
estava fixo em `0` — forcando ALIGNED independentemente da geometria. Para
duas faces feitas pra se encarar (duas faces planas pressionadas, eixo contra
ombro), ALIGNED e a solucao errada; o SolidWorks nao reporta erro, porque e
uma solucao matematicamente valida, so nao e a que encaixa. Era a causa da
"brecha torta" sem erro nenhum. `add_mate` agora tem `align`
('aligned' | 'anti_aligned' | **'closest'**, novo default) e devolve
`geometry_check` com o angulo real medido entre as direcoes de referencia das
duas faces, em coordenadas da montagem.

**3. Nenhuma ferramenta de posicionamento conferia o proprio resultado.**
Devolviam os parametros de entrada de volta (padrao "eco"), entao uma peca no
lugar errado era indistinguivel de sucesso. Agora `insert_component` e
`set_component_transform` leem a pose de volta do SolidWorks e devolvem
`requested_position` ao lado de `actual_position`, `deviation` e `verified`;
`get_component_transform` rotula a convencao da matriz
(`rotation_convention: "row-major"`) pro chamador nao adivinhar.
`verify_assembly_positions` (nova) e o gate de montagem, no formato do
`verify_drawing`: `M01` nem fixa nem matada, `M02` sobre a origem, `M03` duas
pecas na mesma posicao, `M04` fixa e matada ao mesmo tempo, `M05` posicao
ilegivel, `M06` peca declarada movel mas fixa.

**4. Montagem com movimento perdia as posicoes.** Uma montagem estatica
guarda a posicao de graca: tudo nela e fixo ou totalmente matado, e um
rebuild devolve cada peca ao mesmo lugar. Um mecanismo nao pode funcionar
assim — suas pecas moveis sao sub-restringidas **de proposito**, e esse grau
de liberdade livre e o movimento. A pose em que ele esta e uma de infinitas
validas, e o proximo drag, rebuild ou motion study a substitui sem registrar
qual era. Nao e um bug de mate: nenhuma mate pode travar um grau de liberdade
que deve ficar livre.

- `verify_assembly_positions` ganhou `moving_components`: os nomes das pecas
  que **devem** se mover. Sem isso, cada uma delas dispara `M01`, porque
  estar sub-restringida e exatamente o que uma peca movel e. Declaradas,
  ficam isentas de `M01` e passam a ser checadas pelo problema oposto
  (`M06`: fixa quando deveria estar livre).
- `capture_assembly_pose` / `restore_assembly_pose` (novas): gravam a pose
  exata de cada componente e a colocam de volta. Guardam a matriz de rotacao
  3x3 que o SolidWorks reporta e a translacao em metros — **sem** conversao
  pra angulo de Euler, porque decompor e recompor nao e exato e uma pose de
  mecanismo tem que voltar exata. Com `filepath`, a pose vira um JSON e
  sobrevive a sessao (varios arquivos = varias posicoes do mesmo mecanismo).
  O restore mede o resultado: se uma mate arrastar o componente no rebuild,
  isso aparece como `deviation` em vez de sucesso limpo.

### Novas em v5.12.0 (7 ferramentas, 143 -> 150)
- Camada de leitura/verificacao de desenho (modulo `alfa_drawing.py`, sem COM,
  testado em Linux): `get_drawing_layout`, `get_view_entities`,
  `get_view_dimensions`, `dimension_by_entity_ids` (cota por entidade, com
  `expected_mm` e erro E01 se medir outra coisa) e `verify_drawing` (relatorio
  estruturado de cobertura de cotas, sobreposicao, escala). Validado ao vivo
  29/29 em `tests/run_drawing_live_test.py`.
- `extract_assembly_bom`: BOM recursiva (todos os niveis), chave (arquivo,
  configuracao), ignora suprimidos/excluidos. `extract_assembly_data` fica
  mantida para compatibilidade, marcada como substituida.
- `lookup_material_properties`: propriedades lidas do `.sldmat`.

### Arquitetura
- Todas as chamadas COM rodam em uma unica thread dedicada (COM/STA).
- Timeout de 120s por operacao COM; limpeza automatica no shutdown.
- Late binding (dispatch dinamico) -- igual ao ambiente do Claude Desktop.

### Camada de metodologia (design-sandbox): instructions, resource e prompt
As 157 ferramentas sao primitivas; sozinhas nao ensinam a IA a projetar bem.
O servidor expoe as outras duas primitivas do protocolo MCP para cobrir essa
lacuna, codificando o metodo ja validado nas replicas de engenharia deste
projeto (ver `RELATORIO_TESTES.md`) em vez de depender de disciplina manual
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
