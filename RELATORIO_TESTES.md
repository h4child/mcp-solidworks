# Relatório de testes — SolidWorks MCP

Data: 08 de agosto de 2026

## Entregas

Foi criado um projeto de testes em `tests/`, sem dependências adicionais:

- `tests/test_contract.py`: validação estática do servidor e do manifesto;
- `tests/run_live_test_project.py`: matriz de integração para as 96 ferramentas;
- `tests/README.md`: instruções de execução e limites do teste;
- `tests/output/`: diretório de resultados locais, ignorado pelo Git.

Também foi corrigida a divergência documental: o README agora informa 96
ferramentas na versão 4.2.0, consistentes com `manifest.json`.

## Resultado das validações executadas

| Verificação | Resultado |
| --- | --- |
| Compilação de `server.py` e testes | Aprovada |
| Testes estáticos | 5 de 5 aprovados |
| Funções decoradas no servidor | 96, sem duplicidade |
| Ferramentas no manifesto | 96, idênticas ao servidor |
| Varredura de credenciais comuns | Nenhum padrão encontrado |
| Pacotes MCPB e resultados de teste no Git | Ignorados |
| Matriz de integração em modo seco | 96 de 96 casos planejados |

## Correções validadas posteriormente no SolidWorks 2025 PT-BR

| Ferramenta | Defeito encontrado | Correção | Resultado real |
| --- | --- | --- | --- |
| `open_document` | Abria uma peça já carregada, mas mantinha outro documento como ativo. | Ativação explícita com `ActivateDoc3` após `OpenDoc6`. | A peça `base_part.SLDPRT` passou a ficar ativa como `Part`. |
| `open_document` (Python 3.14) | O proxy COM tipado rejeitava os `VARIANT` passados como parâmetros de saída a `OpenDoc6`, impedindo a abertura de arquivos. | Adaptadores para `OpenDoc6`, `ActivateDoc3` e `Save3` normalizam a forma tipada `(valor, out...)` e o despacho dinâmico. | Abriu, ativou e consultou a peça oficial `block20.sldprt` do SolidWorks 2025 e a fechou sem salvar. |
| `save_document` (Python 3.14) | `Save3` do documento dinâmico, ao contrário do aplicativo tipado, exige `VARIANT` por referência para os resultados. | Adaptador tenta primeiro a forma dinâmica e aceita a forma tipada como alternativa. | Salvou a peça temporária `document_save_probe_148.SLDPRT`, alterou-a e salvou novamente no próprio arquivo. |
| `insert_drawing_view` | Usava apenas nomes de vista em inglês, como `*Front`; a instalação PT-BR expõe `*Frontal`. | Resolução da vista a partir de `GetModelViewNames` do modelo aberto, com aliases em inglês/PT-BR. | Inseriu a vista frontal da peça-base em um desenho temporário e o fechou sem salvar. |
| `insert_drawing_view` (Python 3.14) | `GetModelViewNames` é uma propriedade no `IDispatch` dinâmico e um método no proxy tipado, causando erro de iteração. | Avaliação condicional do membro quando ele é chamável. | Inseriu com êxito uma vista frontal da peça temporária no desenho `Desenho2 - Folha1`; os dois documentos foram fechados. |
| `switch_configuration` | `ShowConfiguration2` retorna `None` pelo COM embora a troca seja executada. | Verificação do nome em `ConfigurationManager.ActiveConfiguration` após a chamada. | Criou, ativou e salvou `MCP_TEST_CFG` com sucesso. |
| `save_document` / `export_document` | Falhas no inventário inicial por documento ativo incorreto. | Correção de ativação em `open_document` e teste com cópia isolada. | Salvou `configuration_validation.SLDPRT` e exportou `configuration_validation.step`. |
| `measure_body` / `set_material` | Exigiam uma peça sólida ativa. | Ambiente isolado com bloco 100 × 50 × 10 mm. | Medição e atribuição de AISI 1020 aprovadas. |
| `insert_component` (Python 3.14) | O pré-carregamento dependia do caminho de abertura COM corrigido. | Reuso do adaptador de abertura/ativação antes de `AddComponent5`. | Inseriu `document_save_probe_148-1` em `Mont1`, confirmou uma instância resolvida e fechou a peça e a montagem temporárias. |
| `suppress_component` / `unsuppress_component` | As chamadas `EditSuppress2` e `EditUnsuppress2` não são métodos invocáveis no COM desta versão. | Busca direta de `IComponent2` e uso de `SetSuppression2`; o código de sucesso `swSuppressionChangeOk` (2) passou a ser aceito. | Suprimiu e restaurou `base_part-1` em assemblies novos e isolados, verificando `IsSuppressed` após cada chamada. |
| `delete_component` / `list_components` | Após remover o único item, `GetComponents` devolve `None` e a listagem falhava ao iterar. | Normalização de coleção vazia para uma tupla vazia. | Excluiu `base_part-1` e confirmou `{"count": 0, "components": []}` no assembly temporário. |
| `add_mate` / `list_mates` / `list_features` | `AddMate5` recebia parâmetros incompletos; as listagens percorriam apenas o primeiro recurso COM. | Assinatura completa de `AddMate5`, marca de seleção 1, verificação de `swAddMateError_NoError` e enumeração por `FeatureManager.GetFeatures(False)`. | Criou `Coincidente1` entre duas cópias da peça-base; a mate e 25 recursos do assembly foram listados corretamente. |
| `draw_line` / `draw_centerline` / `draw_circle` / `draw_rectangle` / `draw_arc` / `draw_polygon` | — | Cada primitiva foi criada em um esboço novo, fechado e descartado isoladamente. | Todas aprovadas no SolidWorks 2025; o recurso `ProfileFeature` foi confirmado após cada teste. |
| `create_sketch_on_face` | Exige uma face plana real e coordenada em unidade de documento. | Bloco de teste extrudado de 10 mm e seleção da face superior em `(0, 0, 10)` mm. | Abriu e fechou um segundo esboço sobre a face superior com sucesso. |
| `add_sketch_dimension` | `AddDimension2` podia abrir a caixa de valor e bloquear o COM; o objeto COM era chamado incorretamente como função ao definir o valor. | Desativa temporariamente `swInputDimValOnCreate`, restaura a preferência do usuário e usa diretamente `IDisplayDimension.GetDimension2(0).SystemValue`. | Criou uma dimensão de linha e confirmou o valor de 35 mm sem diálogo. |
| `add_sketch_relation` | — | Linha diagonal isolada, seleção por coordenada e relação `horizontal`. | A relação foi inserida; `ISketchSegment.GetRelationsCount` retornou 1. |
| `extrude_sketch` / `measure_body` | A primeira asserção de teste usava a chave errada (`volume` em vez de `volume_m3`) no retorno da medição. | Validação pela caixa de corpo e pelas propriedades de massa. | Extrusão de 40 × 20 × 10 mm aprovada; volume, área, massa, centro de massa e caixa retornaram valores coerentes. |
| `cut_extrude` | — | Furo circular atravessante em bloco de teste. | Aprovado: volume caiu de `8.0e-06` para `7.2146e-06 m³`. |
| `revolve_sketch` | — | Perfil fechado afastado de uma linha de centro. | Revolução completa aprovada; sólido medido com volume positivo. |
| `fillet_edges` / `chamfer_edges` | — | Blocos extrudados com seleção automática de todas as 12 arestas. | Recursos `Fillet` e `Chamfer` criados e listados com sucesso. |
| `shell_body` | Usava `IFeatureManager.InsertFeatureShell`, inexistente no binding 2025; a face removida recebia marca de seleção incorreta. | Uso de `IModelDoc2.InsertFeatureShell`, face com marca 1 e verificação pelo novo recurso `Shell`. | Casca aberta de 2 mm aprovada; volume caiu de `8.0e-06` para `3.392e-06 m³`. |
| `create_reference_axis` | Chamava `InsertAxis2` no `FeatureManager`, embora a API 2025 o exponha em `IModelDoc2`. | Chamada de `IModelDoc2.InsertAxis2` e descoberta do novo recurso `RefAxis` na lista completa de recursos. | Criou o eixo `Eixo1` pela interseção dos planos frontal e superior. |
| `linear_pattern` | A chamada `FeatureLinearPattern4` tinha apenas 10 dos 20 argumentos e não marcava corretamente as direções/recursos selecionados. | Assinatura completa, marcas 1/2/4 exigidas pela API e eixos de referência reutilizáveis derivados dos planos-padrão localizados. | Criou `Padrão linear1` com 3 × 2 instâncias e os eixos `MCP Pattern Axis X/Y`. |
| `circular_pattern` | Tratava o parâmetro `FlipDirection` como `EqualSpacing` e tentava usar um plano como eixo de rotação. | Assinatura corrigida e eixo `MCP Pattern Axis Z` criado/reutilizado como referência real. | Criou `PadrãoCircular1` de quatro instâncias a 360°. |
| `hole_wizard` | Usava tipos de furo incorretos, uma chamada incompleta de `HoleWizard4` e criava o ponto de posição somente depois de inserir o recurso. | Tipos/standards/fasteners da API 2025, assinatura completa de 26 argumentos e ponto de esboço pré-selecionado na face antes da criação. | Criou recursos `HoleWzd` reais para furo simples, rebaixo, escareado, roscado cego e roscado passante ISO M6. |
| `create_weldment_profile` | Não criava o recurso-base de soldagem, passava grupos vazios e usava a opção de segmentos `0`, inválida. | Criação de `WeldmentFeature`, grupos `IStructuralMemberGroup` preenchidos por SAFEARRAY de segmentos e `swConnectedSegments_SimpleCut` (1). | Criou `WeldMemberFeat` com perfil ISO `square tube` 40 × 40 × 4, em segmentos simples e em grupo de dois segmentos. |
| `trim_extend_structural` | — | Dois membros estruturais de teste intersectados e corpos retornados pelo modelo. | Criou o recurso `Aparar/Estender1` para aparar um membro contra o outro. |
| `add_end_cap` | Seleção por coordenada atingia paredes laterais de perfis ocos; a implementação chamava a API obsoleta `InsertEndCapFeature` e usava direção de espessura inválida. | Localização da face planar terminal mais próxima, seleção externa por raio e uso de `InsertEndCapFeature3` com `swExtendOutward`. | Criou `Tampa de extremidade1` (`EndCap`) de 2 mm em um perfil ISO `square tube` 20 × 20 × 2. |
| `add_gusset` | `SelectByID2` não localizava faces anônimas de membros estruturais, o array obrigatório de faces era `None` e a semântica do perfil triangular estava invertida. | Resolução por `IFace2.GetClosestPointOn`, SAFEARRAY das duas faces de suporte e parâmetros completos para perfis triangular e poligonal. | Criou `Cantoneira1` (`Gusset`) de 5 mm tanto para o perfil triangular quanto para o perfil poligonal (`flat`). |
| `create_3d_sketch` | — | Peça temporária vazia; verificação pelo status do esboço ativo. | Abriu `3DSketch1` e reportou corretamente o tipo `3D`. |
| `draw_line_3d` | — | Novo esboço 3D temporário com uma linha espacial. | O esboço 3D fechado continha um segmento de `(0, 0, 0)` até `(100, 50, 25)` mm. |
| `create_base_flange` | A chamada obsoleta `InsertSheetMetalBaseFlange2` retornava `None` mesmo com perfil fechado válido. | Definição moderna `swFmBaseFlange` inicializada por `IBaseFlangeFeatureData`, com allowance, alívio e parâmetros de chapa explicitamente configurados. | Criou `Flange-base1` (`SMBaseFlange`), `SheetMetal` e `FlatPattern` para uma chapa de 100 × 50 × 2 mm. |
| `add_sheet_metal_bend` | Chamava `InsertBends2` no `FeatureManager`, que não o expõe; além disso, não fornecia K-factor e a API retornava `False`. | Uso de `IPartDoc.InsertBends2` com K-factor `0.5`, alívio automático e flat pattern. | Converteu uma caixa temporária com casca de 2 mm em `Chapa metálica2` e `Padrão-Plano2`. |
| `add_sheet_metal_edge_flange` | Chamava `InsertSheetMetalEdgeFlange2` com parâmetros escalares incompatíveis, sem o perfil associado à aresta exigido pela API. | Criação do esboço de flange por `InsertSketchForEdgeFlange`, conversão da aresta em geometria de esboço e associação por `IEdgeFlangeFeatureData.AddEdges` antes de `CreateFeature`. | Criou `Aresta-Flange1` (`EdgeFlange`) em uma chapa-base temporária de 100 × 50 × 2 mm; o recurso foi confirmado no FeatureManager. |
| `flatten_sheet_metal` | Chamava como função o booleano já retornado pelo binding COM para `EditUnsuppress2` e `EditSuppress2`. | Tratamento dos dois comandos sem argumentos como propriedades já executadas, com verificação explícita do resultado. | Ativou `Padrão-Plano1` e, na segunda chamada, o suprimiu novamente; o estado dobrado foi confirmado no recurso. |
| `create_helix` | Tratava o retorno `None` de `InsertHelix` como falha, embora a API 2025 declare o método como `void`. | Inventário dos recursos antes/depois da chamada e identificação do novo recurso do tipo `Helix`. | Criou `Hélice/Espiral1` (`Helix`) com círculo de 20 mm, passo de 2 mm e cinco revoluções. |
| `add_thread_feature` | Chamava `InsertCosmeticThread3` no documento e passava texto onde a API exige o enum inteiro de padrão. | Uso de `IFeatureManager.InsertCosmeticThread3` com `swStandardType_StandardNone` e callout que preserva a designação solicitada. | Criou `Representação de rosca1` (`CosmeticThread`) M6 × 1,0 de 10 mm em cilindro temporário. |
| `add_cosmetic_thread` | Chamava as APIs de rosca no documento, usava valor incorreto para condição passante e não validava comprimento cego. | Uso de `IFeatureManager.InsertCosmeticThread3`, `swEndConditionThrough` (2) e validação de comprimento positivo. | Criou `Representação de rosca1` (`CosmeticThread`) passante em cilindro temporário de 10 mm. |
| `create_knurl` | Gerava mais de 80 contornos abertos, inválidos para Wrap/Engrave, e invertia as marcas de seleção de face e esboço. | Perfil leve de três células fechadas, `Wrap/Engrave` analítico e marcas obrigatórias: face 1, esboço 4. | Criou `Envolver1` (`Emboss`, recurso de Wrap) em cilindros temporários para os padrões `diamond` e `straight`. |
| `add_rib` | Enviava sete argumentos com semântica incorreta e tratava o retorno vazio de `InsertRib` como falha. | Assinatura 2025 de dez argumentos, espessura bilateral centrada, direção normal/paralela correta e confirmação pela árvore de recursos. | Em `block20.sldprt`, criou e validou `Nervura1` (`Rib`) com 2,54 mm no ambiente oficial de casca/plano/esboço. |
| `mirror_feature` | Nenhum defeito adicional no ciclo atual. | Validação em peça isolada com ressalto lateral e plano direito. | Criou `Espelhar1` (`MirrorPattern`) a partir de `Ressalto-extrusão2`. |
| `mirror_body` | Enviava `BMirrorBody=False` para `InsertMirrorFeature2`, pedindo espelhamento de recurso mesmo com um corpo sólido selecionado. | Uso de `BMirrorBody=True` e manutenção das marcas de seleção de plano/corpo da API. | Criou `Espelhar1` (`MirrorSolid`) e confirmou dois corpos sólidos separados na peça temporária. |
| `move_copy_body` | Nenhum defeito adicional no ciclo atual. | Validação com cópia linear de um bloco isolado. | Criou `Corpo-Mover/Copiar1` (`MoveCopyBody`) a 40 mm e confirmou dois corpos sólidos. |
| `combine_bodies` | Para `add`/`common`, passava um corpo principal onde a API exige nulo; também montava `ToolVar` com objetos COM brutos, produzindo incompatibilidade de tipos. | `MainBody` nulo explícito para `add`/`common` e `SAFEARRAY` de `IDispatch` dos corpos participantes. | Criou `Combinar1` a partir de dois blocos adjacentes e confirmou um único corpo sólido resultante. |
| `create_reference_plane` | Nenhum defeito adicional no ciclo atual. | Validação de plano paralelo deslocado a partir do plano frontal. | Criou `Plano1` (`RefPlane`) com deslocamento de 25 mm. |
| `create_reference_axis` | Nenhum defeito adicional no ciclo atual. | Validação de eixo por interseção de dois planos padrão. | Criou `Eixo1` (`RefAxis`) pela interseção de `front` e `top`. |
| `split_body` | Tentava criar uma divisão cosmética de face e métodos inexistentes em vez do fluxo de divisão de sólidos. | Uso de `PreSplitBody2` para obter as regiões e `PostSplitBody2` com `SAFEARRAY` de corpos, origens e caminhos vazios. | Criou `Dividir1` (`Split`) e confirmou os dois corpos `Dividir1[1]` e `Dividir1[2]`. |
| `insert_section_view` | Nenhum defeito adicional no ciclo atual. | Ambiente isolado com peça longa, vista frontal e linha de corte horizontal. | Inseriu `Vista de seção A-A` no desenho temporário e fechou os documentos de teste. |
| `insert_detail_view` | Nenhum defeito adicional no ciclo atual. | Ambiente isolado com vista frontal e círculo de detalhe sobre a geometria. | Inseriu uma vista de detalhe em escala 2:1 no desenho temporário e fechou os documentos de teste. |
| `insert_broken_view` | Chamava métodos inexistentes (`BreakView2`/`BreakView3`) no objeto da vista e usava enums/posições de folha diretamente onde a API requer posições relativas à origem da vista. | Fluxo documentado `IView.InsertBreak` + `IDrawingDoc.BreakView`, enums corretos, conversão de coordenadas da folha, validação dos limites e `BreakLineGap`. | Criou uma quebra vertical zig-zag real em `Vista de desenho1`; `IView.IsBroken` confirmou a vista interrompida. |
| `insert_auxiliary_view` | `SelectByID2` não conseguia selecionar arestas projetadas de uma vista de desenho a partir de coordenadas da folha. | Enumeração oficial de `IView.GetVisibleEntities2`, projeção dos extremos de `IEdge` com `GetViewXform`, escolha da aresta visível mais próxima e seleção por `IView.SelectEntity`. | Inseriu `Vista de desenho2` perpendicular a uma aresta inclinada da peça triangular temporária. |
| `add_drawing_dimension` | Nenhum defeito adicional no ciclo atual. | Aresta inclinada de uma vista de desenho temporária, com posição de texto explícita. | Criou uma dimensão associativa real de uma única aresta. |
| `add_drawing_annotation` | Nenhum defeito adicional no ciclo atual. | Desenho isolado com nota `MCP LIVE TEST NOTE`, altura de 4 mm e posição definida. | Inseriu a anotação sem abrir diálogos nem manter documentos de teste abertos. |
| `add_centerline` | Nenhum defeito adicional no ciclo atual. | Desenho triangular temporário; o fallback manual foi selecionado quando as duas arestas não formaram uma linha de centro automática. | Criou a linha de centro manual entre os pontos solicitados. |
| `add_weld_symbol` | Tentava criar um símbolo genérico com `InsertWeldSymbol3`, ignorava o tipo/tamanho solicitados e frequentemente retornava `None` sem anexar a anotação. | Seletor compartilhado de aresta projetada e `IDrawingDoc.InsertWeldSymbol` configurado com símbolos ISO, tamanho e contagem de validação. | Inseriu símbolo de filete de 5 mm; `GetWeldSymbolCount` confirmou uma anotação na vista. |
| `add_surface_finish` | Chamava `InsertSurfaceFinishSymbol3` no desenho, embora a API a exponha em `IModelDocExtension`. | Seleção da aresta projetada pelo seletor compartilhado e chamada de `doc.Extension.InsertSurfaceFinishSymbol3` com os 14 argumentos, incluindo Ra. | Inseriu acabamento usinado Ra 3,2; `GetSFSymbolCount` confirmou uma anotação na vista. |
| `add_gdt_symbol` | Tentava encontrar a aresta com `SelectByID2` e chamava como método o `InsertGtol` já avaliado pelo proxy COM. | Seleção da aresta projetada pelo seletor compartilhado, acesso a `doc.InsertGtol` sem parênteses, símbolos ISO por `SetFrameSymbols2`, valores/datum por `SetFrameValues2` e verificação de anexo. | Inseriu uma moldura de posição de 0,1 em relação ao datum A, anexada à aresta solicitada em desenho temporário. |
| `list_open_documents` | Tratava `ISldWorks.GetDocuments` como propriedade; no proxy COM Python 3.14 ele é um método. | Avaliação condicional do membro antes da enumeração, preservando ambos os formatos de proxy. | Listou corretamente os documentos abertos e permitiu confirmar/limpar apenas os documentos temporários do ciclo. |
| `insert_bom_table` | Chamava `IView.InsertBomTable4` com a assinatura de `IModelDocExtension`, omitindo `UseAnchorPoint` e `AnchorType`; também passava configuração vazia e não encontrava templates instalados. | Uso da assinatura atual `IView.InsertBomTable6`, configuração referenciada pela vista e resolução do template de BOM nas pastas PT-BR/English da instalação. | Inseriu uma BOM `Parts Only` real para uma montagem temporária com uma peça. |
| `add_balloon` | Usava a sobrecarga errada de `IModelDocExtension.InsertBOMBalloon2` e dependia de `SelectByID2`, que não resolve arestas projetadas. | Seleção geométrica da aresta projetada; conversão de `IModelDocExtension` para a interface tipada; criação/configuração de `IBalloonOptions` e inserção da anotação. | Inseriu um balão circular de número de item em uma vista de montagem com BOM real. |
| `insert_cut_list_table` | Nenhum defeito adicional no ciclo atual. | Peça tubular ISO 40 × 40 × 4 criada como weldment, salva em ambiente isolado e inserida em uma vista de desenho. | Inseriu a tabela de lista de corte padrão em desenho temporário. |
| `create_weldment_profile` (3D) | A busca de esboço aceitava somente o tipo `ProfileFeature`, descartando `3DProfileFeature`. | Reconhecimento de ambos os tipos de recurso de esboço antes de montar os grupos estruturais. | Criou e confirmou um membro ISO `square tube` 40 × 40 × 4 em segmento espacial de 120 × 40 × 30 mm. |
| `create_exploded_view` | Tentava adicionar passos em `IAssemblyDoc`; a API 2025 os expõe em `IConfiguration`, exige a vista explodida ativa e o proxy já avalia alguns métodos sem argumentos. | Criação/ativação pelo nome real da vista, conversão para `IConfiguration`, seleção de componente com marca 1, normalização de retornos COM e rebuild seguro. | Criou dois passos solicitados em montagem temporária de duas peças; `GetNumberOfExplodeSteps` confirmou três passos totais (incluindo o automático). |
| `add_advanced_mate` | Nenhum defeito adicional no ciclo atual. | Montagem isolada com dois blocos, faces opostas selecionadas por coordenadas medidas das caixas de componente e mate de distância de 20 mm. | Criou `Distância1` (`MateDistanceDim`); `list_mates` confirmou uma mate ativa. |
| `interference_check` | Interpretava o retorno vazio de `ToolsCheckInterference2` como uma contagem, reportando falsamente que uma montagem sobreposta estava limpa. | Uso de `IInterferenceDetectionMgr.GetInterferenceCount`, com liberação garantida por `Done`. | Duas peças sobrepostas em 80 mm retornaram exatamente uma interferência e `clear: false`. |
| `create_assembly_pattern` | O ramo linear não selecionava uma direção e chamava `FeatureLinearPattern4` com argumentos faltantes; o circular usava marcas de seleção e assinatura incompletas. | Eixo de referência da montagem, componente com marca 1, direção com marca 2 e assinaturas completas de `FeatureLinearPattern4`/`FeatureCircularPattern5`. | Padrão linear criou três instâncias; padrão circular criou quatro instâncias em torno de uma face cilíndrica real. |
| `set_appearance` | Listas Python eram convertidas em `SAFEARRAY(VARIANT)` e corrompiam silenciosamente os canais RGB; o setter da extensão também recebia esse tipo incorreto. | Conversão explícita dos nove valores de aparência para `SAFEARRAY(double)` e uso de `IModelDocExtension.SetMaterialPropertyValues`/`IFace2.SetMaterialPropertyValues2`. | Corpo confirmou RGB `(10,120,200)` e face confirmou `(200,20,30)` por leitura oficial da API. |
| `connect_solidworks` | O proxy tipado expunha `RevisionNumber` como método, mas a ferramenta o serializava como objeto método. | Execução condicional quando o membro COM é chamável. | Conectou à instância aberta e retornou a revisão real `33.4.1`, sem iniciar outra instância. |
| `get_solidworks_info` | Mesmo defeito de serialização de `RevisionNumber`. | Normalização idêntica entre propriedade e método COM. | Retornou `33.4.1` e visibilidade `true` da instância aberta. |
| `create_new_part` | Sem defeito. | Não aplicável. | Criou uma peça a partir do template padrão, salvou-a em `tests/output` e a fechou isoladamente. |
| `create_new_assembly` | Sem defeito. | Não aplicável. | Criou uma montagem a partir do template padrão, salvou-a em `tests/output` e a fechou isoladamente. |
| `create_new_drawing` | Sem defeito. | Não aplicável. | Criou um desenho a partir do template padrão, confirmou o tipo `Drawing`, salvou-o e o fechou isoladamente. |
| `get_document_info` | Sem defeito. | Não aplicável. | Confirmou título, caminho absoluto e tipo `Part` de uma peça temporária salva. |
| `close_document` | Sem defeito. | Não aplicável. | Fechou somente a peça temporária ativa e confirmou que ela não permaneceu na coleção de documentos abertos. |
| `fix_component` | Sem defeito. | Não aplicável. | Em montagem temporária, fixou a segunda instância de componente e confirmou `IsFixed=true` pela API. |
| `float_component` | Sem defeito. | Não aplicável. | Em outra montagem temporária, liberou a primeira instância fixa e confirmou `IsFixed=false` pela API. |
| `create_sketch` | Sem defeito. | Não aplicável. | Criou um esboço no plano frontal localizado em SolidWorks PT-BR. |
| `get_sketch_status` | Sem defeito. | Não aplicável. | Confirmou estado inativo antes/depois e estado ativo com nome de esboço durante a edição. |
| `close_sketch` | Sem defeito. | Não aplicável. | Encerrou o esboço ativo e retornou confirmação de fechamento. |

## Execução de integração real

Foi executada a matriz com conexão limitada a 30 segundos. O SolidWorks foi
localizado em `C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe`, mas
o servidor não conseguiu obter uma instância COM utilizável dentro do prazo.

Resultado: 0 ferramentas aprovadas, 1 falha de conexão e 95 testes não
executados por dependência da conexão. O arquivo JSON detalhado foi salvo em
`tests/output/` e não será versionado.

Em uma execução posterior, fora do sandbox e conectada ao SolidWorks 33.4.1,
a matriz executou as 96 ferramentas: 35 passaram e 61 falharam. Esse primeiro
resultado é inventário, não uma certificação final: os testes ainda reutilizavam
um documento ativo inadequado entre alguns casos. A correção de `open_document`
acima elimina esse problema para as próximas execuções isoladas por ferramenta.

## Achado prioritário

A rotina `_connect()` tenta obter uma instância pelo ROT do COM e, quando isso
falha, inicia `SLDWORKS.exe`. Em seguida, ela aguarda a instância aparecer por
COM. Nesta máquina, a chamada permaneceu bloqueada e expirou. Além disso, o
timeout de `_run()` cancela a espera assíncrona, mas não interrompe a chamada
COM que já está rodando na thread do executor; essa thread impede o processo de
teste de terminar normalmente.

O executor de testes foi preparado para salvar o diagnóstico e finalizar de
forma controlada nesse caso. A correção definitiva do servidor deve tratar a
conexão COM bloqueada e evitar nova inicialização quando houver uma instância
do SolidWorks aberta, porém não exposta ao ROT (por exemplo, por diferença de
privilégios ou caixa de diálogo de inicialização).

## Próximo teste recomendado

Feche todas as janelas e processos do SolidWorks, abra uma única instância de
forma manual e conclua quaisquer telas de licença/recuperação. Em seguida,
execute:

```powershell
python tests/run_live_test_project.py --live
```

O comando cria seus resultados somente em `tests/output/` e produz um JSON com
o resultado individual de cada uma das 96 ferramentas.
