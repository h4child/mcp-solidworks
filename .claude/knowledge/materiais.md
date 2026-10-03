# Materiais — escolha e propriedades

Use `set_material(material, database)` com o nome **exato** da biblioteca
SolidWorks (sensível a maiúsculas). Os nomes abaixo existem na biblioteca
padrão "SOLIDWORKS Materials". Valores de propriedade são típicos de
referência (variam por têmpera/liga específica) — úteis pra decidir "qual
material faz sentido", não pra engenharia de certificação.

## Aços

| Nome no SolidWorks | Densidade (kg/m³) | Escoamento (MPa) | Ruptura (MPa) | Quando usar |
| --- | --- | --- | --- | --- |
| `AISI 1020` | 7870 | ~350 | ~420 | Aço baixo carbono, uso geral, boa soldabilidade e usinabilidade. Default razoável quando o usuário só diz "aço" sem mais detalhe. |
| `Plain Carbon Steel` | 7800 | ~220 | ~400 | Genérico, similar ao 1020. |
| `ASTM A36 Steel` | 7850 | ~250 | ~400 | Estrutural — perfis, chapas, vigas. Padrão pra estrutura soldada/parafusada civil e industrial. |
| `Alloy Steel` | 7700 | ~620 | ~790 | Maior resistência que carbono puro — eixos, engrenagens, componentes carregados. |
| `AISI 4340 Steel, normalized` | 7850 | ~710 | ~1110 | Alta resistência, eixos críticos, trem de pouso, componentes que precisam de tenacidade + resistência. |
| `Cast Alloy Steel` | 7700 | ~380 | ~620 | Peças fundidas estruturais. |

## Inoxidáveis

| Nome | Densidade | Escoamento (MPa) | Observação |
| --- | --- | --- | --- |
| `AISI 304` | 8000 | ~215 | Inox austenítico geral — não magnético, boa resistência à corrosão, não temperável. Default pra "inox" sem mais contexto (alimentício, médico, ambiente externo não agressivo). |
| `AISI 316` | 8000 | ~205 | Como o 304 + molibdênio → resistência superior a cloretos (ambiente marinho, químico, implante). Use quando houver exposição a sal ou produtos químicos agressivos. |

## Alumínios

| Nome | Densidade | Escoamento (MPa) | Observação |
| --- | --- | --- | --- |
| `6061 Alloy` | 2700 | ~275 (T6) | O alumínio "default" de uso geral — boa usinabilidade, soldável, boa relação resistência/peso. Use quando peso importa e a carga não é extrema. |
| `1060 Alloy` | 2700 | ~28 | Alumínio comercialmente puro, macio, alta condutividade — não estrutural (trocadores de calor, condutores). |
| `7075-T6` | 2810 | ~503 | Alta resistência (aeroespacial), mas baixa soldabilidade e mais caro — só especifique se o 6061 não aguentar a carga. |

## Plásticos de engenharia

| Nome | Densidade | Observação |
| --- | --- | --- |
| `ABS` | 1020 | Carcaças, protótipos, impressão 3D — resistente a impacto, não pra carga estrutural alta nem temperatura alta. |
| `Nylon 101` | 1140 | Engrenagens plásticas, buchas de baixo atrito — absorve umidade (muda dimensão). |
| `Polycarbonate` | 1200 | Alta resistência a impacto, transparente — visores, proteções. |

## Regra prática de seleção (ordem de decisão)

1. **Tem exposição a corrosão/química/alimento?** → inox (304 genérico, 316 se
   for ambiente agressivo/marinho).
2. **Peso é crítico (móvel, aéreo, manual)?** → alumínio 6061 primeiro; só suba
   pra 7075 se o cálculo mostrar que 6061 não aguenta.
3. **É estrutura fixa, soldada, grande (plataforma, suporte, chassi)?** → aço
   carbono (A36 se for perfil estrutural padronizado, 1020 se for chapa/peça
   usinada).
4. **Precisa de alta resistência num eixo/componente carregado pequeno?** →
   Alloy Steel ou 4340 conforme a carga.
5. **Não é estrutural, é carcaça/protótipo?** → plástico (ABS pra geral,
   Nylon se tiver atrito/desgaste).

Depois de `set_material`, confirme com `measure_body` que a massa calculada
bate com a expectativa (ver `verificacao_e_qa.md`) — densidade errada ou
geometria errada costumam aparecer ali primeiro.
