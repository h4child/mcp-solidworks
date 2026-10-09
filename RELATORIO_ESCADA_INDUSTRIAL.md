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
- Nao ha corte de extremidade (mitra / boca-de-peixe): ligacoes banzo-perna e corrimao-guarda-corpo ficam em contato de linha/ponto, nao de face.
- SolidWorks encerrou sozinho uma vez ao inserir SubTrilhos (RPC indisponivel); relancado e repetido com sucesso.
