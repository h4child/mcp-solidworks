# 🔧 Guia de Instalação Completo - SolidWorks MCP

## ✅ REQUISITOS ANTES DE COMEÇAR

- **Windows 10/11** (64-bit)
- **SolidWorks 2022+** instalado e licenciado
- **Python 3.10+** (vamos instalar via `uv`)
- **Claude Desktop** (para usar o MCP)
- **Git** (opcional, para clonar o repositório)

---

## 📋 PASSO 1: Preparar o Ambiente Windows

### 1.1 Abrir PowerShell como Administrador
- Pressione `Win + X` → Selecione **"Terminal (Admin)"** ou **"Windows PowerShell (Admin)"**
- Confirme quando pedir permissão

### 1.2 Verificar Python instalado
```powershell
python --version
```
- Se retornar uma versão (ex: `Python 3.12.1`), Python já existe ✅
- Se não reconhecer o comando, prossiga para instalar

### 1.3 Instalar Python via Winget (se não tiver)
```powershell
winget install Python.Python.3.12
```
- Aguarde a instalação completar
- Reinicie o PowerShell após finalizar

---

## 📂 PASSO 2: Clonar ou Descarregar o Repositório

### Opção A: Via Git (recomendado)
```powershell
git clone https://github.com/h4child/mcp-solidworks.git
cd mcp-solidworks
```

### Opção B: Descarregar ZIP manual
1. Acesse: https://github.com/h4child/mcp-solidworks
2. Clique em **"Code"** → **"Download ZIP"**
3. Extraia em uma pasta (ex: `C:\Users\SEU_USUARIO\mcp-solidworks`)
4. Abra PowerShell e entre na pasta:
```powershell
cd C:\Users\SEU_USUARIO\mcp-solidworks
```

---

## 🐍 PASSO 3: Instalar Dependências

### 3.1 Instalar `uv` (gerenciador de pacotes Python)
```powershell
pip install uv
```

### 3.2 Instalar dependências do projeto
```powershell
uv sync
```
Isso vai instalar:
- `win32com` (comunicação COM com SolidWorks)
- `pydantic` (validação de dados)
- `mcp` (SDK do protocolo MCP)
- E outras dependências

**⏱️ Pode levar 2-3 minutos**

### 3.3 Verificar instalação
```powershell
uv run python -c "import win32com; print('✅ COM library pronta')"
```

---

## 🚀 PASSO 4: Testar a Conexão

### 4.1 Iniciar o servidor MCP
```powershell
uv run python server.py
```

Você deve ver algo assim:
```
18:51:57 [DEBUG] mcp.server.lowlevel.server: Initializing server 'solidworks-mcp'
18:51:57 [DEBUG] solidworks-mcp: COM thread initialised (CoInitialize)
18:51:57 [INFO] solidworks-mcp: Connected to running SolidWorks instance
```

✅ **Se ver "Connected" = Sucesso!**
❌ **Se ver erro = Veja a seção de Troubleshooting abaixo**

### 4.2 Deixar servidor rodando
- **NÃO feche o PowerShell** — o servidor precisa ficar aberto
- Abra **outra janela de PowerShell** para testar

### 4.3 Testar em outra janela PowerShell
```powershell
cd C:\Users\SEU_USUARIO\mcp-solidworks
uv run python tests/run_inspection_live_test.py --live
```

Espere pelos testes rodarem (~30 segundos)

---

## 🎨 PASSO 5: Configurar no Claude Desktop

### 5.1 Abrir arquivo de configuração
- Pressione `Win + R`
- Cole: `%APPDATA%\Claude\claude_desktop_config.json`
- Pressione Enter

### 5.2 Editar o arquivo JSON
Procure por `mcpServers` e adicione (se não existir):

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\SEU_USUARIO\\mcp-solidworks",
        "run",
        "python",
        "server.py"
      ]
    }
  }
}
```

**⚠️ IMPORTANTE:**
- Substitua `SEU_USUARIO` pelo seu nome de usuário Windows
- Use barras invertidas `\\` (não `/`)
- Verifique se o JSON está válido (sem vírgulas extras)

### 5.3 Salvar e reiniciar Claude Desktop
- Salve o arquivo (Ctrl+S)
- Feche e reabra o Claude Desktop completamente

---

## ✅ PASSO 6: Validar a Instalação

### No Claude Desktop:
1. Abra uma conversa nova
2. Digite: **"conectar ao SolidWorks"**
3. O Claude deve reconhecer e oferecer ferramentas do MCP

Se vir sugestões de ferramentas tipo `create_new_part`, `draw_rectangle`, etc. = ✅ **Funcionando!**

---

## 🆘 TROUBLESHOOTING

### ❌ Erro: "O servidor RPC não está disponível"

**Solução 1: SolidWorks não está aberto**
```powershell
# Abra o SolidWorks manualmente antes de rodar o servidor
# Ou deixe que o servidor abra automaticamente (pode levar 10s)
```

**Solução 2: Porta bloqueada**
```powershell
# Verifique e libere portas COM
netstat -ano | findstr "COM"
```

**Solução 3: Reiniciar tudo**
```powershell
# 1. Feche o SolidWorks (Alt+F4)
# 2. Feche o servidor MCP (Ctrl+C no PowerShell)
# 3. Aguarde 3 segundos
# 4. Reabra o servidor:
uv run python server.py
# 5. Reabra o SolidWorks
```

### ❌ Erro: "ModuleNotFoundError: No module named 'win32com'"

```powershell
# Reinstale as dependências
uv sync --refresh
uv run python -m pip install --force-reinstall pywin32
```

### ❌ Erro: "Python 3.14 removed the cgi module"

```powershell
# Certifique-se de usar uv para rodar (nunca python direto):
uv run python server.py  # ✅ Correto
python server.py        # ❌ Errado
```

### ❌ Claude não reconhece o MCP

1. Verifique o caminho em `claude_desktop_config.json`
2. Confirme que não há erros JSON (use um validador online)
3. Reinicie o Claude Desktop completamente
4. Limpe o cache: Delete `%APPDATA%\Claude\mcp_server_cache\`

---

## 📊 VALIDAÇÃO COMPLETA

Depois de instalar, execute todos os testes:

```powershell
# Terminal 1: Servidor rodando
uv run python server.py

# Terminal 2: Testes (em outra janela)
# Teste 1: Geral
uv run python tests/run_live_test_project.py --live

# Teste 2: Sheet Metal + Weldment
uv run python tests/run_sheet_metal_weldment_live_test.py --live

# Teste 3: Leitura de estado
uv run python tests/run_read_tools_live_test.py

# Teste 4: Contrato estático
python -m unittest discover -s tests -p "test_*.py"
```

Se todos os testes passarem: ✅ **Instalação 100% pronta!**

---

## 🎯 Próximos Passos

1. **Abra o Claude Desktop**
2. **Peça ao Claude:** "Criar uma peça de teste no SolidWorks"
3. **Veja o SolidWorks se atualizando automaticamente** 🚀

---

## 📞 Suporte

Se algo não funcionar:

1. Verifique se o SolidWorks está aberto
2. Veja o terminal do servidor para mensagens de erro
3. Confirme que Python 3.10+ está instalado
4. Teste a conexão COM manualmente:
   ```powershell
   uv run python
   >>> import win32com.client
   >>> sw = win32com.client.GetObject("", "SldWorks.Application")
   >>> print(sw.RevisionNumber)  # Deve exibir a versão
   ```

---

**Versão:** 5.5.0 | **Data:** 16/08/2026 | **Status:** ✅ Operacional
