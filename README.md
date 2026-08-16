# SolidWorks MCP Server

Servidor MCP em Python que controla o SolidWorks via COM (`win32com`), escrito
com o SDK oficial (`mcp`, usando `FastMCP`). **139 ferramentas** (v5.5.0).

## Para quem so quer usar

Instale o `.mcpb` com dois cliques. O passo a passo completo, sem jargao, esta
em [COMO_USAR.md](COMO_USAR.md).

Requisitos: Windows, SolidWorks 2022+ instalado e licenciado, e Claude Desktop.

## Para quem vai desenvolver

```bash
pip install -r requirements.txt
python -m unittest tests.test_contract
```

Configuracao manual em `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:\\caminho\\para\\mcp-solidworks\\server.py"]
    }
  }
}
```

## Gerar o pacote distribuivel

O `manifest.json` declara `server.type: "uv"`, entao o Claude Desktop resolve
Python e dependencias sozinho a partir do `pyproject.toml` -- necessario porque
`pywin32` e `pydantic` sao extensoes compiladas e nao podem ser empacotadas de
forma portatil.

```bash
npx mcpb pack . solidworks-mcp-5.5.0.mcpb
```

Abra o SolidWorks (opcional -- o servidor consegue abrir sozinho) e peca para o
Claude "conectar ao SolidWorks".

## Seguranca para uso publico

Este servidor foi projetado para automacao local confiavel. Ele controla uma
sessao real do SolidWorks pelo COM do Windows, entao ferramentas como salvar,
fechar, suprimir componentes e alterar geometrias modificam documentos abertos.

A ferramenta `execute_python` fica desativada por padrao porque executa Python
com acesso aos objetos COM `sw` e `doc`. Para usar somente em depuracao local
confiavel, defina explicitamente:

```powershell
$env:SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON = "1"
```

Nao publique modelos CAD privados, pacotes `.mcpb`, logs ou resultados de teste
gerados localmente. O `.gitignore` ja exclui esses artefatos por padrao.

## Status de verificacao (testado ao vivo no SolidWorks 2025, PT-BR)

A matriz abaixo registra o estado validado no SolidWorks 2025 PT-BR. Legenda:
- OK  = criou o recurso com sucesso no teste ao vivo.
- EXP = experimental: a assinatura COM esta correta mas o recurso depende de
        selecao/estado especifico ou de uma parte da API que se comporta de
        forma inconsistente nesta versao; pode exigir ajuste manual.

### Conexao / Documentos / Utilidades -- OK
`connect_solidworks`, `get_solidworks_info`, `create_new_part`,
`create_new_assembly`, `create_new_drawing`, `open_document`, `close_document`,
`save_document`, `get_document_info`, `list_open_documents`, `set_units`,
`set_view`, `zoom_to_fit`, `zoom_to_area`, `get_view_state`

`execute_python` e uma ferramenta de depuracao privilegiada. Ela existe no
catalogo, mas permanece bloqueada ate
`SOLIDWORKS_MCP_ENABLE_EXECUTE_PYTHON=1` ser definido.

> Correcao importante: os templates padrao sao resolvidos pelos indices
> corretos (`swDefaultTemplatePart/Assembly/Drawing` = 8/9/10). Antes, o desenho
> era criado silenciosamente como uma peca.

### Esbocos e desenho 2D -- OK
`create_sketch`, `create_sketch_on_face`, `close_sketch`, `get_sketch_status`,
`create_3d_sketch`, `draw_line`, `draw_line_3d`, `draw_circle`, `draw_rectangle`,
`draw_arc`, `draw_polygon`, `add_sketch_dimension`, `add_sketch_relation`

### Features 3D -- OK
`extrude_sketch`, `cut_extrude`, `revolve_sketch`, `sweep_sketch`,
`loft_sketches`, `fillet_edges`, `chamfer_edges`, `shell_body`,
`linear_pattern`, `circular_pattern`, `hole_wizard`, `list_features`

### Operacoes de corpo / referencia
`mirror_feature` (OK), `mirror_body` (OK), `move_copy_body` (OK),
`create_reference_plane` (OK), `set_material` (OK), `set_appearance` (OK),
`create_reference_axis` (EXP -- InsertAxis2 depende de selecao valida),
`combine_bodies` (EXP -- corpos precisam se tocar/interseccionar),
`split_body` (EXP -- fluxo Pre/PostSplitBody), `add_rib` (EXP)

### Montagem
`insert_component` (OK), `list_components` (OK), `fix_component`,
`float_component`, `delete_component`, `suppress_component`,
`unsuppress_component`, `list_mates`, `interference_check` (OK),
`add_mate` (EXP), `add_advanced_mate` (EXP), `add_cam_follower_mate` (OK),
`add_screw_mate` (OK), `add_rack_pinion_mate` (OK),
`list_motion_studies` (OK), `create_motion_study` (OK),
`create_assembly_pattern` (EXP -- precisa de referencia de direcao),
`create_exploded_view` (EXP)

### Propriedades / Medicao / Exportacao / Configuracoes -- OK
`get_custom_properties`, `set_custom_property`, `measure_body`,
`export_document`, `create_configuration`,
`switch_configuration` (resolve nomes localizados, ex. "Valor predeterminado")

### Leitura de estado (tela, arvore, montagem) -- OK
Adicionado e validado ao vivo em 16/08/2026: `get_view_state` reporta o zoom
(`Scale2`), o codigo bruto de modo de exibicao, a matriz de rotacao 3x3 da
camera e -- quando ela bate exatamente com uma das 9 vistas nomeadas de
`set_view` -- esse nome em `closest_named_view` (`None` quando o usuario girou
o modelo livremente; testado ao vivo com uma rotacao manual para confirmar que
nao ha falso-positivo). `list_features` passou a reportar `suppressed` e
`error_code` por recurso, e `list_components` passou a reportar `visible`
(independente de `suppressed`) via `IComponent2.GetVisibility`, confirmado por
um teste de ida-e-volta ocultar/exibir. Nenhum destes numeros foi assumido: os
valores de referencia das 9 orientacoes nomeadas e o mapeamento 0/1 de
visibilidade foram capturados ao vivo antes de escrever o codigo.

### Perfis estruturais (weldments) -- OK, exceto add_end_cap
Validado ao vivo (SolidWorks 2025) com um membro em L + um membro cruzando-o,
ambos em tubo quadrado ISO: `create_3d_sketch`, `draw_line_3d`,
`create_weldment_profile`, `add_gusset`, `trim_extend_structural`. O caminho da
biblioteca de perfis e resolvido em
`C:\ProgramData\SOLIDWORKS\...\weldment profiles\<padrao>\<tipo>.sldlfp`
(o tamanho e uma *configuracao* dentro do arquivo, descoberta automaticamente
por `create_weldment_profile` a partir do nome pedido).

`add_gusset` funciona de forma confiavel no caso classico (uma face de viga +
uma face de chapa/outra viga que se encontram numa aresta real); um canto
chanfrado (miter) de uma unica peca em L e um caso mais ambiguo e pode
precisar de um par de faces escolhido com mais cuidado.

`add_end_cap` (EXP -- **defeito confirmado do SolidWorks 2025**): apos corrigir
um bug real do lado do MCP (a face certa era identificada mas depois
descartada e re-selecionada por ray-casting a partir do ponto original, que
cai exatamente na borda do vazio interno de um perfil oco -- agora a face
identificada e selecionada diretamente), `InsertEndCapFeature3` ainda retorna
`None` em toda uma matriz de tentativas: os tres valores do enum de direcao,
com/sem tratamento de canto, com uma ou duas faces selecionadas, com o
documento salvo, e ate com os valores exatos do exemplo oficial da API da
SolidWorks copiados literalmente. Mesmo padrao ja documentado abaixo para
`create_helix` (`InsertHelix`).

### Chapa metalica -- OK
Validado ao vivo (SolidWorks 2025): `create_base_flange` (o parametro PCBA
exige um VARIANT de dispatch, nao bool -- corrigido), `add_sheet_metal_bend`
(usa Insert Bends num corpo previamente casqueado com `shell_body`),
`add_sheet_metal_edge_flange` (requer objetos de aresta, resolvidos a partir
de um ponto conhecido na aresta), `flatten_sheet_metal` (alterna
flat/folded).

### Roscas / usinagem -- parcial
`add_cosmetic_thread` (EXP), `add_thread_feature` (delega para rosca cosmetica --
roscas 3D reais NAO sao expostas pela API do SolidWorks),
`create_helix` (EXP -- `InsertHelix` retorna None via COM nesta versao),
`create_knurl` (EXP -- Wrap+Deboss).

### Desenho tecnico detalhado -- parcial
`add_drawing_annotation` (OK), `insert_drawing_view` (OK -- validado ao vivo
inserindo uma vista isometrica de uma peca de weldment; a nota anterior sobre
`CreateDrawViewFromModelView3` retornar `None` estava desatualizada),
`add_weld_symbol` (OK -- validado ao vivo colocando um simbolo de solda de
filete numa aresta da vista inserida). As demais sao EXP e dependem do
gerenciador de propriedades ou de uma vista de modelo com estado especifico:
`insert_section_view`, `insert_detail_view`, `insert_broken_view`,
`insert_auxiliary_view`, `add_drawing_dimension`, `add_centerline`,
`add_surface_finish`, `add_gdt_symbol`, `add_balloon`, `insert_bom_table`,
`insert_cut_list_table`.

## Notas de implementacao

### Arquitetura
- Todas as chamadas COM rodam em uma unica thread dedicada (COM/STA).
- Timeout de 120s por operacao COM; limpeza automatica no shutdown.
- Late binding (dispatch dinamico) -- igual ao ambiente do Claude Desktop.

### Descoberta do SolidWorks
- `_find_solidworks_exe` le `HKLM\SOFTWARE\SolidWorks\SOLIDWORKS <ano>\Setup\
  SolidWorks Folder` e cobre o layout novo de pastas (`...\SOLIDWORKS\` sem ano,
  e `SOLIDWORKS (2)` para instalacoes lado a lado).

### Planos localizados
- Nomes de planos padrao sao resolvidos pela posicao na arvore (1o/2o/3o
  `RefPlane`), nao por texto em ingles.

### Unidades
- Distancias/raios sao convertidos para metros antes de qualquer chamada COM.

### Proximos passos para "producao completa"
As ferramentas EXP sao os proximos alvos. As mais impactantes:
1. `insert_drawing_view` (desbloqueia toda a prancha de desenho -- categoria E).
2. `create_weldment_profile` (estruturas de plataformas/tanques).
3. `create_base_flange` (tanques de chapa).
Recomenda-se validar cada fluxo gravando uma macro VBA na versao alvo e
espelhando a sequencia exata de selecao/chamada COM.
