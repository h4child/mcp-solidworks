# Relatório — Caixa de engrenagens de dois estágios (teste do MCP)

Sessão: 2026-10-06 · SolidWorks 2025 (API 33.4.1, PT-BR) · MCP 5.16.0 (instalado, SEM `create_gear`)
Arquivos do modelo: `C:\Users\pcrod\OneDrive\caixa_engrenagens_sw\`

## Decisões de projeto (e por quê)

1. **O enunciado é geometricamente inconsistente com 2 eixos.** Com o mesmo módulo, 20/40 exige
   distância entre centros 30·m e 18/54 exige 36·m; duas árvores só dão UMA distância. Solução
   padrão de mercado (confirmada em pesquisa: caixa de 2 estágios de eixos paralelos tem eixo de
   entrada, **intermediário** e saída): adicionar eixo intermediário com as engrenagens 40 e 18.
   Consequência: 3 eixos / 6 rolamentos (e não 2 / 4).
2. **Módulo m = 1,5 mm** (único que fecha as dimensões): C1 = 45 mm (20/40), C2 = 54 mm (18/54 —
   próximo dos "≈55 mm" pedidos). Eixos em linha em X = 0 / 45 / 99 mm.
3. Pinhão de 18 dentes: raiz Ø23,25 → eixo intermediário Ø12 (chaveta 4×4), senão sobra 1,3 mm de
   parede sob o rasgo de chaveta.
4. Comprimento dos eixos "≈100 mm" não é possível se uma ponta precisa sair da carcaça de 120 mm:
   entrada e saída ficam com ≈136 mm (20 mm expostos). Intermediário 112 mm.
5. Flange de união e pés de fixação ficam FORA do envelope 180×120×100 da caixa (a caixa em si
   mede 180×120×100).

## Erros encontrados (causa → solução)

### E1 — `draw_spline` gera curva errada no SolidWorks 2025 (API 33.4)
- Sintoma: spline de 10 pontos devolveu `length 139 mm` (esperado ≈ 5 mm), desenho com laços
  enormes; um 2º spline foi recusado ("SolidWorks refused to create a spline").
- Diagnóstico: teste com 3 pontos (0,0)(10,5)(20,0) deu comprimento 20,6155 = √(5²+20²), ou seja o
  SolidWorks leu o array de `CreateSpline3` como **triplas (x,y,z)** — não pares (x,y). O
  comentário em `server.py` (`draw_spline`: "CreateSpline3 expects XY pairs") está errado nesta versão.
- Contorno usado (sem mudar o MCP): enviar os números já como triplas `x,y,0` fatiados em pares
  (`[[x1,y1],[0,x2],[y2,0],...]`) — comprimento passou a bater.
- **Correção no servidor:** em `draw_spline`, montar `flattened` como `x, y, 0.0` por ponto
  (e remover o aviso errado do comentário). `create_gear` (já commitado, 269613f) usa o mesmo
  `CreateSpline3` com pares → **tem o mesmo bug**; precisa do mesmo ajuste.
