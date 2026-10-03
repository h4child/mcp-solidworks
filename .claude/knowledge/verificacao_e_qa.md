# Verificação e QA — antes de dizer "pronto"

Checklist real, não decorativo. Rode isso antes de responder ao usuário que
uma peça/montagem está terminada. Ver também o fluxo em `CLAUDE.md`.

## 1. A árvore de features reconstrói sem erro

`validate_model` — rebuilda e relata erros/avisos. SolidWorks às vezes
"sucede" com uma feature internamente suprimida/com erro — não confie só no
retorno `True` de uma chamada anterior, rode `validate_model` de fato.
`list_features` com atenção ao campo `suppressed`/`error_code` de cada
feature confirma o que `validate_model` resumiu.

## 2. Massa e dimensões batem com a expectativa

`measure_body` depois de cada feature estrutural significativa, não só no
final — um erro de escala (mm vs m, ou um sketch 10× maior por engano)
aparece imediatamente na massa, e é muito mais barato corrigir uma feature
atrás do que destrinchar o erro no final.

Teste de sanidade rápido: `massa = volume × densidade`. Se a peça é maciça
(sem `shell_body`), `volume_m3 × densidade_do_material` deve bater com
`mass_kg` dentro de 1-2%. Se não bate, o material errado foi atribuído ou a
geometria não é o que você imagina.

## 3. Inspeção visual bate com a referência

`capture_standard_views` (ou `zoom_to_fit` + `set_view` + `capture_viewport`)
e compare proporção e silhueta com a foto/desenho/descrição original. Preste
atenção especial a:

- Proporção entre as dimensões principais (uma peça "parece" certa mesmo com
  erro de escala absoluto, se as proporções relativas estiverem erradas isso
  aparece no olho).
- Simetria, quando a peça deveria ser simétrica — erro de offset de sketch é
  comum e salta aos olhos numa vista frontal/superior.
- Features que deveriam existir e não existem (furo esquecido, chanfro
  esquecido).

## 4. Montagem: interferência e graus de liberdade

`interference_check` antes de considerar uma montagem pronta — peças que se
sobrepõem fisicamente não é um erro que o SolidWorks bloqueia sozinho ao
posicionar com `set_component_transform`/mates.

Confirme também que todo componente tem posição definida — ou fixo
(`fix_component`) ou totalmente restringido por mates (`list_mates`). Um
componente com graus de liberdade sobrando "flutua" na montagem real mesmo
que pareça no lugar certo na vista atual.

## 5. Checklist de fabricabilidade (DFM) antes de fechar

Ver `processos_de_fabricacao.md` pros números — mas a pergunta de triagem é:
*alguém consegue fabricar isso com o processo pretendido, do jeito que está
modelado?* Raio de canto interno existe onde a ferramenta exige, parede não
está fina demais pro processo, rosca/furo está na tabela padrão (ver
`elementos_de_maquina.md`), chapa dobrada respeita raio mínimo e flange
mínimo (ver `chapa_metalica.md`).

## 6. Quando o usuário pede "confirma que aguenta a carga"

Este MCP **não tem FEA**. Para uma resposta honesta:

1. Calcule à mão com a teoria aplicável — viga em flexão (`σ = M·c/I`),
   coluna em flambagem (Euler, se esbelta), pressão de contato, etc.,
   usando as dimensões e o material reais do modelo (`measure_body` dá área,
   `materiais.md` dá o escoamento do material atribuído).
2. Compare a tensão calculada com o escoamento do material e aplique um
   fator de segurança — **2 a 3 pra carga estática bem conhecida, 4+ pra
   carga dinâmica/fadiga ou incerteza relevante sobre a carga real.**
3. Deixe claro na resposta que é um cálculo analítico simplificado, não uma
   simulação — e recomende validação por simulação/ensaio real antes de
   produção se a aplicação for crítica (segurança, carga de pessoas,
   certificação regulatória).

## 7. Antes de `save_document`/entregar

- `get_custom_properties` preenchido com o que o usuário/processo downstream
  precisa (material, código, descrição — ver o backend Alfa Detail AI, que
  lê exatamente essas propriedades).
- Nome do arquivo e localização fazem sentido (não ficou em "Peça1.SLDPRT"
  default se o usuário pediu algo específico).
