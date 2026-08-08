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
| `insert_drawing_view` | Usava apenas nomes de vista em inglês, como `*Front`; a instalação PT-BR expõe `*Frontal`. | Resolução da vista a partir de `GetModelViewNames` do modelo aberto, com aliases em inglês/PT-BR. | Inseriu a vista frontal da peça-base em um desenho temporário e o fechou sem salvar. |
| `switch_configuration` | `ShowConfiguration2` retorna `None` pelo COM embora a troca seja executada. | Verificação do nome em `ConfigurationManager.ActiveConfiguration` após a chamada. | Criou, ativou e salvou `MCP_TEST_CFG` com sucesso. |
| `save_document` / `export_document` | Falhas no inventário inicial por documento ativo incorreto. | Correção de ativação em `open_document` e teste com cópia isolada. | Salvou `configuration_validation.SLDPRT` e exportou `configuration_validation.step`. |
| `measure_body` / `set_material` | Exigiam uma peça sólida ativa. | Ambiente isolado com bloco 100 × 50 × 10 mm. | Medição e atribuição de AISI 1020 aprovadas. |
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
