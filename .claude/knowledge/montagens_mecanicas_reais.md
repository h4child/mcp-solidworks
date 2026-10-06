# Montagens mecânicas — encaixe real, não só "parece certo"

Casos reais encontrados ao montar um pistão + biela (2026-10-05) onde o
modelo validava (`validate_model` 0 erros) e parecia correto numa vista
isométrica solta, mas a peça não encaixava como a referência mecânica real
exige. Nenhum desses erros gera uma mensagem de erro do SolidWorks — todos
exigem verificação ativa, não só "rodou sem exceção".

## 1. Pesquise a referência mecânica ANTES de modelar uma montagem

Antes de desenhar peças que se encaixam (pino+biela, eixo+rolamento,
parafuso+rosca), busque como a montagem real funciona — patente, diagrama
técnico, manual. Um pistão automotivo real tem **dois mancais internos
separados** (pin bosses) com um vão entre eles exatamente para receber o
olhal menor da biela; não é um furo único contínuo atravessando a peça. Um
furo único passante (mais simples de modelar) parece plausível à primeira
vista mas não é como a peça real se monta — a biela fica apoiada por fora,
não encaixada por dentro. Ver `roteiro_projetista.md` para o fluxo geral;
isto é especificamente sobre a etapa "a peça se encaixa com outra peça".

## 2. Mate `concentric` trava só 2 graus de liberdade — não a posição ao longo do eixo

Uma mate concêntrica alinha o eixo de dois cilindros (remove 2 translações +
fixa a direção), mas **deixa livre o deslizamento ao longo do eixo
compartilhado** e a rotação em torno dele. Se você faz
`add_advanced_mate(concentric)` e depois `fix_component` sem conferir a
posição final ao longo desse eixo, a peça trava onde o solver a deixou —
não necessariamente onde você queria. Isso só aparece calculando a posição
global real (ver item 4) ou inspecionando de um ângulo que mostre esse eixo
de frente (ver item 3); uma isométrica genérica esconde o problema.

Depois de uma mate `concentric`, sempre:
1. `get_component_transform` na peça recém-mateada — leia a posição real
   resultante, não assuma que ficou onde você mandou antes da mate.
2. Verifique essa posição contra a geometria real que deveria limitá-la
   (ex.: o vão entre dois mancais) antes de `fix_component`.

## 2.1. Alinhamento de mate forçado é uma segunda causa de peça torta (corrigido v5.14.0)

Até a v5.13.0, `add_mate` chamava `AddMate5` com o parâmetro de alinhamento
(`swMateAlign_e`) fixo em `ALIGNED`. É diferente do item 2: mesmo escolhendo
as faces certas e com a posição dentro da tolerância, a mate podia resolver
com as duas faces apontando pro MESMO lado quando a geometria pede que elas
se encarem (duas faces planas pressionadas, eixo contra ombro) — encaixe
torto ou com brecha, e o SolidWorks **não reporta erro**, porque `ALIGNED` é
uma solução matematicamente válida, só não é a física certa.

Desde a v5.14.0 o default é `align='closest'` (o solver escolhe o lado mais
próximo da pose atual) e `add_mate` devolve `geometry_check` com o ângulo
medido entre as direções de referência das duas faces. Leia esse campo: se
não for 0, 90 ou 180 graus, a peça está torta — e agora é um número, não uma
impressão de vista isométrica. Se `closest` resolver do lado errado, chame de
novo com `align='aligned'` ou `'anti_aligned'`.

**A correção da v5.14.0 estava incompleta (fechado na v5.16.0).** Ela consertou
o `add_mate` e deixou o literal `0` idêntico no `add_advanced_mate` — que é
justamente a ferramenta com que um mecanismo é construído, porque é dela que
saem `distance`, `angle`, `gear`, `width`, `symmetric` e `lock`. Resultado
prático: montagem estática (feita com `add_mate`) saía certa, e **movimento
saía torto**, porque toda mate de mecanismo forçava `ALIGNED`. Desde a v5.16.0
o `add_advanced_mate` tem o mesmo `align` com o mesmo default `'closest'`.

## 2.1.1. Toda mate de mecanismo move peça — e agora diz quais

Uma mate não recebe uma posição: recebe uma *relação*, e a posição é a
consequência. Quando a mate resolve, o SolidWorks arrasta o que ainda está
livre até a configuração que ela permite. Isso é correto, e é exatamente onde
nasce o "pedi movimento e saiu fora de posição": a geometria passa a estar num
lugar que ninguém escolheu.

Desde a v5.16.0, `add_advanced_mate`, `add_cam_follower_mate`, `add_screw_mate`
e `add_rack_pinion_mate` devolvem `components` com `position_before`,
`position_after` e `moved.distance` de cada componente pego. Leia o `moved`: se
a peça que você só queria *acoplar* andou 40 mm, a mate está resolvendo num
lugar diferente do que você imaginou — corrija a mate, não compense com a
próxima.

As mates que acoplam dois graus de liberdade (`gear`, parafuso, pinhão) fixam
só a **razão** entre eles e deixam os dois livres: criar a mate desliza a peça
até onde o acoplamento fecha. Esperar que ela fique onde estava é o erro.

## 2.1.2. Criar/ativar motion study pode mover a montagem

`create_motion_study` com `activate=True` faz o MotionManager trocar a montagem
para o estado do estudo, e o SolidWorks **não registra** a pose anterior em
lugar nenhum. É a forma mais provável de perder uma pose de mecanismo que deu
trabalho pra alcançar.

Desde a v5.16.0 a ferramenta mede: `components_moved` lista quem saiu do lugar
(pior primeiro) e `pose_preserved` responde a pergunta direta. O fluxo certo é
`capture_assembly_pose(filepath=...)` **antes** de criar o estudo, e
`restore_assembly_pose` se o relatório mostrar movimento indesejado.

## 2.2. Mecanismo: a posição tem que ser GRAVADA, nenhuma mate a segura

Montagem estática guarda a posição de graça — tudo fixo ou totalmente matado,
e um rebuild devolve cada peça ao mesmo lugar. Mecanismo não: as peças móveis
são sub-restringidas **de propósito**, e é esse grau de liberdade livre que é
o movimento. A pose em que o mecanismo está é uma de infinitas válidas, e o
próximo drag, rebuild, motion study ou edição de mate a substitui sem
registrar qual era. É por isso que "com movimentação fica perdido as
posições" enquanto a mesma montagem modelada estática fica quieta.

Não tente resolver isso com mate nem com `fix_component`: travar a peça mata
o mecanismo. O caminho é:

1. Declare as peças móveis em `verify_assembly_positions(moving_components=[...])`
   — sem isso, cada uma dispara `M01` ("nem fixa nem matada"), que é
   exatamente o que uma peça móvel é. Declaradas, viram isentas de `M01` e
   passam a ser checadas pelo problema oposto (`M06`: fixa quando deveria
   estar livre).
2. Grave a pose com `capture_assembly_pose(label="biela a 45 graus",
   filepath="...pose_45.json")` antes de qualquer coisa que mova o mecanismo
   — inclusive antes de gerar desenho, porque a prancha mostra a pose em que
   a montagem estava.
3. Volte com `restore_assembly_pose(filepath=...)`. Ele mede o resultado: se
   uma mate arrastar o componente no rebuild, aparece como `deviation`, não
   como sucesso.

Uma pose por arquivo = uma posição do mecanismo. É assim que se compara duas
posições (PMS/PMI de um pistão, portão aberto/fechado) de forma reproduzível.

## 3. Vistas ortográficas por eixo, não só isométrica

Uma vista isométrica "parece encaixada" com muita facilidade mesmo quando
duas peças estão a 20mm de distância no eixo errado — a perspectiva
disfarça gaps ao longo do eixo que aponta quase na direção da câmera. Para
confirmar encaixe de verdade:

- Vista alinhada ao eixo da mate (ex.: `set_view("top")` ou `"right")`,
  dependendo de qual eixo global é o eixo da mate) — isso mostra projeção
  2D limpa onde um gap ou sobreposição errada salta aos olhos.
- Depois, `zoom_to_area` na região exata da junção, não `zoom_to_fit` da
  peça inteira — detalhe de 5-20mm se perde numa peça de 80mm de vista geral.

## 4. Verificação dimensional por cálculo, não só visual

Quando a vista não deixa claro (ou antes de confiar nela), calcule a
posição GLOBAL esperada a partir da transformação real do componente
(`get_component_transform` devolve `translation` + `rotation_matrix`) e
compare contra a geometria conhecida da peça parceira (ex.: centro e raio
de um furo, de um `list_faces` feito na peça sozinha antes de montá-la).

**A convenção da matriz de rotação devolvida é por LINHA, não por coluna**:
para `rotation_matrix = [[r00,r01,r02],[r10,r11,r12],[r20,r21,r22]]`, um
ponto local `(lx,ly,lz)` vira global via:
```
global.x = r00*lx + r01*ly + r02*lz
global.y = r10*lx + r11*ly + r12*lz
global.z = r20*lx + r21*ly + r22*lz
```
(mais a translação). Confirmado ao vivo 2026-10-05: uma suposição por
coluna (trocando o papel de linha/coluna) produz um ponto plausível mas
errado, que falha ao selecionar a face certa — e se você ajustar o ponto
até a seleção "funcionar", está validando a convenção errada, não a certa.
Teste a convenção com um ponto de verificação simples (ex. uma face plana
larga e inconfundível) antes de confiar nela para algo com tolerância
apertada como o eixo de um furo.

## 5. Mancal/boss só "encaixa" de verdade se a geometria se FUNDE com a parede

Um boss cilíndrico deslocado do eixo central só vira parte sólida da peça
se seu raio alcançar a parede existente. Se `offset` é a distância do
centro do boss até o eixo da peça e `parede_interna` é o raio da cavidade
oca mais próxima, o boss só se funde quando:
```
raio_do_boss ≥ parede_interna − offset
```
Com raio menor que isso, o boss fica como um corpo **flutuante,
desconectado**, dentro do oco — `validate_model` não acusa erro (SolidWorks
aceita multi-corpos dentro de uma peça), mas visualmente aparece como um
círculo solto, sem fusão suave com a parede, em vez de um relevo contínuo.
Confirmado ao vivo: boss raio 15mm a 17mm do eixo, parede interna a
37,25mm, não tocava a parede (37,25−17=20,25 > 15) — corrigido para
raio 22mm (dentro do intervalo 20,25–23,75mm que funde sem furar a parede
externa).

## 6. `create_reference_plane`: `flip=True` e offset negativo não funcionam para o plano "right"

Confirmado ao vivo, 2026-10-05: `create_reference_plane(reference="right",
offset=X, flip=True)` e `create_reference_plane(reference="right",
offset=-X, flip=False)` **ambos ignoram silenciosamente a direção** e
criam o plano em X=0 (na própria origem), não no deslocamento negativo
esperado — sem erro, sem aviso. Testado isoladamente com offset=20mm,
reproduzido duas vezes. O mesmo padrão (`flip=True`) funciona normalmente
para o plano "front". Até isso ser corrigido no servidor: para um
deslocamento no sentido negativo do eixo X a partir do plano "right", **não
use `flip` nem offset negativo** — construa a feature do lado positivo
(que funciona) e use `mirror_feature` para espelhar para o lado negativo.

## 7. Depois de QUALQUER falha de `create_sketch`, pare — não continue a sequência

Se `create_sketch(plane=X)` falhar (ex. nome de plano errado/desatualizado),
as chamadas seguintes (`draw_circle`, `close_sketch`, `extrude_sketch`) **não
necessariamente falham também** — elas podem prosseguir usando algum
contexto de esboço remanescente, produzindo geometria real, "bem-sucedida",
na peça inteiramente errada. Confirmado ao vivo duas vezes: um
`create_sketch` com nome de plano desatualizado falhou, mas o
`draw_circle`+`extrude_sketch` seguintes ainda criaram um disco real — só
que na orientação do plano frontal padrão, não no plano pretendido. Sempre
confira o retorno de `create_reference_plane` e use o nome EXATO devolvido
(nunca reuse um nome de plano assumido de uma tentativa anterior — a
numeração `PlanoN` não é estável entre criar/deletar). Se `create_sketch`
falhar, pare a sequência e corrija antes de continuar.

## 8. `close_document` fecha o documento ATIVO — confirme qual é antes de chamar

Depois de um erro (ex. `save_document` falhou), o documento "ativo" pode
não ser o que você espera. Confirmado ao vivo: um `save_document` que
falhou foi seguido de `close_document(save=False)` pretendendo fechar um
documento ANTIGO e indesejado — mas o documento ativo no momento era na
verdade a peça nova, ainda não salva, que foi descartada por engano,
perdendo um trabalho de reconstrução inteiro. **Sempre `get_document_info`
logo antes de um `close_document(save=False)`** quando não há certeza
absoluta de qual documento está ativo — o custo de checar é uma chamada
barata; o custo de errar é perder trabalho não salvo sem aviso.

## 9. Montagem com componente referenciando um arquivo que você quer sobrescrever

`save_document` falha silenciosamente (sem detalhe útil) se outro documento
aberto (ex. uma montagem) mantém o mesmo arquivo carregado como componente
referenciado — mesmo que a janela "standalone" dessa peça pareça fechada.
Feche a MONTAGEM que referencia o arquivo primeiro, depois feche/salve a
peça standalone. Depois de sobrescrever o arquivo no disco, reabra a
montagem — ela recarrega o componente do disco automaticamente, mas as
mates antigas que referenciavam a topologia antiga provavelmente ficam
inválidas e o componente aparece **suprimido**. Se `unsuppress_component`
falhar (confirmado: status 3, causa não diagnosticada), o caminho robusto é
`delete_component` + `insert_component` de novo na mesma posição, e refazer
as mates do zero contra a geometria nova.

## 9. `create_automotive_piston_assembly` não cria mate nenhum

Confirmado por leitura do código (2026-10-05), contagem no corpo inteiro da
função (195 linhas):

```
add_mate                   0      insert_component           5
add_advanced_mate          0      fix_component              0
AddMate                    0      set_component_transform    0
interference_check         0      validate_model             0
```

A ferramenta insere cinco componentes (pistão, pino, biela, bronzina, capa)
em offsets x/y/z calculados e **para**. Não existe restrição entre eles.
Consequências:

- **Não é montagem móvel**, apesar de a docstring antiga chamá-la de "the
  moving assembly": não há junta cinemática, nada ali gira ou desliza.
- **O encaixe depende só da origem de cada peça.** Se a biela não nasceu
  posicionada em relação à própria origem exatamente como o offset assume,
  ela aparece deslocada — e nada mede isso. É a causa direta do sintoma
  "pistão e biela nunca ficam com uma conexão boa".
- Nenhum `interference_check`, nenhuma verificação dimensional.

Trate o resultado como **modelo de referência posicionado**, não como junta.
Para junta de verdade, mateie à mão e verifique:

1. `concentric` pino ↔ furo do mancal, e `concentric` pino ↔ olhal da biela.
2. `add_advanced_mate("width")` entre as duas faces internas dos mancais para
   centrar o olhal no vão — é isso que trava o eixo axial que o item 2 deste
   arquivo explica ficar livre.
3. `get_component_transform` na biela e compare a posição axial com o centro
   do vão (calculado, não estimado) **antes** de `fix_component`.
4. `interference_check` no fim.

Cuidado adicional ao mateal à mão: `add_mate` seleciona face por coordenada
via `SelectByID2`, que **pica a partir da câmera** e não alcança face oculta
atrás do modelo (a própria docstring de `list_faces` registra que isso só
vale para peça convexa). O furo do mancal do pistão é interno à saia —
nenhuma orientação de câmera o alcança. Para faces internas, `add_mate` por
coordenada não serve; use o pino como intermediário (cujas faces são
externas) em vez de tentar mateal a biela direto no mancal.

## 10. `create_automotive_piston_assembly`: pino e biela nascem com eixo perpendicular ao mancal do pistão (90°), não só deslocados

Confirmado por medição ao vivo (2026-10-05), comparando `list_faces(surface_type="cylinder")`
de cada peça standalone (pistão, pino, biela) depois de `create_automotive_piston_assembly`:

- Furo do mancal do pistão: `axis=[1,0,0]` (eixo global X, transversal — correto para um
  pino de pistão real).
- Face externa do pino (`wrist_pin`), inserido sem rotação pelo `insert_component`:
  `axis=[0,0,-1]` (eixo local Z).
- Furo do olhal pequeno da biela: `axis=[0,0,1]` (eixo local Z).

Ou seja: pino e biela foram modelados internamente usando um eixo Z para o furo,
mas o pistão foi modelado com o mancal em X. Como `insert_component` só translada
(não gira) a peça, o resultado nativo tem os eixos **perpendiculares entre si**, não
apenas um gap de alinhamento — nenhuma mate concêntrica resolve isso sem antes girar
o componente. Isso é consistente com o sintoma relatado pelo usuário: "para criar o
pistão/biela funciona 100%, mas ao tentar posicionar para gerar movimento, perde a
posição e perde o desenho" — porque qualquer tentativa de ajuste fino parte de uma
base já desalinhada em rotação, não só em translação.

**Correção aplicada e verificada ao vivo:** `set_component_transform` com
`rotation_y=90` no pino e na biela resolve o desalinhamento de eixo (confirmado via
`get_component_transform`: a matriz resultante `[[0,0,1],[0,1,0],[-1,0,0]]` mapeia o
eixo local Z exatamente para o eixo global X do mancal). A translação precisa ser
recalculada com a convenção de linha do item 4 deste arquivo — **não** basta girar;
sem recompor a translação, o furo gira em torno da origem local da peça (que não
coincide com o centro do furo) e sai do lugar.

**Limite ainda não contornado:** mesmo depois de alinhar pino e biela por
`set_component_transform` (validado visualmente: vista `right` mostra o conjunto
pistão→pino→biela→(mancal) perfeitamente alinhado, sem gap), tentar formalizar esse
encaixe com uma mate real (`add_mate(concentric, ...)`) no furo do olhal da biela
falhou com `"No face/plane found at point2"` **mesmo usando o ponto exato devolvido
por `list_faces`**, transformado corretamente pela matriz de rotação. Causa provável:
uma vez que pino e biela já estão coaxiais (é exatamente o objetivo), o corpo sólido
do PINO ocupa fisicamente o mesmo eixo e bloqueia a câmera de alcançar a face interna
do furo da biela por trás dele — autoexclusão geométrica, não um erro de cálculo de
ponto. Isso significa que, com o toolset atual (seleção por coordenada/câmera via
`SelectByID2`), **não dá para criar uma mate concêntrica real e resolvida pelo
solver** nessa junta depois de alinhada — só dá para posicionar por transformação
absoluta (`set_component_transform` + `fix_component`), o que é posição estática
correta, mas não é uma junta cinemática (o solver do SolidWorks não está envolvido,
então nada "sabe" que a peça deve deslizar/girar dentro de limites).

**Caminho correto pesquisado (ver sessão de 2026-10-05, resposta ao usuário sobre
"como a mate vai saber as posições"):** a forma robusta de o SolidWorks resolver
posição/encaixe é via geometria de referência estável — **Mate References**
(`InsertMateReference2` na API) marcadas em cada peça no momento da modelagem, ou
**Coordinate System Mates** entre sistemas de coordenadas nomeados e coincidentes.
Qualquer uma das duas elimina o cálculo manual de matriz de rotação e o problema de
autoexclusão por câmera, porque a mate deixa de depender de picar um ponto visível e
passa a referenciar uma entidade nomeada. A correção de causa raiz fica em
`create_automotive_piston_assembly` no `server.py`: ou (a) modelar o furo do mancal
do pistão, o pino e o olhal da biela todos no mesmo eixo/plano de sketch desde a
criação (eliminando a necessidade de qualquer rotação pós-inserção), ou (b) inserir
um `Coordinate System` em cada componente no ponto de articulação e mateá-los entre
si via coordinate-system mate em vez de `add_mate` por coordenada de face.
