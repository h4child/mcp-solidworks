# Redutor planetário industrial de dois estágios — especificação e estado de validação

## Estado

Este arquivo é uma especificação verificável e um relatório de preparação. **A montagem não foi criada no SolidWorks nesta sessão**: não havia processo/documento ativo e as ferramentas MCP expostas não incluíam `create_new_part`, `create_new_assembly`, inserção de componentes, geração de coroa interna, estudo de movimento ou salvamento. Consequentemente, nenhum SLDPRT/SLDASM, vista explodida/cortada, interferência ou simulação é declarado como concluído.

## Configuração geométrica proposta

- Norma de dente: involuta de evolvente, dentado reto, ISO 53, sem deslocamento de perfil.
- Módulo: **m = 2,00 mm**.
- Ângulo de pressão: **20°**.
- Largura de face proposta: **20 mm** para engrenagens; coroas com 20 mm de largura útil.
- Arranjo: três planetárias a **0°, 120° e 240°**.
- Condição de montagem planetária: `(Z_solar + Z_coroa) / 3` inteiro.
- Condição cinemática: coroa fixa, solar de entrada, porta-satélites de saída.

### Estágio 1

| componente | dentes | diâmetro primitivo | diâmetro de base | diâmetro externo | diâmetro de raiz |
|---|---:|---:|---:|---:|---:|
| solar | 21 | 42,0 mm | 39,467 mm | 46,0 mm | 37,0 mm |
| cada planetária (3x) | 21 | 42,0 mm | 39,467 mm | 46,0 mm | 37,0 mm |
| coroa interna | 63 | 126,0 mm | 118,401 mm | — | — |

Distância solar–planetária: **42,0 mm**. Distância planetária–eixo central: **42,0 mm**. Distância entre centros da planetária e da coroa: **84,0 mm**. Condição de fase: `(21 + 63) / 3 = 28`.

### Estágio 2

| componente | dentes | diâmetro primitivo | diâmetro de base | diâmetro externo | diâmetro de raiz |
|---|---:|---:|---:|---:|---:|
| solar | 18 | 36,0 mm | 33,829 mm | 40,0 mm | 31,0 mm |
| cada planetária (3x) | 18 | 36,0 mm | 33,829 mm | 40,0 mm | 31,0 mm |
| coroa interna | 54 | 108,0 mm | 101,487 mm | — | — |

Distância solar–planetária: **36,0 mm**. Distância planetária–eixo central: **36,0 mm**. Distância entre centros da planetária e da coroa: **72,0 mm**. Condição de fase: `(18 + 54) / 3 = 24`.

## Relações matemáticas verificadas

Com a coroa fixa, solar acionadora e porta-satélites acionado:

`i = ω_solar / ω_carrier = 1 + Z_coroa / Z_solar`

- Estágio 1: `i₁ = 1 + 63/21 = 4,000:1`.
- Estágio 2: `i₂ = 1 + 54/18 = 4,000:1`.
- Redução total: **16,000:1**.
- Sentido: com a coroa fixa, a saída do porta-satélites gira no mesmo sentido da solar, a `1/16` da velocidade de entrada em regime ideal.

As tabelas acima foram recalculadas com `gear_geometry.py`, que gera o contorno completo de dentes involutos, não um disco liso. A tolerância de corda calculada foi 0,01345 mm (solares/planetárias do estágio 1), 0,00524 mm (coroa equivalente do estágio 1), 0,01350 mm (solares/planetárias do estágio 2) e 0,00671 mm (coroa equivalente do estágio 2). O gerador emitiu aviso de ausência de filete de raiz para as coroas; isso precisa ser tratado antes de uma peça industrial carregada.

## Árvore de arquivos que deveria ser criada no SolidWorks

`Carcaca_Principal.SLDPRT`, `Tampa_Carcaca.SLDPRT`, `Eixo_Entrada.SLDPRT`, `Eixo_Saida.SLDPRT`, `Solar_E1.SLDPRT`, `Planetaria_E1.SLDPRT`, `Coroa_E1.SLDPRT`, `PortaSat_E1.SLDPRT`, `Solar_E2.SLDPRT`, `Planetaria_E2.SLDPRT`, `Coroa_E2.SLDPRT`, `PortaSat_E2.SLDPRT`, `Pino_Planetario.SLDPRT`, `Espacador.SLDPRT`, `Rolamento.SLDPRT`, `Retentor.SLDPRT`, `Parafuso.SLDPRT`, `Submontagem_Estagio1.SLDASM`, `Submontagem_Estagio2.SLDASM` e `Redutor_Planetario_2E.SLDASM`.

## Checklist de verificação

| verificação | resultado nesta sessão |
|---|---|
| contagem de dentes e diâmetros primitivos | **verificado matematicamente** |
| compatibilidade `(Zs+Zr)/3` e centros | **verificado matematicamente** |
| involuta real | **verificado pelo gerador local**, não materializado no SolidWorks |
| montagem e mates | **não executado** |
| eixos, alojamentos, rolamentos e folgas | **não executado** |
| engrenamento sólido e colisões | **não executado** |
| interferência em posições angulares | **não executado** |
| estudo de movimento | **não executado** |
| materiais e densidades efetivas | **não executado** |
| BOM nativa do SolidWorks | **não executado** |
| vista explodida e vista em corte | **não executado** |
| salvar/reabrir SLDASM | **não executado** |

## Limitação operacional

Para concluir o pedido original é necessário iniciar o SolidWorks com uma sessão conectada ao MCP e expor novamente as operações de criação/abertura de documentos. Sem isso, criar arquivos com nomes `.SLDPRT`/`.SLDASM` seria apenas falsificação de artefatos e não uma montagem reabrível.
