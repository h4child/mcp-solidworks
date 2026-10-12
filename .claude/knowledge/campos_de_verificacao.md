# Campos de verificação — como ler o que o servidor mediu

Este servidor **mede o próprio resultado**. Quase toda ferramenta que posiciona
ou dimensiona algo devolve, além do que você pediu, o que o SolidWorks de fato
fez. Esses campos existem porque a classe de erro mais cara aqui não é a
chamada que falha — é a que **tem sucesso no lugar errado**.

Leia isto antes de concluir que um passo deu certo.

## 1. O contrato dos campos

| campo | significado | o que fazer |
| --- | --- | --- |
| `requested_*`, e os campos antigos (`position`, `width`, `depth`, `radius`…) | **o que você pediu.** Não é prova de nada | nunca cite como confirmação |
| `actual_*` | **o que o SolidWorks reporta.** Esta é a medição | compare com a intenção |
| `deviation` | diferença, com `within_tolerance` | se fora, corrija a feature responsável |
| `verified: true` | mediu **e** bateu | pode seguir |
| `verified: false` | mediu e **não** bateu | pare e corrija |
| `verified: null` | **não foi possível verificar** — veja `verification_skipped` | não é sucesso nem falha; decida se precisa conferir de outra forma |
| `warnings` contendo `UNVERIFIED` | a operação aconteceu, a **conferência** não | confirme por outro caminho antes de construir em cima |

`false` e `null` em `verified` são coisas diferentes. Tratar `null` como sucesso
é reintroduzir exatamente o problema que esses campos resolvem.

### Campos específicos

- **`snapped: true`** (ferramentas `draw_*`) — a geometria foi criada e o
  SolidWorks a **deslocou** para geometria vizinha. O perfil não é o que você
  pediu. Apague e redesenhe; não compense depois.
- **`zoom_retry: true`** (`draw_*`) — a primeira tentativa foi recusada e
  funcionou com zoom na área. Não é erro, mas avisa que você está desenhando
  pequeno numa peça grande, e **a câmera ficou aproximada**.
- **`is_construction: false`** (`draw_centerline`) — não é geometria de
  construção, então `revolve_sketch` não aceita como eixo.
- **`moved.distance`** (mates) — quanto o solve arrastou aquele componente. Se
  a peça que você só queria *acoplar* andou dezenas de milímetros, a mate está
  resolvendo em lugar diferente do imaginado.
- **`components_moved`** (`create_motion_study`) — quem saiu do lugar ao criar
  ou ativar o estudo, pior primeiro. `pose_preserved` responde direto.
- **`measurement_method`** (dimensões de feature) — por qual rota o valor foi
  lido (`GetDefinition().X` ou `Parameter('D1@...')`). Serve para rastrear um
  número suspeito em vez de adivinhar.
- **`fixed: true`** (`insert_component`) — a peça está **ancorada**. Ver
  armadilha 3.
- **`geometry_check.angle_degrees`** (`add_mate`) — ângulo real entre as
  direções de referência das duas faces, em coordenadas da montagem. 0 ou 180 =
  paralelo/antiparalelo, 90 = perpendicular. Qualquer outro valor numa junta
  que deveria ser rente significa **peça torta** — e agora é número, não
  impressão de vista isométrica.

## 2. As cinco armadilhas confirmadas

### 2.1. O snap de esboço é em espaço de TELA

Toda chamada `Create*` do `ISketchManager` passa pelo motor de inferência, cujo
raio de snap é uma distância **em pixels**. Medido ao vivo numa peça de
1,74 × 1,80 × 1,50 m:

| chamada | zoom | resultado |
| --- | --- | --- |
| `draw_rectangle(-20,-10,20,10)` | enquadrando a peça | **recusado** (`null`) |
| `draw_line(0,0,30,0)` | o mesmo | funcionou |
| `draw_rectangle(-200,200,200,600)` (400×400) | o mesmo | funcionou |
| o mesmo 40×20, após `zoom_to_area` | aproximado | funcionou |

`CreateLine` não passa por esse caminho — é por isso que o bug parece aleatório.
A recusa é a falha ruidosa; a silenciosa é o snap **deslocar** um ponto e a
chamada ter sucesso.

**Faça:** `zoom_to_area` na região antes de desenhar feature pequena em peça
grande; e `list_sketch_entities` antes de extrudar.

### 2.2. Alinhamento de mate — por que estático saía bem e movimento saía torto

O segundo parâmetro do `AddMate5` é `swMateAlign_e`. Estava fixo em `ALIGNED`.
Para duas faces feitas para se encarar, `ALIGNED` é a solução errada, e o
SolidWorks **não reporta erro**, porque é matematicamente válida.

Corrigido em duas etapas, e a primeira foi incompleta:

| o que se faz | ferramenta | corrigido em |
| --- | --- | --- |
| montagem estática | `add_mate` | v5.14.0 |
| **mecanismo** (distance, angle, **gear**, width, symmetric) | `add_advanced_mate` | **v5.16.0** |

Era literalmente isso: duas ferramentas, uma corrigida e outra não. Hoje ambas
têm `align` com default `'closest'`.

**Faça:** se `'closest'` resolver pro lado errado, chame de novo com
`'aligned'` ou `'anti_aligned'` — é um parâmetro, não um rebuild.

### 2.3. O primeiro componente da montagem é ancorado

O SolidWorks fixa o primeiro componente inserido. **Peça fixa nunca se move:**
toda mate contra ela é resolvida movendo *a outra*. Sem saber disso, você mata
a peça principal esperando que ela se alinhe, vê a outra se mover, e conclui
que a principal "não muda de posição".

**Faça:** leia `fixed` no retorno do `insert_component`. Se a peça principal
deve ser posicionada pelas mates, `float_component` nela primeiro.

### 2.4. O SolidWorks reduz dimensão que não cabe, sem erro

Raio de arredondamento maior do que a geometria permite sai menor. Casca mais
grossa que a parede sai mais fina. A feature existe, a chamada teve sucesso, e
a peça não tem a dimensão que ninguém conferiu.

**Faça:** compare `actual_radius` / `actual_thickness` / `actual_depth` /
`actual_distance` com o pedido. Estão no retorno.

### 2.5. Pose de mecanismo não é guardada por mate nenhuma

Montagem estática guarda a posição de graça — tudo fixo ou totalmente matado, e
um rebuild devolve cada peça ao mesmo lugar. Mecanismo não: as peças móveis são
sub-restringidas **de propósito**, e esse grau de liberdade livre **é** o
movimento. A pose atual é uma de infinitas válidas, e o próximo drag, rebuild
ou motion study a substitui sem registrar qual era.

Não tente resolver com mate nem com `fix_component` — travar mata o mecanismo.

**Faça:** `capture_assembly_pose(label=..., filepath=...)` quando a pose
estiver boa, e `restore_assembly_pose` para voltar. Declare as peças móveis em
`verify_assembly_positions(moving_components=[...])`, senão cada uma dispara
`M01`, que é exatamente o que uma peça móvel é.

## 3. A regra que resume tudo

**Corrija a feature responsável; não compense num passo posterior.** Um desvio
compensado adiante some da vista e volta na montagem, onde é muito mais caro
achar.

## 4. O que ainda NÃO foi validado ao vivo

Honestidade sobre o próprio ferramental, para não ser citado como garantia:

- **`align='closest'`** nunca foi testado numa junta real — vem da documentação
  do `swMateAlign_e`. Se uma junta sair invertida, é o primeiro suspeito.
- **As rotas de leitura de dimensão de feature** (`GetDefinition` e o parâmetro
  nomeado) não foram confirmadas ao vivo. Por isso `measurement_method` existe:
  se ele vier `null` e o valor vier `UNVERIFIED`, foi essa leitura que não
  respondeu — a feature foi criada de qualquer forma.
- **O deslocamento silencioso do snap** (2.1) é inferido do mecanismo de
  inferência, não medido. O que foi medido é a recusa ruidosa.
- **`draw_polygon`** reporta `actual_circumradius` e `actual_apothem` mas não os
  compara com `radius`: qual dos dois o SolidWorks trata como o argumento
  depende da flag `inscribed`, e esse mapeamento não foi confirmado.

Toda leitura de volta é **não-fatal**: se a medição falhar, o campo vem
`UNVERIFIED` com aviso — nunca quebra a modelagem.
