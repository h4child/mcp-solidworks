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
3. Furos **depois** das dobras estarem decididas, respeitando a distância
   mínima dobra-furo.
4. `flatten_sheet_metal` pra conferir a planificação antes de fechar o
   projeto — se a planificação falhar ou ficar com geometria estranha, quase
   sempre é K-factor ou raio de dobra inconsistente com a espessura.
5. `export_flat_pattern_dxf` é a entrega real pra corte a laser/punção — o
   3D dobrado é pra visualização/montagem, quem fabrica a chapa usa o DXF.
