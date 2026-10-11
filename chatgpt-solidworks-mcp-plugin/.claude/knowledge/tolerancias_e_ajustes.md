# Tolerâncias e ajustes

O MCP não tem um jeito de "aplicar tolerância" em `add_sketch_dimension` além
do valor nominal — então tolerância vira anotação no desenho (`add_drawing_dimension`,
em grande parte EXP hoje) ou decisão documentada em `set_custom_property`/na
resposta ao usuário. Mesmo sem aplicar no modelo, **decidir** a tolerância certa
muda a geometria (folga de furo, ajuste entre peças) — isso sim o modelo precisa
refletir.

## Tolerância geral (quando ninguém pediu uma tolerância específica)

Use ISO 2768-1, classe **m (média)** como default sensato pra peça usinada
genérica; **f (fina)** se for ajuste/acoplamento; **c (grosseira)** pra chapa
dobrada/solda sem função de precisão.

| Faixa de dimensão (mm) | f (fina) | m (média) | c (grosseira) |
| --- | --- | --- | --- |
| 0.5 – 3 | ±0.05 | ±0.1 | ±0.2 |
| 3 – 6 | ±0.05 | ±0.1 | ±0.3 |
| 6 – 30 | ±0.1 | ±0.2 | ±0.5 |
| 30 – 120 | ±0.15 | ±0.3 | ±0.8 |
| 120 – 400 | ±0.2 | ±0.5 | ±1.2 |
| 400 – 1000 | ±0.3 | ±0.8 | ±2.0 |

## Sistema de ajustes ISO (furo-base, o mais comum)

Furo sempre `H` (limite inferior = nominal); eixo varia pra definir o tipo de
ajuste. Notação: `H7/g6` = furo H7, eixo g6.

| Ajuste | Tipo | Folga/interferência típica (eixo Ø20) | Quando usar |
| --- | --- | --- | --- |
| `H7/g6` | Deslizante com folga | +7 a +28 µm | Peças que deslizam ou giram livre, mancal não-crítico, pino-guia removível. |
| `H7/h6` | Deslizante justo (folga zero nominal) | 0 a +21 µm | Peça que se move à mão mas sem jogo perceptível — tampas, localizadores. |
| `H7/k6` | Transição (levemente apertado) | -2 a +15 µm | Montagem fixa mas desmontável com força leve — bucha, rolamento em alojamento. |
| `H7/p6` | Interferência leve | -18 a -2 µm | Encaixe prensado leve — pino fixo, bucha que não deve girar. |
| `H7/s6` | Interferência média | -35 a -18 µm | Prensado permanente — precisa de prensa ou dilatação térmica pra montar. |

Regra prática: **eixo gira ou desliza → `g6`/`h6`. Eixo fica fixo e
desmontável → `k6`. Fixo pra sempre → `p6`/`s6`.**

## Furos pra parafuso passante (folga, não ajuste de precisão)

| Diâmetro do parafuso | Furo de folga normal | Furo de folga larga |
| --- | --- | --- |
| M3 | Ø3.4 | Ø3.6 |
| M4 | Ø4.5 | Ø4.8 |
| M5 | Ø5.5 | Ø5.8 |
| M6 | Ø6.6 | Ø7.0 |
| M8 | Ø9.0 | Ø10.0 |
| M10 | Ø11.0 | Ø12.0 |
| M12 | Ø13.5 | Ø14.5 |

Use `hole_wizard` com esses diâmetros pra furo passante; combine com
`knowledge/elementos_de_maquina.md` pro furo roscado correspondente na peça
que recebe o parafuso.

## Acabamento superficial (Ra) — referência rápida

| Processo | Ra típico (µm) |
| --- | --- |
| Corte a laser/plasma | 3.2 – 12.5 |
| Fresamento/torneamento padrão | 1.6 – 6.3 |
| Retificado | 0.2 – 0.8 |
| Fundido bruto | 6.3 – 25 |

Não especifique Ra mais fino do que o processo de fabricação exige — custo
sobe rápido abaixo de 1.6 µm. Ver `processos_de_fabricacao.md`.
