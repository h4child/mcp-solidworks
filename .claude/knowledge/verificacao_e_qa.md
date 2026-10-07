# Verificação e QA — antes de dizer "pronto"

Checklist real, não decorativo. Rode isso antes de responder ao usuário que
uma peça/montagem está terminada. Ver também o fluxo em `CLAUDE.md`.

## 0. O esboço é onde o erro nasce — confira antes de extrudar

Isto vem antes de tudo porque é o passo mais barato de corrigir e o mais caro
de descobrir depois: um esboço deslocado extruda perfeitamente, reconstrói sem
erro, passa no `validate_model` e só aparece como "peça que não encaixa" na
montagem, três etapas adiante.

**A causa, medida ao vivo (v5.15.0).** Toda chamada `Create*` do
`ISketchManager` passa pelo motor de inferência do SolidWorks, e o raio de snap
dele é uma distância **em pixels, não em milímetros**. Numa peça de
1,74 × 1,80 × 1,50 m:

| chamada | zoom | resultado |
| --- | --- | --- |
| `draw_rectangle(-20,-10,20,10)` | enquadrando a peça | **recusado** (`null`) |
| `draw_line(0,0,30,0)` | o mesmo | funcionou |
| `draw_rectangle(-200,200,200,600)` (400×400) | o mesmo | funcionou |
| o mesmo retângulo de 40×20, após `zoom_to_area` | aproximado | funcionou |

O `CreateLine` não passa por esse caminho; é por isso que o bug parece
aleatório. A recusa é a falha **ruidosa**. A **silenciosa** é a perigosa:
quando o snap apenas *desloca* um ponto para um vértice vizinho, a chamada tem
sucesso e devolve um segmento válido no lugar errado.

**O critério NÃO é "peça grande, feature pequena" (corrigido na v5.18.0).**
Essa era a leitura da tabela acima, e ela é incompleta. O que o motor junta é um
ponto novo que cai a poucos pixels de um ponto **que já está no esboço** — então
o que decide é o **espaçamento entre os pontos consecutivos do próprio perfil**,
não o tamanho da feature nem o da peça. Prova: a engrenagem saía lisa numa peça
de **44 mm**, onde não há peça grande nenhuma. O dente inteiro tem 4,5 mm (10%
da vista, confortável); as amostras do flanco involuto estão a 0,3 mm uma da
outra (0,7% da vista, dentro do raio de snap em qualquer zoom normal). O dente
"cabia" e colapsava do mesmo jeito.

Consequência prática: **toda curva amostrada corre esse risco**, em peça de
qualquer tamanho — involuto, lei de came, dente de polia HTD/GT2, vão de roda
dentada, trocoide, perfil de rosca, aerofólio, seção digitalizada de um desenho.

**A saída, e ela é uma só: `draw_profile`.** Calcule a curva, passe a lista de
pontos, e ela desenha a cadeia inteira com o motor de inferência **desligado**
(`SetAddToDB`) numa única chamada — nada pode snapar — e depois **lê cada
vértice de volta** do SolidWorks e compara: `max_point_deviation`,
`displaced_points` com índice, `verified`. Se faltar segmento, ela levanta erro
em vez de devolver: perfil incompleto é perfil aberto, e a extrusão seguinte
falharia com uma mensagem muito pior. Chame uma vez por contorno fechado (duas
chamadas = contorno externo + furo no mesmo esboço).

Não use `draw_line` em laço (200 round-trips, todos pelo motor de inferência) e
não use `draw_spline` para curva calculada — ela verifica os pontos de controle
desde a v5.18.0, mas continua passando pelo motor de inferência, então o erro é
detectado, não evitado. Spline é para forma livre (transição ergonômica, linha
de estilo). Para engrenagem, nem uma nem outra: `create_spur_gear`.

**O que fazer:**

0. Se a geometria é uma **curva calculada**, use `draw_profile` e pare aqui —
   os itens abaixo são sobre conviver com o motor de inferência, e o
   `draw_profile` não passa por ele.
1. Leia o campo **`snapped`** no retorno de qualquer `draw_*`. `snapped: true`
   significa que a geometria foi criada e o SolidWorks a pôs em outro lugar —
   apague e redesenhe, não compense no passo seguinte.
2. `verified: false` com `unreadable` preenchido é diferente: a geometria pode
   estar certa, só não foi possível conferir. Confirme com
   `list_sketch_entities` antes de seguir.
3. `zoom_retry: true` avisa que a primeira tentativa foi recusada e funcionou
   com zoom — a câmera ficou aproximada. Não é erro, mas é sinal de que você
   está desenhando pequeno numa peça grande: as próximas chamadas no mesmo
   esboço correm o mesmo risco.
4. **Antes de `extrude_sketch`/`revolve_sketch`/`cut_extrude`:** feche o
   esboço e rode `list_sketch_entities(name="Esboço3")`. É a única forma de
   perguntar onde a geometria foi parar — `measure_body` e `list_faces` só
   falam de sólido, e sólido só existe depois da extrusão.
5. Peça grande + feature pequena: `zoom_to_area` na região **antes** de
   desenhar. Barato, e evita a recusa.

**Spline:** `draw_spline` reporta `interior_points` desde a v5.18.0. Um ponto de
controle **interior** deslocado deixa as duas pontas exatamente onde foram
pedidas — era o caso em que a curva saía deformada e o retorno dizia
`verified: true`. Se `interior_points.verified` vier **`None`**, o SolidWorks não
expôs os pontos de controle nesta versão: o interior está **não verificado**, não
está certo.

**Centerline:** `draw_centerline` devolve `is_construction`. Se vier `false`, o
`revolve_sketch` não aceita aquilo como eixo — e se o perfil fechar em volta
dele, revoluciona um sólido diferente sem erro em lugar nenhum.

## 1. A árvore de features reconstrói sem erro

`validate_model` — rebuilda e relata erros/avisos. SolidWorks às vezes
"sucede" com uma feature internamente suprimida/com erro — não confie só no
retorno `True` de uma chamada anterior, rode `validate_model` de fato.
`list_features` com atenção ao campo `suppressed`/`error_code` de cada
feature confirma o que `validate_model` resumiu.

## 2. Massa e dimensões batem com a expectativa

`measure_body` depois de cada feature estrutural significativa, não só no
final — um erro de escala (mm vs m, ou um sketch 10× maior por engano)
aparece imediatamente na massa, e é muito mais barato corrigir uma feature
atrás do que destrinchar o erro no final.

Teste de sanidade rápido: `massa = volume × densidade`. Se a peça é maciça
(sem `shell_body`), `volume_m3 × densidade_do_material` deve bater com
`mass_kg` dentro de 1-2%. Se não bate, o material errado foi atribuído ou a
geometria não é o que você imagina.

## 3. Inspeção visual bate com a referência

`capture_standard_views` (ou `zoom_to_fit` + `set_view` + `capture_viewport`)
e compare proporção e silhueta com a foto/desenho/descrição original. Preste
atenção especial a:

- Proporção entre as dimensões principais (uma peça "parece" certa mesmo com
  erro de escala absoluto, se as proporções relativas estiverem erradas isso
  aparece no olho).
- Simetria, quando a peça deveria ser simétrica — erro de offset de sketch é
  comum e salta aos olhos numa vista frontal/superior.
- Features que deveriam existir e não existem (furo esquecido, chanfro
  esquecido).

## 4. Montagem: interferência e graus de liberdade

`interference_check` antes de considerar uma montagem pronta — peças que se
sobrepõem fisicamente não é um erro que o SolidWorks bloqueia sozinho ao
posicionar com `set_component_transform`/mates.

Confirme também que todo componente tem posição definida — ou fixo
(`fix_component`) ou totalmente restringido por mates (`list_mates`). Um
componente com graus de liberdade sobrando "flutua" na montagem real mesmo
que pareça no lugar certo na vista atual.

## 5. Checklist de fabricabilidade (DFM) antes de fechar

Ver `processos_de_fabricacao.md` pros números — mas a pergunta de triagem é:
*alguém consegue fabricar isso com o processo pretendido, do jeito que está
modelado?* Raio de canto interno existe onde a ferramenta exige, parede não
está fina demais pro processo, rosca/furo está na tabela padrão (ver
`elementos_de_maquina.md`), chapa dobrada respeita raio mínimo e flange
mínimo (ver `chapa_metalica.md`).

## 6. Quando o usuário pede "confirma que aguenta a carga"

Este MCP **não tem FEA**. Para uma resposta honesta:

1. Calcule à mão com a teoria aplicável — viga em flexão (`σ = M·c/I`),
   coluna em flambagem (Euler, se esbelta), pressão de contato, etc.,
   usando as dimensões e o material reais do modelo (`measure_body` dá área,
   `materiais.md` dá o escoamento do material atribuído).
2. Compare a tensão calculada com o escoamento do material e aplique um
   fator de segurança — **2 a 3 pra carga estática bem conhecida, 4+ pra
   carga dinâmica/fadiga ou incerteza relevante sobre a carga real.**
3. Deixe claro na resposta que é um cálculo analítico simplificado, não uma
   simulação — e recomende validação por simulação/ensaio real antes de
   produção se a aplicação for crítica (segurança, carga de pessoas,
   certificação regulatória).

## 7. Antes de `save_document`/entregar

- `get_custom_properties` preenchido com o que o usuário/processo downstream
  precisa (material, código, descrição — ver o backend Alfa Detail AI, que
  lê exatamente essas propriedades).
- Nome do arquivo e localização fazem sentido (não ficou em "Peça1.SLDPRT"
  default se o usuário pediu algo específico).
