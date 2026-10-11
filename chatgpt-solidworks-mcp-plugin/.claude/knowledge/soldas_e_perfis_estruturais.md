# Estruturas soldadas (weldments) e perfis

Ferramentas: `create_3d_sketch` + `draw_line_3d` (esqueleto da estrutura) →
`create_weldment_profile` (aplica o perfil ao longo das linhas) →
`trim_extend_structural` (acerta os encontros) → `add_gusset` (reforço) →
`add_weld_symbol` (anotação no desenho). Todas validadas OK (ver README),
exceto `add_end_cap`, que tem defeito confirmado nesta versão do SolidWorks —
não dependa dela, fabricação resolve tampa de ponta com chapa cortada e
soldada separadamente se for mesmo necessário.

## Escolha de perfil por tipo de carga

| Situação | Perfil recomendado | Por quê |
| --- | --- | --- |
| Carga em várias direções, estrutura de chão/plataforma | Tubo quadrado/retangular | Boa rigidez à torção em qualquer direção, fácil de soldar em esquadro, boa estética. |
| Viga com carga predominante numa direção (flexão) | Perfil I ou U | Momento de inércia concentrado numa direção — mais eficiente em peso pra flexão pura. |
| Contraventamento, diagonal, treliça leve | Cantoneira (perfil L) | Barato, fácil de furar/parafusar, suficiente pra tração/compressão axial. |
| Corrimão, guarda-corpo | Tubo redondo | Ergonomia (segurar com a mão) + estética, carga é leve. |

## Dimensionamento rápido (regra de proporção, não substitui cálculo estrutural)

Pra um primeiro dimensionamento de tubo quadrado numa estrutura leve-a-média
(plataforma industrial, suporte, carrinho): altura da seção ≈ **vão livre /
20 a / 25** pra manter deflexão visualmente aceitável sob carga distribuída
moderada. Vão de 2 m → seção de ~80-100 mm. Isso é ponto de partida pra
modelar, não dimensionamento certificado — se o usuário pedir confirmação de
resistência estrutural real, calcule tensão de flexão (`σ = M·c/I`) com a
carga e o perfil específico, ou recomende revisão de um engenheiro
estrutural pra aplicação crítica.

## Espessura de parede do tubo

Tubo estrutural comum (ISO/catálogo): espessura ≈ **1/15 a 1/20 do lado da
seção** pra tubo quadrado de uso geral. Tubo 40×40 → parede ~2 a 2.5 mm; tubo
80×80 → parede ~3 a 4 mm. Parede mais fina que isso tende a amassar/flambar
localmente nas conexões soldadas.

## Gussets (reforço de canto)

`add_gusset` funciona bem no caso clássico: uma face de viga + uma face de
chapa/outra viga que se tocam numa aresta real (ver README — canto chanfrado
de peça única é mais ambíguo). Dimensione o gusset com cateto ≈ **a altura da
seção da viga principal**, espessura ≈ a parede do tubo ou um pouco mais.

## Símbolos de solda — os mais comuns

| Símbolo (texto) | Tipo | Quando usar |
| --- | --- | --- |
| Filete | Solda de canto/sobreposição | A maioria das juntas de tubo estrutural — rápida, não precisa de preparação de borda. |
| Entalhe/ranhura (groove) | Solda de topo, penetração | Juntas que precisam de resistência igual ao material base (chapa topo-a-topo). |
| Tampão (plug) | Preenche um furo pra unir chapas sobrepostas | Quando não dá acesso pra filete contínuo. |

`add_weld_symbol` aplica o símbolo numa aresta da vista inserida no desenho —
valide primeiro que `insert_drawing_view` trouxe a vista isométrica certa
antes de tentar posicionar o símbolo.

## Sequência prática no MCP

1. `create_3d_sketch` + `draw_line_3d` pro esqueleto (linhas de centro de cada
   membro).
2. `create_weldment_profile` com padrão (ISO) e perfil/tamanho da tabela de
   dimensionamento acima.
3. `trim_extend_structural` em cada encontro de membros (T, L, X).
4. `add_gusset` nos encontros que recebem carga concentrada ou vão ficar
   expostos a vibração/fadiga.
5. `measure_body` pra conferir massa total da estrutura (confirma que o
   material e a seção fazem sentido pro peso esperado) antes de considerar a
   estrutura pronta.
