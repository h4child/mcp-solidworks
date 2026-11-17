# Changelog

Formato inspirado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).
A versao e declarada em `pyproject.toml` e repetida em `manifest.json`
(`tests/test_contract.py` confere as duas e o README). A matriz de
confiabilidade OK/EXP por ferramenta continua no README, na secao
"Status de verificacao" -- e ela que o resource `solidworks://tool-status`
serve ao cliente.

## [5.22.0] - 2026-11-17

Primeira versao do roteiro em `PLANO_DE_MELHORIA_MCP.txt` (Fase 0: higiene).
Nenhum comportamento de ferramenta mudou.

### Adicionado (entrou no `main` depois da 5.21.5, sem bump de versao)
- `get_default_templates`, `set_default_templates`,
  `create_document_from_template`, `save_as_template`: templates padrao do
  System Options e criacao a partir de um template especifico.
- `run_command_by_id`: executa um comando da UI por ID (`ISldWorks::RunCommand`).
  Anotada `openWorldHint=True`, como `run_macro` e `execute_python`.
- `set_section_view`, `clear_section_view`: vista em corte 3D (nomes de metodo
  COM ainda nao confirmados ao vivo; ver LT-10).
- `set_sheet_scale`, `insert_model_items`: escala da folha e insercao de itens
  do modelo em desenhos (ver LT-11).
- `apply_texture` com mapeamento U/V real; blend/relevo e `pbr_extended` em
  `list_p2m_appearances` em modo best-effort (BUG-03).
- `scripts/build_chatgpt_plugin.py`: gera o plugin ChatGPT a partir do codigo
  principal; `packaging/chatgpt/` guarda so `plugin.json` e `mcp.json`.
- `CHANGELOG.md` (este arquivo) e `PLANO_DE_MELHORIA_MCP.txt`.

### Corrigido
- COM timeout: uma chamada travada nao derruba mais as seguintes com uma
  mensagem enganosa (BUG-01).
- `validate_model` deixou de se anunciar `readOnlyHint=True` (BUG-02).
- CI vermelha: o teste de contrato de ferramentas open-world nao conhecia
  `run_command_by_id`; 21 erros de lint (`ruff`) em `server.py` e `tests/`.
- Contagem de ferramentas desatualizada no README, `.claude/CLAUDE.md`,
  `tests/README.md` e comentarios de `server.py` (diziam 158 ou 150; sao 167
  incondicionais + `execute_python`).

### Removido
- `chatgpt-solidworks-mcp-plugin/`: copia manual do servidor parada na 5.21.0,
  ~900 linhas atras do principal e ainda com BUG-01/02/03. Substituida pelo
  script de build acima.
- As receitas de produto completo (`create_automotive_piston`,
  `create_automotive_piston_assembly`,
  `create_automotive_piston_with_connecting_rod`,
  `create_pedestal_fan_propeller`) sairam de `server.py` (~830 linhas) para
  `examples/recipes/product_recipes.py`, sem alteracao. Nunca foram
  ferramentas MCP.

### Organizacao
- Relatorios e auditorias foram para `docs/audits/` e `docs/reports/`;
  o script e a especificacao do redutor planetario para `examples/`.
- CI roda em push para qualquer branch, nao so `main`.

## [5.21.0] - [5.21.5] - 2026-10-09 a 2026-10-12
- 5.21.0: `cut_part_end` (corte de extremidade / mitra por plano) com resultado medido.
- 5.21.1: inicio/fim/centro de segmento de esboco lidos pelo helper de dispatch COM.
- 5.21.2: `cut_extrude(through_all, both_directions)` agora corta dos dois lados.
- 5.21.3: `insert_component` corrige a origem de sub-montagens tambem.
- 5.21.4: `hole_wizard` tipo "simple" e selecao de face independente da camera.
- 5.21.5: `EditRebuild3()` chamava o bool que o dispatch dinamico ja tinha devolvido.

## [5.20.0] - 2026-10-09
- `create_profile_part`, `create_tube_part`, `add_mate_by_name`,
  `interference_report`, `set_view_direction` (gaps revelados pela escada
  industrial; ver `docs/reports/RELATORIO_ESCADA_INDUSTRIAL.md`).

## Versoes anteriores (texto original, movido do README em 5.22.0)

### Novas em v5.19.0 (caixa de engrenagens: o que o teste ao vivo mostrou)

Testado ao vivo no SolidWorks 2025 (API 33.4) montando uma caixa de dois
estagios (relatorio: `docs/reports/RELATORIO_CAIXA_ENGRENAGENS.md`).

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

### Novas em v5.17.0 (contexto para a IA: resource novo + descricoes corrigidas)

As tres versoes anteriores criaram uma camada de medicao inteira — `verified`,
`snapped`, `moved`, `deviation`, `measurement_method`, `components_moved` — e
**nada explicava ao cliente como ler esses campos**. Um `verified: null` lido
como sucesso reintroduz exatamente o problema que a medicao resolve.

**`solidworks://knowledge/campos-de-verificacao` (resource novo, 13 no total).**
Registrado **primeiro** na lista, porque e o unico que vale para qualquer pedido:
fala do RETORNO DAS FERRAMENTAS, nao de engenharia. Cobre o contrato
(`requested_*` vs `actual_*`), por que `verified: false` e `verified: null` sao
coisas diferentes, o que fazer com cada campo especifico, as **cinco armadilhas
confirmadas** (snap de esboco em espaco de tela, alinhamento de mate forcado,
primeiro componente ancorado, dimensao de feature reduzida sem erro, pose de
mecanismo que nenhuma mate segura) e — deliberadamente — a lista do que **ainda
nao foi validado ao vivo**, para que nada disso seja citado como garantia.

**Duas descricoes de resource estavam desatualizadas**, e isso e pior do que
parece: a descricao e o que o modelo usa para **decidir** se abre o resource,
entao uma descricao que nao menciona uma secao garante que aquela secao nunca
seja lida. `montagens-mecanicas-reais` nao falava de mecanismo, motion study nem
`add_advanced_mate`; `verificacao-e-qa` nao falava do snap de esboco. Corrigidas.

**Testes de resource, que nao existiam.** 19 novos, entre eles: todo arquivo em
`.claude/knowledge/` tem de estar registrado (um arquivo nao registrado alcanca o
Claude Code neste repo e mais nada — do Claude Desktop, `.claude/` e invisivel e
os resources sao tudo), todo resource registrado aponta para um arquivo que
existe, toda descricao tem corpo, o resource novo cobre cada campo que as
ferramentas devolvem, e ele admite o que nao foi provado.

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
