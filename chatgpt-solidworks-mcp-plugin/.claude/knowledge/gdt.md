# GD&T (tolerância geométrica) — quando e o quê

`add_gdt_symbol` existe no MCP mas é **EXP** (depende do PropertyManager do
desenho). Isso não muda a decisão de engenharia — decida a tolerância
geométrica certa e documente na resposta/propriedade customizada mesmo que a
aplicação visual no desenho não seja confiável hoje.

## As 14 características, resumidas

| Símbolo | Nome (PT) | Controla | Precisa de datum? |
| --- | --- | --- | --- |
| ⏤ | Retitude | desvio de uma linha em relação à reta ideal | não |
| ⏥ | Planeza | desvio de uma superfície em relação ao plano ideal | não |
| ○ | Circularidade | desvio de uma seção circular em relação ao círculo ideal | não |
| ⌭ | Cilindricidade | desvio de uma superfície cilíndrica | não |
| ⌒ | Perfil de linha | forma de um perfil 2D | opcional |
| ⌓ | Perfil de superfície | forma de uma superfície 3D | opcional |
| ∠ | Angularidade | ângulo em relação a um datum | sim |
| ⊥ | Perpendicularidade | 90° em relação a um datum | sim |
| ∥ | Paralelismo | paralelo a um datum | sim |
| ⌖ | Posição | localização de um furo/feature em relação a datums | sim |
| ◎ | Concentricidade | eixo coincidente com eixo datum | sim |
| ⌯ | Simetria | plano médio coincidente com plano datum | sim |
| ↗ | Batimento circular | variação radial numa volta, relativo a um eixo datum | sim |
| ↗↗ | Batimento total | batimento em toda a superfície | sim |

## Quando vale a pena aplicar (e quando é exagero)

- **Furos de fixação em padrão (parafusos de flange, por exemplo):**
  Posição (⌖) com datum A (face), B e C (dois furos ou eixo + um furo) é o
  padrão da indústria — melhor que tolerância linear ±, porque permite zona de
  tolerância circular em vez de quadrada (mais folga real pro mesmo risco).
- **Face de vedação (O-ring, gaxeta):** Planeza — vedação depende disso mais
  que do diâmetro.
- **Eixo que gira em rolamento:** Cilindricidade no assento do rolamento +
  batimento circular em relação ao eixo de rotação.
- **Peça prismática simples, sem acoplamento crítico:** tolerância geral
  (ISO 2768) já basta — não cubra o desenho de GD&T sem necessidade funcional.
  GD&T existe pra resolver um problema de função ou de custo de inspeção, não
  é "mais preciso = melhor".

## Datum reference frame — a ordem importa

Datum primário (A) remove o máximo de graus de liberdade possível (geralmente
a maior face plana, apoiada primeiro). Datum secundário (B) remove rotação.
Datum terciário (C) remove o grau de liberdade restante. Errar a ordem muda o
que a peça realmente controla — sempre pergunte "como essa peça é apoiada/
localizada na montagem real?" antes de escolher A/B/C.

## Regra prática pra decidir "preciso de GD&T aqui?"

Pergunte: essa feature **acopla** com outra peça, **veda**, ou **gira**? Se
sim, GD&T. Se é só forma/estética/não-crítica, tolerância geral resolve com
menos custo de inspeção.
