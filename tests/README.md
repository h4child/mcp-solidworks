# Testes do SolidWorks MCP

Esta pasta contém uma suíte sem dependências extras para revisar o contrato do
servidor e um executor de integração que cria somente arquivos de teste em
`tests/output/` (diretório ignorado pelo Git).

Os arquivos CAD, pacotes MCPB, logs e temporários que estavam soltos na raiz
foram movidos para `tests/output/legacy/`, separados em `parts/`, `packages/`,
`logs/` e `temporary/`. Eles continuam locais e ignorados pelo Git.

## Validação sem SolidWorks

Execute em qualquer máquina com Python:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python tests/run_live_test_project.py --dry-run
```

Os testes estáticos verificam sintaxe, as 138 funções MCP, a equivalência entre
`server.py` e `manifest.json`, documentação de versão, regras de `.gitignore`
e padrões comuns de credenciais.

## Integração real

Com SolidWorks instalado e fechado ou aberto, execute:

```powershell
python tests/run_live_test_project.py --live
```

O comando tenta conectar ao SolidWorks, cria um projeto de teste separado e
invoca todas as ferramentas declaradas no manifesto, registrando sucesso,
falha ou bloqueio em `tests/output/live-test-AAAAmmdd-HHMMSS.json`.

Dois executores adicionais cobrem grupos de ferramentas com mais profundidade
do que a matriz genérica acima consegue (eles descobrem coordenadas reais via
`list_faces` em vez de assumir geometria fixa, e fecham o ciclo alimentando
essas descobertas de volta nas próprias ferramentas):

```powershell
python tests/run_inspection_live_test.py --live
python tests/run_sheet_metal_weldment_live_test.py --live
```

O primeiro valida `capture_viewport`, `get_selection` e `list_faces`. O
segundo valida chapa metálica (`create_base_flange`,
`add_sheet_metal_edge_flange`, `add_sheet_metal_bend`, `flatten_sheet_metal`)
e weldment/solda (`create_weldment_profile`, `add_gusset`, `add_end_cap`,
`trim_extend_structural`, `insert_drawing_view`, `add_weld_symbol`).

Algumas ferramentas exigem geometria ou seleção específica e são descritas como
experimentais no README principal. Uma falha dessas ferramentas será registrada
no relatório, sem interromper os demais casos. Execute o modo `--live` somente
em uma instalação de teste: ele cria, salva, altera e remove documentos CAD de
teste.

Se o SolidWorks estiver abrindo lentamente ou houver uma caixa de diálogo
pendente, use `--connection-timeout 30` para produzir imediatamente um
relatório de bloqueio e corrigir o ambiente antes de tentar a matriz completa.
