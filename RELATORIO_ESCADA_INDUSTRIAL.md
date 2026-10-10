# Relatorio - escada industrial (v5.20.0)

Pasta de saida: C:\Users\pcrod\Documents\EscadaIndustrial  (EscadaIndustrial.SLDASM, 12 pecas unicas, 9 sub-montagens, PNG isometrico, Lista_de_componentes.csv).

## Ferramentas novas (testadas ao vivo no SolidWorks 2025 SP4.1)
- create_profile_part, create_tube_part: 12 pecas criadas; volume medido = volume esperado (razao 1.00000) em todas; tubos realmente ocos.
- add_mate_by_name: ~150 mates (distancia/coincidente contra planos de origem), todos com movimento medido 0.
- interference_report: 0 interferencias volumetricas, 72 contatos de superficie.
- set_view_direction: isometrica Z-up exportada em PNG.

## Bugs encontrados / corrigidos
1. set_view_direction gravava a matriz da vista por LINHAS; a Orientation3 guarda right/up/back em COLUNAS (confirmado lendo *Right/*Bottom). Corrigido.
2. set_view_direction exportava imagem desatualizada: faltava GraphicsRedraw2() antes do SaveAs3. Corrigido.
3. Docstring de create_tube_part tinha o mapeamento largura/altura errado para axis x (largura corre em Z, altura em Y). Corrigido e medido.
4. add_mate_by_name so aceitava componentes de 1o nivel e recusava mates que posicionam: adicionados 'Sub-1/Peca-1' e allow_move.
5. create_profile_part: KeyError se a medicao nao trouxesse volume; agora erro claro; avisos de densidade propagados.
6. Contrato: manifest.json, tests/tool_names.json, README e versao (5.20.0) nao conheciam as 5 ferramentas.

## Limitacoes conhecidas (nao corrigidas)
- set_material('AISI 1020') nao tem efeito nesta instalacao (densidade continua 1000 kg/m3): massa do SolidWorks NAO e real; o material ficou so como propriedade 'Material'. Massa de aco calculada = volume x 7850.
- insert_component desloca sub-montagens (origem corrigida pelo centro da caixa, correto so para pecas): inseridas flutuantes na origem e fixadas por mates coincidentes aos planos de origem.
- (resolvido na v5.21.0, ver abaixo) Nao havia corte de extremidade.
- SolidWorks encerrou sozinho uma vez ao inserir SubTrilhos (RPC indisponivel); relancado e repetido com sucesso.

## Corte de extremidade (v5.21.0) - ferramenta `cut_part_end`
Corta a ponta de uma peca com um plano (normal + offset/ponto, `origin` para dar o plano em coordenadas da montagem) e mede volume, caixa, ponta sobre o plano e area da nova face.
Aplicada em Banzo (topo) e CorrimaoInclinado (topo), ambos cortados no plano vertical X=880 (face da perna / extremidade do guarda-corpo):
- Banzo: bloco alongado de 1189.29 para 1265 mm e cortado: volume 838353.3 mm3 (esperado A x L = 684 x 1225.66 = 838353.3, razao 1.00000), face de corte 924.4 mm2 (= 684/cos 42.27), caixa X max = 880.000.
- Corrimao: bloco alongado para 1222.29 mm, corpo deslocado -10.304 mm em Z (para o eixo cruzar X=880 em Z=1900, centro da extremidade do trilho), cortado: 306025.7 mm3 (esperado 306004.5, razao 1.00007), face de corte 343.5 mm2.
- Contato de face medido (interseccao booleana dos dois solidos com um deslocamento de 0.05 mm): banzo x perna 0.32 mm2 -> 381.1 mm2; corrimao x trilho 0 mm2 -> 99.2 mm2; sobreposicao volumetrica 0 nos dois.
- interference_report: 0 volumetricas, 78 contatos de superficie (72 antes; +2 banzo x perna, +4 entradas corrimao x trilho: duas regioes de contato por junta, pois o tubo e oco).
- Reabrir a montagem: 50 componentes, 0 nao resolvidos, 0 suprimidos.
- Armadilha medida: recriar o arquivo da peca do zero (mesmo nome) faz o SolidWorks 2025 abrir os componentes SUPRIMIDOS nas sub-montagens; editar a peca no lugar (profundidade da extrusao + corte) mantem a referencia. Os nomes de arquivo continuam "L1189" (comprimento do bloco original).
Limitacao: a normal do corte tem de ser perpendicular a um eixo global (sem angulo composto); o corte e plano (mitra), nao boca-de-peixe curva.
