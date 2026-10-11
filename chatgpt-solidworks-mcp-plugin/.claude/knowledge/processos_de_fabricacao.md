# Processos de fabricação — projetar pensando em como a peça é feita (DFM)

Decida o processo **antes** de desenhar a primeira feature — ele determina
que tipo de geometria é barata/cara, quais raios são obrigatórios, e qual
tolerância é razoável pedir (ver `tolerancias_e_ajustes.md`).

## Usinagem CNC (fresamento/torneamento)

- **Cantos internos nunca são perfeitamente vivos** — toda cavidade usinada
  com fresa de topo deixa um raio de canto igual ao raio da ferramenta.
  Especifique `fillet_edges` com raio ≥ 1.5 mm em cantos internos de bolsões
  (raio menor que isso exige ferramenta frágil e cara). Cantos vivos só em
  geometria externa ou faces que o torneamento cria naturalmente.
- **Furos profundos são caros:** razão profundidade/diâmetro acima de 4:1
  precisa de broca especial e perde precisão — evite ou avise o usuário.
- **Paredes finas usinadas vibram:** abaixo de ~0.8 mm em metal (depende do
  tamanho da face) a peça flete sob a força de corte — prefira 1.5 mm+ se não
  houver razão específica pra ir mais fino.
- Tolerância geral confortável pra CNC: classe `m` do ISO 2768 sem custo
  extra; classe `f` ou ajustes H7 só nas features que realmente acoplam.

## Corte a laser / plasma + dobra (chapa metálica)

- Ver `chapa_metalica.md` pras regras de dobra. Além disso: furos muito perto
  da borda de corte (< 1× a espessura) distorcem; diâmetro mínimo de furo a
  laser ≈ a espessura da chapa (furo menor que a espessura fecha/queima).
- Texto/gravação fina é mais barato a laser (é o mesmo processo de corte,
  sem ferramenta extra) — considere em vez de relevo usinado quando só
  precisa de identificação.

## Dobra sem ferramenta especial vs. com matriz dedicada

Dobras em ângulo reto com raio próximo da espessura usam ferramenta padrão
de prensa dobradeira — barato. Ângulos incomuns ou raios muito maiores que a
espessura podem exigir matriz específica — avise se a geometria pedir isso
sem necessidade funcional clara.

## Peças soldadas (ver também `soldas_e_perfis_estruturais.md`)

- Deixe folga de ~0.5-1 mm entre peças que serão soldadas topo-a-topo — ajuste
  "perfeito" (zero folga) dificulta a penetração da solda.
- Preveja espaço de acesso pra tocha/eletrodo em todo cordão de solda — um
  canto fechado por outra peça não dá pra soldar depois da montagem.

## Injeção plástica (se o usuário pedir peça plástica "de produção", não protótipo)

- **Ângulo de saída (draft) obrigatório** em toda face paralela à direção de
  desmoldagem — mínimo 1-2° (mais em textura). Sem isso a peça trava no molde.
- **Espessura de parede uniforme** — variação brusca de espessura causa
  rechupe (marca de afundamento) e empenamento. Se precisar de uma seção mais
  robusta, use nervura (costela) fina (≈ 50-60% da espessura da parede
  principal) em vez de engrossar a parede toda.
- Raio mínimo em todo canto, interno e externo — canto vivo concentra tensão
  e é onde a peça trinca primeiro.

## Fundição (se vier à tona pra peça grande/complexa em ferro/alumínio)

- Precisa de ângulo de saída como injeção (2-3°, geralmente maior que
  plástico).
- Seções muito espessas formam vazio de contração no resfriamento — prefira
  parede uniforme, com nervuras pra rigidez em vez de maciço.

## Como isso vira decisão no MCP

Antes de `extrude_sketch`/`cut_extrude`, pergunte (ou decida com base no
contexto): *isso vai ser usinado, dobrado, soldado, moldado ou fundido?* A
resposta já define: raio mínimo de canto a aplicar com `fillet_edges`,
espessura mínima de parede antes de `shell_body`, e se faz sentido modelar
como peça única usinada ou como weldment de peças soldadas.
