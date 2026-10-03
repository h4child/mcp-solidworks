# Elementos de máquina padronizados

Nunca invente dimensão de parafuso, rolamento ou chaveta — são padronizados,
e uma peça que não bate com o padrão não encontra componente comprado pra
montar. Use os valores abaixo; modele roscas via `hole_wizard` (tipo
"tapped") ou `add_cosmetic_thread` + anotação, nunca hélice 3D real (ver
`CLAUDE.md`, roscas não são suportadas de verdade pela API).

## Parafusos métricos ISO — passo grosso (o default, salvo indicação)

| Designação | Passo (mm) | Furo roscado (broca, mm) | Furo passante normal (mm) | Chave (mm) |
| --- | --- | --- | --- | --- |
| M3 | 0.5 | 2.5 | 3.4 | 5.5 (sextavado) / 2.5 (allen) |
| M4 | 0.7 | 3.3 | 4.5 | 7 / 3 |
| M5 | 0.8 | 4.2 | 5.5 | 8 / 4 |
| M6 | 1.0 | 5.0 | 6.6 | 10 / 5 |
| M8 | 1.25 | 6.8 | 9.0 | 13 / 6 |
| M10 | 1.5 | 8.5 | 11.0 | 17 / 8 |
| M12 | 1.75 | 10.2 | 13.5 | 19 / 10 |
| M16 | 2.0 | 14.0 | 17.5 | 24 / 14 |

Profundidade mínima de rosca num furo cego, em material de resistência
equivalente ao parafuso: **1.5× o diâmetro nominal** (M8 → 12 mm de rosca
útil mínima). Em material mais mole que o parafuso (alumínio com parafuso de
aço), use 2× a 2.5×.

## Rolamentos rígidos de esferas — série 6 00/6 200/6 300 (mais comuns)

| Designação | Furo (mm) | Diâmetro externo (mm) | Largura (mm) |
| --- | --- | --- | --- |
| 608 | 8 | 22 | 7 |
| 6000 | 10 | 26 | 8 |
| 6001 | 12 | 28 | 8 |
| 6002 | 15 | 32 | 9 |
| 6003 | 17 | 35 | 10 |
| 6200 | 10 | 30 | 9 |
| 6201 | 12 | 32 | 10 |
| 6202 | 15 | 35 | 11 |
| 6203 | 17 | 40 | 12 |
| 6204 | 20 | 47 | 14 |
| 6300 | 10 | 35 | 11 |
| 6301 | 12 | 37 | 12 |
| 6302 | 15 | 42 | 13 |
| 6303 | 17 | 47 | 14 |

Série 6 00 = leve/miniatura, 6 200 = leve padrão, 6 300 = média (mais robusta
pra mesmo furo). Alojamento do rolamento: ajuste `H7` no alojamento (furo),
eixo em `k6`/`j6` se o eixo girar junto com o anel interno (caso mais comum).

## Chavetas paralelas (DIN 6885 / ISO 773) por diâmetro de eixo

| Diâmetro do eixo (mm) | Chaveta (largura × altura, mm) | Profundidade no eixo (mm) |
| --- | --- | --- |
| 6 – 8 | 2 × 2 | 1.2 |
| 8 – 10 | 3 × 3 | 1.8 |
| 10 – 12 | 4 × 4 | 2.5 |
| 12 – 17 | 5 × 5 | 3.0 |
| 17 – 22 | 6 × 6 | 3.5 |
| 22 – 30 | 8 × 7 | 4.0 |
| 30 – 38 | 10 × 8 | 5.0 |
| 38 – 44 | 12 × 8 | 5.0 |

Comprimento da chaveta: normalmente 1.2× a 1.5× o diâmetro do eixo, limitado
pelo comprimento do cubo que ela trava.

## Molas helicoidais de compressão — regra de proporção (sem catálogo)

Sem catálogo específico, dimensione por proporção: diâmetro do arame ≈
diâmetro externo da mola / 8 a /10; altura livre ≈ 3 a 4× o diâmetro externo;
número de espiras ativas entre 4 e 10 pra comportamento de mola previsível
(menos que isso fica rígido/não-linear demais). Para carga real, calcule com
a fórmula de rigidez `k = G·d⁴ / (8·D³·n)` (G = módulo de cisalhamento do
material, d = diâmetro do arame, D = diâmetro médio da mola, n = espiras
ativas) em vez de só proporção.

## Como isso vira modelo no SolidWorks

- Furo roscado → `hole_wizard(hole_type="tapped", size=8, ...)` (size = o número
  do M, ex. 8 para M8 — `hole_wizard` resolve o passo ISO coarse automaticamente).
- Furo passante → `hole_wizard(hole_type="simple", size=<Ø da tabela acima>)` ou `draw_circle` + `cut_extrude`
  com o diâmetro de folga da tabela acima.
- Rasgo de chaveta → `draw_rectangle` no plano certo + `cut_extrude`, usando
  largura/profundidade da tabela.
- Alojamento de rolamento → `draw_circle` com o Ø externo da tabela + `extrude`/
  `cut_extrude` conforme for furo passante ou rebaixo.
