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

## Passo 1 — Preparar o computador (uma vez só)

Este passo é **obrigatório**. Sem ele, o programa instala mas não funciona.

1. Clique no menu Iniciar, digite `PowerShell` e abra
2. Cole o comando abaixo e aperte Enter:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

3. Espere terminar (uns 30 segundos) e **feche o PowerShell**

Isso instala uma ferramenta chamada `uv`, que cuida sozinha de tudo que o
programa precisa por baixo dos panos. Você não vai precisar mexer nela nunca
mais.

> **Por que isso é necessário?** O Claude Desktop não vem com essa parte
> incluída, e o programa não consegue trazê-la dentro do arquivo. É a única
> coisa que precisa ser instalada à parte — depois disso, tudo é automático.

## Passo 2 — Instalar o programa

1. Dê **dois cliques** no arquivo `solidworks-mcp-5.5.0.mcpb`
2. O Claude Desktop vai abrir e mostrar uma tela de instalação
3. Clique em **Instalar**
4. **Feche o Claude Desktop completamente** e abra de novo

Para fechar completamente: clique com o botão direito no ícone do Claude perto
do relógio (canto inferior direito) e escolha **Sair**. Só fechar a janela não
basta.

---

## Passo 3 — Primeiro teste

Abra o Claude Desktop e escreva:

```
Conecte ao SolidWorks
```

O SolidWorks vai abrir sozinho (ou o Claude vai usar o que já estiver aberto).
Se ele responder que conectou, está tudo funcionando.

> A primeira conexão pode demorar até 1 minuto, porque o SolidWorks é um
> programa pesado. É normal.

---

## Passo 4 — Usando de verdade

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
| Instalou, mas o Claude diz que não tem as ferramentas | Você provavelmente pulou o **Passo 1**. Rode o comando do `uv`, reinicie o Claude e tente de novo |
| "incompatível" na hora de instalar | Mesma coisa: faça o **Passo 1** primeiro |

---

## Resumindo

1. Rode uma vez o comando do `uv` no PowerShell (Passo 1)
2. Instale o arquivo `.mcpb` com dois cliques
3. Reinicie o Claude Desktop
4. Escreva `Conecte ao SolidWorks`
5. Peça o que quiser, em português

Só o Passo 1 exige um comando. O resto é conversa normal.
