# Engrenagens cilíndricas de dentes retos

Regra número um: **nunca desenhe dente de engrenagem à mão.** Use
`create_spur_gear`. Flanco involuto desenhado com `draw_line`/`draw_arc` +
`cut_extrude` + `circular_pattern` sai **liso** — não por falta de habilidade,
mas porque o motor de inferência do SolidWorks tem raio de snap em *pixels de
tela* (ver `verificacao_e_qa.md`, seção 0) e junta silenciosamente pontos na
escala de um dente. O vão chega degenerado no corte, e um vão achatado
repetido 20 vezes é um disco. `validate_model` passa, `measure_body` devolve
massa plausível, e você entrega um cilindro chamando de engrenagem.

`create_spur_gear` calcula o contorno inteiro analiticamente, desenha num só
perfil fechado com a inferência desligada, extruda uma vez, e **confere o
volume do sólido contra o volume analítico do contorno**. Leia `verified` e
`teeth_present` no retorno — `teeth_present: false` é literalmente "saiu
lisa".

## Os três números que definem a engrenagem

| Grandeza | Símbolo | Relação |
| --- | --- | --- |
| Módulo | m | tudo escala com ele; é o "passo" métrico |
| Número de dentes | z | — |
| Ângulo de pressão | α | 20° é o padrão universal; 14,5° só em peça antiga |

- Diâmetro primitivo: **d = m·z** (é o diâmetro que "engrena", não o externo)
- Diâmetro externo/de topo: **da = d + 2m**
- Diâmetro de pé/raiz: **df = d − 2,5m**
- Diâmetro de base: **db = d·cos α**
- Passo circular: **p = π·m**
- Espessura do dente na primitiva: **π·m/2**
- Distância entre centros de um par: **a = m·(z₁ + z₂)/2**
- Relação de transmissão: **i = z₂/z₁** (reduz se z₂ > z₁)

Duas engrenagens só engrenam se tiverem **o mesmo módulo e o mesmo ângulo de
pressão**. Nada mais precisa combinar.

## Módulos preferenciais (ISO 54) — escolha daqui

**1 · 1,25 · 1,5 · 2 · 2,5 · 3 · 4 · 5 · 6 · 8 · 10 · 12 · 16 · 20**

Série secundária (use só se a principal não servir): 1,125 · 1,375 · 1,75 ·
2,25 · 2,75 · 3,5 · 4,5 · 5,5 · 7 · 9 · 11 · 14 · 18.

Regra prática de partida: módulo pequeno (1 a 2) para mecanismo leve, de
instrumento ou impresso em 3D; 2 a 4 para redutor de pequeno porte e máquina
de bancada; 5 ou mais quando o torque é industrial de verdade.

## Número de dentes

- **Mínimo prático: 17 dentes** a 20°. Abaixo disso o flanco é *rebaixado*
  (undercut) pelo cremalheira que gera o dente: o pé fica entalhado, o dente
  enfraquece e o segmento útil de contato encurta. `create_spur_gear` avisa, e
  desenha o involuto completo (ou seja, mais forte do que a peça que uma
  fresa-caracol realmente produziria — não confie nela como se fosse a real).
- 12 a 16 dentes só com **correção de perfil** (x·m), que este gerador não
  faz. Se o projeto precisa, diga ao usuário em vez de entregar um pinhão
  rebaixado.
- Evite z₁ e z₂ com fator comum grande (ex.: 20 e 40): o mesmo par de dentes
  se encontra sempre, e o desgaste concentra. Um primo (19 e 41) distribui.

## Largura do dente (face width)

Usual: **b = 6·m a 12·m**. Mais estreito perde capacidade de carga; mais
largo exige alinhamento muito bom, senão só uma parte do dente carrega.
`create_spur_gear` avisa fora da faixa de 4·m a 16·m.

## Cubo, furo e rebaixo

- Deixe **ao menos 1,5·m de material** entre o furo do eixo e o pé do dente
  (a "alma" do aro). Menos que isso o aro flexiona sob carga e os dentes
  perdem contato — `create_spur_gear` avisa.
- Furo com assento de rolamento ou de eixo: dimensione o ajuste em
  `tolerancias_e_ajustes.md` (H7/k6 para cubo com chaveta é o lugar-comum).
- Chaveta: dimensão **nunca** inventada, veja `elementos_de_maquina.md`
  (DIN 6885).

## Material

Ver `materiais.md`. Para decidir rápido:

| Aplicação | Escolha típica |
| --- | --- |
| Protótipo, carga baixa, impresso | PLA/PETG, POM se precisar deslizar |
| Mecanismo leve, seco, silencioso | POM (acetal) ou nylon 6.6 |
| Redutor de uso geral | aço SAE 1045 (temperar a chama na superfície) |
| Carga alta / vida longa | aço SAE 4140 ou 8620 cementado e retificado |

Par de materiais diferentes (aço contra bronze/POM) desgasta melhor que aço
contra aço sem lubrificação.

## Folga (backlash)

O perfil que `create_spur_gear` corta é de **folga zero**: a espessura do
dente na primitiva é exatamente π·m/2. Numa montagem real a folga tem que
existir, ou o par trava com dilatação e erro de montagem. Folga normal de
referência: **0,04·m a 0,10·m** medida no diâmetro primitivo. Introduza-a
afastando os centros em metade disso, ou reduzindo a espessura de uma das
duas engrenagens — e diga ao usuário qual dos dois você usou.

## Montagem do par

1. Crie as duas engrenagens com o **mesmo** `module` e `pressure_angle`, cada
   uma em sua peça (`create_new_part` antes de cada `create_spur_gear`).
2. `insert_component` das duas na montagem, com os eixos paralelos, a
   `a = m·(z₁+z₂)/2` de distância entre centros (o retorno da ferramenta já
   traz esse número).
3. `add_mate` concêntrico de cada furo com seu eixo — lembrando que
   concêntrico **não** trava rotação (`montagens_mecanicas_reais.md`).
4. `add_advanced_mate("gear")` com a razão z₂/z₁ para o movimento girar
   acoplado. Essa mate é cinemática: ela **não** confere se os dentes
   realmente se encaixam.
5. `interference_check`. Com folga zero os flancos vão se tocar — é esperado;
   interferência *profunda* (mais que uns 0,1·m) significa que a distância
   entre centros está errada, não que a folga está apertada.

## Resistência do dente sem FEA

Não há simulação neste servidor. Para uma estimativa honesta de flexão na
raiz do dente, use **Lewis**:

σ = F_t / (b · m · Y)

- F_t = torque / raio primitivo = 2T/d (força tangencial)
- b = largura do dente, m = módulo (unidades coerentes)
- Y = fator de forma de Lewis: ≈ 0,245 (z=14), 0,289 (z=17), 0,322 (z=20),
  0,377 (z=30), 0,422 (z=50), 0,446 (z=75), 0,484 (z→∞), para 20° altura cheia

Compare σ com o escoamento do material (`materiais.md`) e aplique fator de
segurança — **2 a 3** para torque estático bem conhecido, **4 ou mais** para
carga alternada, choque ou torque incerto (o que é o caso quase sempre num
acionamento por motor). Lewis ignora concentração de tensão no pé, carga
dinâmica e desgaste de superfície: diga isso ao usuário, e recomende
verificação por AGMA/ISO 6336 ou ensaio se a aplicação for crítica.

## O que este gerador não faz

Helicoidal, interna, cônica, coroa e rosca sem fim, cremalheira, correção de
perfil (x·m), abaulamento (crowning) e chanfro de topo. Nenhuma dessas sai de
`create_spur_gear` — e nenhuma delas sai de `draw_*` na mão, pelo mesmo motivo
do snap. Se o pedido exige uma delas, diga ao usuário o que falta em vez de
entregar um cilindro.
