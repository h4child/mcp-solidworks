# Relatório do repositório — SolidWorks MCP

Data da análise: 08 de agosto de 2026
Escopo: todos os arquivos presentes na raiz do repositório e o código Python
executável.

## Resumo

Este é um servidor MCP em Python para automatizar o SolidWorks por meio da API
COM do Windows. O ponto de entrada é `server.py`; o pacote é descrito por
`manifest.json` e pode ser distribuído como um pacote MCPB. O repositório
estava sem commits e sem `.gitignore` antes desta revisão.

## Estrutura

| Arquivo | Finalidade | Situação para publicação |
| --- | --- | --- |
| `server.py` | Servidor FastMCP e ferramentas de automação COM | Versionar |
| `manifest.json` | Metadados e catálogo de ferramentas MCP | Versionar |
| `requirements.txt` | Dependências Python (`mcp` e `pywin32`) | Versionar |
| `README.md` | Instalação, operação e estado das ferramentas | Versionar |
| `.mcpbignore` | Exclusões do conteúdo de pacotes MCPB | Versionar |
| `bracket_mcp.SLDPRT`, `peca_simples.SLDPRT` | Modelos CAD locais/de teste | Não versionar por padrão |
| `debug.log`, `__pycache__/`, `~$*.SLDPRT` | Logs, cache e arquivos temporários | Não versionar |

## Resultado da revisão técnica

- `server.py` possui 4.779 linhas e passou na compilação (`python -m compileall`).
- Há 96 ferramentas MCP decoradas no servidor e 96 entradas no manifesto.
  Os dois conjuntos são idênticos e não possuem nomes duplicados.
- A arquitetura usa uma única thread COM/STA com timeout de 120 segundos,
  apropriada para objetos COM do SolidWorks.
- Não foram encontradas chamadas de rede, `subprocess`, `os.system`, escrita
  direta de arquivos pelo Python do servidor nem dependências fora das duas
  declaradas em `requirements.txt`.

## Revisão de informações sensíveis

Foram procurados padrões de chaves de API, tokens, senhas, chaves privadas,
JWTs e credenciais em todos os arquivos textuais do projeto. Nenhuma credencial
ou segredo foi encontrado.

O arquivo `debug.log` contém um caminho local do Windows com o nome de usuário.
Não contém uma credencial, mas revela informação do ambiente local; por isso
foi incluído no `.gitignore`.

Os arquivos `.SLDPRT` não contêm credenciais identificadas nesta análise, mas
podem expor geometria, dimensões, nomes de projeto e outras informações de
propriedade intelectual. Eles são ignorados por padrão. Caso um modelo de
exemplo deva ser público, ele deve ser revisado e adicionado conscientemente em
um diretório `examples/` com uma exceção explícita no `.gitignore`.

## Atenção de segurança operacional

A ferramenta `execute_python` executa código fornecido pelo cliente MCP. Ela
remove alguns built-ins perigosos (`open`, `import`, `exec`, `eval` e outros),
mas expõe objetos COM do SolidWorks e `win32com` ao código executado. Portanto,
ela não deve ser considerada um sandbox forte. Publique/execute o servidor
somente com clientes MCP confiáveis; para uma distribuição mais restrita,
considere remover ou proteger essa ferramenta por configuração/autorização.

## Exclusões adicionadas

O `.gitignore` passa a excluir:

- caches Python, logs e temporários do Windows/SolidWorks;
- arquivos locais de ambiente, certificados e arquivos de credenciais comuns;
- documentos nativos e backups do SolidWorks;
- todos os pacotes `*.mcpb` e diretórios de trabalho MCPB, para Claude Desktop
  e Codex.

## Limites da análise

Esta revisão é estática: não abre o SolidWorks, não executa comandos contra a
API COM e não inspeciona internamente os binários CAD. Antes de publicar uma
release, execute as ferramentas pretendidas em uma máquina de teste e revise
manualmente qualquer modelo CAD que queira distribuir.
