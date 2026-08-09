# SolidWorks MCP Server (novo)

Servidor MCP em Python que controla o SolidWorks via COM (`win32com`), escrito
com o SDK oficial (`mcp`, usando `FastMCP`). **104 ferramentas** (v4.8.0).

## Instalacao

```bash
pip install -r requirements.txt
```

## Configuracao no Claude Desktop / Claude Code

Instale o `.mcpb` (duplo clique) ou adicione em `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:\\caminho\\para\\solidworks-mcp-novo\\server.py"]
    }
  }
}
```

Abra o SolidWorks (opcional -- o servidor consegue abrir sozinho) e peca para o
Claude "conectar ao SolidWorks".

## Status de verificacao (testado ao vivo no SolidWorks 2025, PT-BR)

Cada ferramenta foi executada contra uma sessao real do SolidWorks. Legenda:
- OK  = criou o recurso com sucesso no teste ao vivo.
- EXP = experimental: a assinatura COM esta correta mas o recurso depende de
        selecao/estado especifico ou de uma parte da API que se comporta de
        forma inconsistente nesta versao; pode exigir ajuste manual.

### Conexao / Documentos / Utilidades -- OK
`connect_solidworks`, `get_solidworks_info`, `create_new_part`,
`create_new_assembly`, `create_new_drawing`, `open_document`, `close_document`,
`save_document`, `get_document_info`, `list_open_documents`, `set_units`,
`set_view`, `zoom_to_fit`, `zoom_to_area`, `execute_python`

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
`add_mate` (EXP), `add_advanced_mate` (EXP),
`create_assembly_pattern` (EXP -- precisa de referencia de direcao),
`create_exploded_view` (EXP)

### Propriedades / Medicao / Exportacao / Configuracoes -- OK
`get_custom_properties`, `set_custom_property`, `measure_body`,
`export_document`, `create_configuration`,
`switch_configuration` (resolve nomes localizados, ex. "Valor predeterminado")

### Perfis estruturais (weldments) -- parcial
`create_3d_sketch` (OK), `draw_line_3d` (OK). O caminho da biblioteca de perfis
agora e resolvido corretamente em
`C:\ProgramData\SOLIDWORKS\...\weldment profiles\<padrao>\<tipo>.sldlfp`
(o tamanho e uma *configuracao* dentro do arquivo).
`create_weldment_profile` (EXP), `trim_extend_structural` (EXP),
`add_gusset` (EXP), `add_end_cap` (EXP) -- a API de membro estrutural
(`InsertStructuralWeldment5`) e sensivel via COM; a geometria pode precisar ser
inserida pela interface (Weldments > Structural Member).

### Chapa metalica -- parcial
`create_base_flange` (EXP -- erro de tipo de argumento corrigido; o parametro
PCBA exige um VARIANT de dispatch, nao bool), `add_sheet_metal_bend`
(EXP -- usa Insert Bends), `add_sheet_metal_edge_flange` (EXP -- requer objetos
de aresta), `flatten_sheet_metal` (OK quando ha corpo de chapa).

### Roscas / usinagem -- parcial
`add_cosmetic_thread` (EXP), `add_thread_feature` (delega para rosca cosmetica --
roscas 3D reais NAO sao expostas pela API do SolidWorks),
`create_helix` (EXP -- `InsertHelix` retorna None via COM nesta versao),
`create_knurl` (EXP -- Wrap+Deboss).

### Desenho tecnico detalhado -- parcial
`add_drawing_annotation` (OK). As demais sao EXP e dependem de uma vista de
modelo existente e/ou do gerenciador de propriedades:
`insert_drawing_view` (`CreateDrawViewFromModelView3` retorna None mesmo com o
modelo carregado -- em investigacao), `insert_section_view`,
`insert_detail_view`, `insert_broken_view`, `insert_auxiliary_view`,
`add_drawing_dimension`, `add_centerline`, `add_weld_symbol`,
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
