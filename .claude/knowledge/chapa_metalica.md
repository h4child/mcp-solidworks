# Chapa metálica — regras de projeto

Ferramentas: `create_base_flange`, `add_sheet_metal_bend`, `add_sheet_metal_edge_flange`,
`flatten_sheet_metal`, `export_flat_pattern_dxf` — todas validadas OK no
`solidworks://tool-status` (ver README). O que muda o resultado é respeitar
as regras físicas da dobra abaixo; o SolidWorks não impede você de pedir uma
dobra impossível de fabricar.

## Raio mínimo de dobra por espessura

Regra prática: raio de dobra interno mínimo ≈ **1× a espessura do material**
pra aço doce, **1.5× a 2×** pra alumínio (menos dúctil), **2× a 3×** pra inox
(mais encruamento no dobramento).

| Espessura (mm) | Raio interno mínimo — aço doce | Raio interno mínimo — alumínio |
| --- | --- | --- |
| 0.8 | 0.8 | 1.5 |
| 1.0 | 1.0 | 2.0 |
| 1.5 | 1.5 | 2.5 |
| 2.0 | 2.0 | 3.5 |
| 3.0 | 3.0 | 5.0 |

## K-factor (posição da fibra neutra na dobra)

Controla o cálculo da planificação. Sem dado do fabricante, use **K = 0.44**
como default de uso geral (razoável pra aço carbono em dobra a 90°, raio
próximo da espessura). Alumínio tende a K levemente maior (~0.47-0.5), inox
levemente menor por encruar mais rápido no dobramento. `create_base_flange`/
`flatten_sheet_metal` usam o K-factor configurado no documento — se o
resultado planificado parecer "comprido" ou "curto" demais comparado ao
desenho de referência, o K-factor é o primeiro suspeito.

## Flange mínimo (distância da dobra até a borda livre)

Flange mais curto que **4× a espessura + o raio de dobra** tende a deformar
na prensa (o material não tem onde "segurar" durante a dobra). Para chapa de
2 mm com raio de 2 mm: flange mínimo ≈ 4×2 + 2 = 10 mm — projete flanges
maiores que isso em `add_sheet_metal_edge_flange`.

## Relevo de dobra (bend relief) e distância de furo até a dobra

- Dobra que termina no meio de uma borda (não atravessa a chapa toda) precisa
  de um corte de alívio na borda, senão a chapa rasga na ponta da dobra.
  Largura do alívio ≈ a espessura do material, profundidade ≈ espessura + raio.
- Furos: mantenha pelo menos **2.5× a espessura** de distância entre o furo e
  o início da dobra, senão o furo distorce durante o dobramento. Pra chapa de
  3 mm, isso é ~7.5 mm mínimo.

## Sequência prática no MCP

1. `create_sketch` no plano base → `draw_rectangle`/perfil → `create_base_flange`
   (define espessura e primeira dobra de uma vez).
2. Para cada aba adicional: selecionar a aresta → `add_sheet_metal_edge_flange`
   com o comprimento calculado acima.
3. Furos numa aba já dobrada (face criada por `add_sheet_metal_edge_flange`):
   **ver a seção "Furo em face de EdgeFlange" abaixo antes de tentar** —
   `hole_wizard` e `create_sketch_on_face` não funcionam de forma confiável
   nessa face especificamente.
4. `flatten_sheet_metal` pra conferir a planificação antes de fechar o
   projeto — se a planificação falhar ou ficar com geometria estranha, quase
   sempre é K-factor ou raio de dobra inconsistente com a espessura.
5. `export_flat_pattern_dxf` é a entrega real pra corte a laser/punção — o
   3D dobrado é pra visualização/montagem, quem fabrica a chapa usa o DXF.

## ⚠️ Furo em face de EdgeFlange — limitação confirmada, não tente de novo sem ler isto

Testado ao vivo e esgotado em sete iterações de correção (2026-10-04, ver
`bug_report_solidworks_mcp.txt` e o histórico de commits do `server.py` a
partir de `92964ac`). **Não existe, hoje, um caminho confiável pelas
ferramentas deste servidor para furar a face plana de uma aba criada por
`add_sheet_metal_edge_flange` de um jeito que sobreviva tanto ao estado
dobrado quanto ao desdobrado.** As três rotas tentadas, e por que cada uma
falha:

1. **`hole_wizard` direto na face dobrada** — falha sempre com "Hole Wizard
   failed on face... Ensure the face is flat", mesmo com coordenadas
   corretas e confirmadas por `list_faces`. Causa raiz não identificada no
   lado do servidor (a seleção da face funciona; é o próprio `HoleWizard4`
   do SolidWorks que recusa essa face). Reproduzido de forma determinística.

2. **`create_sketch_on_face` + `draw_circle` + `cut_extrude` na face dobrada**
   — a face externa da aba é literalmente o mesmo plano do sketch que
   *define* a dobra (o sketch interno do `add_sheet_metal_edge_flange`,
   aparece como `Esboço9`/`Esboço10` etc. na árvore). `InsertSketch2` nessa
   face **reabre esse sketch existente para edição** em vez de criar um novo
   independente — o círculo que você desenha entra misturado com a geometria
   que define a dobra, e o corte subsequente falha ("Check that its profile
   is closed") porque o sketch agora tem geometria conflitante. Isso não é
   bug de seleção corrigível em Python: o próprio SolidWorks está decidindo
   reabrir o sketch errado. O servidor (a partir de 5.8.7) detecta esse caso
   e falha alto e claro em vez de corromper o sketch em silêncio — mas não
   há, ainda, um jeito de fazer a operação em si funcionar.

3. **Cortar com a peça DESDOBRADA** (`flatten_sheet_metal` → `create_sketch_on_face`
   na face plana resultante → `cut_extrude` → `flatten_sheet_metal` de volta)
   — essa parte funciona sem erro (`validate_model` retorna `valid: true`
   nos dois estados). **Mas é uma armadilha**: o corte feito dessa forma vira
   um feature preso à configuração de Flat Pattern (`suppressed: true` assim
   que você dobra de volta) — a peça dobrada final **não tem o furo**. O
   "sucesso" do `validate_model` é enganoso: não há erro porque não há
   furo nenhum na peça real, só no desenho planificado.

**Se a tarefa pedir furo numa aba dobrada:** avise o usuário da limitação
acima em vez de tentar as três rotas de novo. Alternativas reais:
- Furar a chapa **antes** de dobrar (no sketch plano original, na posição
  equivalente desdobrada) — o furo acompanha a dobra naturalmente. Só
  funciona se a posição do furo não depender de geometria criada pela
  própria dobra.
- Usar um plano de referência nomeado (`create_reference_plane` + `create_sketch`,
  não `create_sketch_on_face`) para cortar — funciona de forma confiável,
  mas o corte fica fixo no espaço global, não vinculado à face da aba, o que
  quebra o Flat Pattern se a peça precisar desdobrar depois (mesmo problema
  que o "sucesso" do item 3 acima, por um caminho diferente).
- Fazer o furo manualmente no SolidWorks depois que a automação terminar o
  resto da peça.
