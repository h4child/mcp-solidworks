# Como usar o SolidWorks MCP

Este programa deixa você **conversar em português com o Claude** e ele opera o
SolidWorks para você: cria peças, monta conjuntos, gera desenhos, mede, exporta.

Você não precisa saber programar. Basta pedir.

---

## Antes de começar

Você precisa de **três coisas**, todas obrigatórias:

| O que | Observação |
|---|---|
| **Windows** | Não funciona em Mac ou Linux |
| **SolidWorks instalado e ativado** | Versão 2022 ou mais nova. É o seu SolidWorks normal, com sua licença |
| **Claude Desktop** | Baixe em [claude.ai/download](https://claude.ai/download) |

> **Importante:** este programa não substitui o SolidWorks. Ele *controla* o
> SolidWorks que já está no seu computador. Sem o SolidWorks instalado, nada
> funciona.

---

## Passo 1 — Instalar

1. Dê **dois cliques** no arquivo `solidworks-mcp-5.5.0.mcpb`
2. O Claude Desktop vai abrir sozinho e mostrar uma tela de instalação
3. Clique em **Instalar**
4. Pronto

### Se aparecer a mensagem "incompatível"

Isso acontece em alguns computadores. A solução leva um minuto:

1. Clique no menu Iniciar, digite `PowerShell` e abra
2. Cole o comando abaixo e aperte Enter:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

3. **Feche o Claude Desktop completamente** e abra de novo
4. Tente instalar o arquivo outra vez

---

## Passo 2 — Primeiro teste

Abra o Claude Desktop e escreva:

```
Conecte ao SolidWorks
```

O SolidWorks vai abrir sozinho (ou o Claude vai usar o que já estiver aberto).
Se ele responder que conectou, está tudo funcionando.

> A primeira conexão pode demorar até 1 minuto, porque o SolidWorks é um
> programa pesado. É normal.

---

## Passo 3 — Usando de verdade

Escreva pedidos em português comum. Alguns exemplos que funcionam:

**Criar peças**
```
Crie uma peça nova: um bloco de 100 x 50 x 20 mm
```
```
Faça um furo de 10 mm de diâmetro no centro dessa face
```

**Ver e entender**
```
Me mostre o que está na tela
```
```
Quais faces essa peça tem?
```
```
O que eu acabei de selecionar?
```

**Medir e conferir**
```
Quanto pesa essa peça?
```
```
Verifique se o modelo tem erros
```

**Salvar e exportar**
```
Salve em C:\Meus Projetos\peca.sldprt
```
```
Exporte para STEP
```

### Dica que faz diferença

Antes de pedir algo complicado, peça:

```
Olhe a tela e me diga o que está vendo
```

O Claude tira uma foto da janela do SolidWorks e realmente *enxerga* o modelo.
Depois disso ele acerta muito mais nos pedidos seguintes.

---

## O que ele sabe fazer

São 138 comandos. Em resumo:

- **Peças** — esboços, extrusão, corte, revolução, furos, arredondamentos, chanfros, roscas, nervuras
- **Montagens** — inserir componentes, posicionar, criar acoplamentos (mates), engrenagens, cames, vista explodida
- **Desenhos** — vistas, cortes, detalhes, cotas, símbolos de solda, tabelas de materiais
- **Chapa metálica** — dobras, abas, planificação, exportar DXF
- **Aparência** — cores, texturas, acabamentos, materiais
- **Análise** — massa, volume, centro de gravidade, colisões, verificação de erros
- **Arquivos** — abrir, salvar, exportar STEP/STL/PDF/DXF, empacotar projeto

---

## Cuidados importantes

⚠️ **Ele mexe nos seus arquivos de verdade.** Salvar, fechar, apagar componentes
e alterar geometria acontecem no documento aberto, na hora.

Por isso:

1. **Salve seu trabalho antes** de começar a usar
2. **Teste primeiro em uma cópia**, não no projeto importante
3. Se pedir algo e não gostar do resultado, use **Ctrl+Z** no SolidWorks

Não é preciso configurar nada de segurança. O comando mais perigoso
(`execute_python`) já vem **desligado de fábrica** e só um técnico consegue
ligar de propósito.

---

## Problemas comuns

| Aconteceu | O que fazer |
|---|---|
| "Não encontrei o SolidWorks" | Abra o SolidWorks manualmente e peça de novo |
| Travou / não responde | Veja se o SolidWorks está com alguma janela aberta esperando resposta (ex.: "Deseja salvar?"). Feche a janela |
| "Nenhum documento ativo" | Peça: `Crie uma peça nova` ou `Abra o arquivo C:\...` |
| Ele não achou a face que você queria | Peça: `Coloque em vista isométrica` e tente de novo. Algumas faces ficam escondidas em outras vistas |
| Nada funciona depois de instalar | Feche o Claude Desktop **completamente** (inclusive o ícone perto do relógio) e abra de novo |

---

## Resumindo

1. Instale o arquivo `.mcpb` com dois cliques
2. Escreva `Conecte ao SolidWorks`
3. Peça o que quiser, em português

É só isso.
