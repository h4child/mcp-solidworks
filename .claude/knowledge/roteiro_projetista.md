# Roteiro completo — de um pedido/referência até a peça pronta

Este é o fluxo pra "faz qualquer peça", amarrando os outros arquivos de
`knowledge/` em ordem de decisão. Para peças a partir de uma referência real
(foto, catálogo, desenho técnico), use também o prompt MCP
`design_from_reference` — ele formaliza esse mesmo roteiro como um prompt
reutilizável do protocolo.

## Passo 1 — Entender o pedido antes de desenhar

Extraia (do usuário ou da referência):

- **Função:** o que essa peça faz na montagem/sistema? Isso guia tolerância
  (acopla com algo? → `gdt.md`/`tolerancias_e_ajustes.md`) e material
  (estrutural? estético? exposto a química?).
- **Dimensões críticas conhecidas** — mesmo que aproximadas. Se não tiver
  nenhuma, pergunte ou estime a partir de proporção com algo conhecido na
  referência.
- **Processo de fabricação pretendido** (ou infira pela forma: chapa fina
  dobrada → chapa metálica; perfil tubular repetido → weldment; forma
  orgânica/complexa → usinado ou moldado). Ver `processos_de_fabricacao.md`.
- **Material** — ver `materiais.md`, regra de decisão por exposição/peso/carga.

Escreva isso em texto, de verdade, antes da primeira chamada de ferramenta —
é o passo "PLANEJAR" do `CLAUDE.md`.

## Passo 2 — Isolar

`create_new_part` (ou `create_new_assembly` se a referência já é um
conjunto de peças). Nunca reaproveite um documento que o usuário tinha
aberto, a menos que ele peça explicitamente.

## Passo 3 — Decidir a árvore de features

Mapeie a peça em features na ordem que o SolidWorks realmente constrói:
sketch base → extrude/revolve/sweep/loft (o que define o volume principal) →
features secundárias (furos, chanfros, filetes, padrões) → acabamento
(aparência, se relevante). Decida aqui, não improvisando feature a feature:

- Peça com seção constante extrudada → `extrude_sketch`/`cut_extrude`.
- Peça de revolução (eixo, polia, flange redondo) → `revolve_sketch` com
  `draw_centerline` como eixo.
- Peça com seção variando ao longo de um caminho → `sweep_sketch` ou
  `loft_sketches`.
- Chapa dobrada → ver `chapa_metalica.md`, começa com `create_base_flange`,
  não com `extrude_sketch`.
- Estrutura de perfis → ver `soldas_e_perfis_estruturais.md`, começa com
  `create_3d_sketch` + `create_weldment_profile`.

## Passo 4 — Construir incremental, validando

Grupos pequenos de 2-3 features, depois `measure_body`/`validate_model` (ver
`verificacao_e_qa.md` passos 1-2). Aplicar furos/chanfros/filetes com os
valores das tabelas certas:

- Furo de parafuso/rosca → `elementos_de_maquina.md`.
- Raio de canto (usinagem) → `processos_de_fabricacao.md`.
- Raio de dobra (chapa) → `chapa_metalica.md`.

## Passo 5 — Material e propriedades

`set_material` (ver `materiais.md` pra escolher). Em seguida
`set_custom_property` pro que for relevante a jusante — código, descrição, e
qualquer propriedade que um sistema downstream (como o backend Alfa Detail
AI) vá ler depois. Confira: esse backend lê `Material`/`PartNumber`/
`Description` por padrão, mas também aceita o material nativo do
`set_material` como fallback se não houver propriedade customizada explícita.

## Passo 6 — Inspecionar visualmente e comparar

`capture_standard_views`, compare com a referência (ver
`verificacao_e_qa.md` passo 3). Se não bateu, volte ao passo 4 e corrija a
feature responsável — não tente compensar com uma feature posterior.

## Passo 7 — Montagem (se for o caso)

`insert_component` pras peças que já existem, posicione com
`set_component_transform` ou mates (`add_mate`/`add_advanced_mate` — EXP,
ver status) ou `fix_component` pra referência fixa. `interference_check`
antes de considerar pronto (ver `verificacao_e_qa.md` passo 4).

## Passo 8 — QA final

Rode o checklist completo de `verificacao_e_qa.md` (reconstrução sem erro,
massa bate, inspeção visual, fabricabilidade, e cálculo analítico se foi
pedida confirmação de resistência). Só depois disso, `save_document` e
reporte ao usuário as dimensões/massa finais medidas comparadas com o
pedido original.

## Passo 9 — Responder ao usuário

Diga o que foi feito, as decisões de material/tolerância/processo tomadas e
**por quê** (uma frase basta — "usei AISI 304 porque é exposto a umidade"),
e os números medidos (massa, dimensões principais) comparados ao alvo. Se
alguma ferramenta usada é EXP, mencione isso em vez de deixar o usuário achar
que é tão confiável quanto o resto.
