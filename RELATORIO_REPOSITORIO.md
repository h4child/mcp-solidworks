# Relatorio do repositorio - SolidWorks MCP

Data da revisao: 16 de agosto de 2026

## Resumo

Este repositorio contem um servidor MCP em Python para automatizar o SolidWorks
pela API COM do Windows. O ponto de entrada e `server.py`; o catalogo publico
das ferramentas fica em `manifest.json`; e a documentacao principal fica em
`README.md`.

A versao revisada declara 139 ferramentas no manifesto e no servidor (versao
5.5.0), distribuida como pacote `.mcpb` com `server.type: "uv"`.

## Estrutura publica recomendada

| Arquivo | Finalidade | Situacao para GitHub |
| --- | --- | --- |
| `server.py` | Servidor FastMCP e ferramentas de automacao COM | Versionar |
| `manifest.json` | Metadados e catalogo de ferramentas MCP | Versionar |
| `requirements.txt` | Dependencias Python minimas | Versionar |
| `README.md` | Instalacao, uso, limites e seguranca | Versionar |
| `tests/` | Testes estaticos e executores de integracao local (geral, inspecao, chapa metalica/weldment) | Versionar |
| `RELATORIO_TESTES.md` | Historico tecnico de validacoes | Versionar |
| `COMO_USAR.md` | Guia de instalacao/uso para usuarios finais nao tecnicos | Versionar |
| `tests/output/` | Arquivos CAD e relatorios gerados localmente | Nao versionar |
| `*.SLDPRT`, `*.SLDASM`, `*.SLDDRW` | Modelos CAD nativos | Nao versionar por padrao |
| `*.mcpb` | Pacotes MCPB locais | Nao versionar |

## Revisao de informacoes sensiveis

Nao foram encontrados padroes comuns de chaves de API, tokens, senhas, chaves
privadas ou credenciais nos arquivos textuais versionaveis.

Os artefatos nativos do SolidWorks foram mantidos fora do Git por padrao porque
podem expor geometria proprietaria, dimensoes, nomes de projeto, caminhos de
referencia e informacoes de propriedade intelectual. Caso um modelo de exemplo
deva ser publico, ele deve ser revisado manualmente e liberado de forma
explicita.

O manifesto nao contem mais nome pessoal do ambiente local; o autor publico foi
normalizado para `SolidWorks MCP Contributors`.

## Seguranca operacional

A ferramenta `execute_python` e uma ferramenta privilegiada de depuracao. Mesmo
com alguns built-ins bloqueados, ela executa codigo Python com acesso aos
objetos COM `sw` e `doc`, portanto nao deve ser tratada como sandbox forte.

Para reduzir o risco em repositorios publicos, ela fica desativada por padrao e
so executa quando a variavel de ambiente abaixo e definida em uma sessao local
confiavel:

```powershell
$env:SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON = "1"
```

## Gitignore

O `.gitignore` cobre:

- caches Python e arquivos compilados;
- arquivos `.env`, certificados e nomes comuns de credenciais;
- logs e temporarios do Windows/SolidWorks;
- documentos nativos e backups do SolidWorks;
- pacotes MCPB e diretorios de empacotamento;
- resultados locais de testes em `tests/output/`.

## Conclusao

O repositorio esta adequado para publicacao tecnica desde que os modelos CAD
privados continuem ignorados e que `execute_python` seja documentada como
ferramenta privilegiada, desativada por padrao. O proximo refinamento para uma
release publica mais polida seria separar exemplos publicos revisados em uma
pasta propria e manter qualquer CAD real de cliente/projeto fora do Git.
