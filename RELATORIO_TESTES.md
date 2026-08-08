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
