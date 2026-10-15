# BUGS_AND_FIXES.md

Cada entrada segue o template da seção 7.5 do pacote de auditoria
(`07_MATRIZ_DE_EVIDENCIAS_E_INVENTARIO.md`).

---

## BUG-01 — Chamada travada após timeout derruba todas as chamadas seguintes com mensagem enganosa

- **Severidade:** Média (robustez/diagnóstico; não corrompe dados, mas torna
  o servidor inteiro inutilizável até a chamada travada terminar, e esconde
  a causa real).
- **Status:** CONFIRMADO e CORRIGIDO.
- **Commit base:** `668f3105fe49196720fba35fe4c0ef72416eebac`.
- **Arquivo e função:** `server.py`, função `_run` (em torno da linha 341).
- **Linhas atuais:** ver `git diff` — a função inteira foi reescrita; a
  lógica nova está entre a declaração de `_stuck_call` (logo antes de `_run`)
  e o `except Exception` final de `_run`.
- **Trecho mínimo (antes):**
  ```python
  async def _run(fn, *args, **kwargs):
      fn_name = fn.__name__ if hasattr(fn, "__name__") else str(fn)
      timeout = _timeout_budget_for(_current_tool_name.get())
      loop = asyncio.get_event_loop()
      future = loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
      try:
          result = await asyncio.wait_for(future, timeout=timeout)
          return result
      except asyncio.TimeoutError:
          raise RuntimeError(f"SolidWorks operation timed out after {timeout}s. ...")
  ```
- **Pré-condições:** uma chamada COM (real ou, no teste, uma função Python
  qualquer) bloqueia por mais tempo que seu orçamento de timeout.
- **Passos para reproduzir (sem SolidWorks):**
  1. Submeter ao `_executor` uma função que bloqueia por tempo indefinido
     (ex.: `threading.Event().wait()`), confirmando via um segundo `Event`
     que ela já começou a rodar na worker thread.
  2. Chamar `server._run(essa_funcao)` com um timeout curto — a chamada
     expira e levanta `RuntimeError("... timed out ...")`, mas a thread
     continua bloqueada de verdade.
  3. Chamar `server._run(outra_funcao_rapida)` imediatamente depois — ela
     nunca chega a rodar (fica atrás na fila do executor de 1 worker), mas
     **antes da correção** ela também expirava seu próprio timeout e
     reportava a mesma mensagem genérica, como se *ela* estivesse lenta.
- **Resultado esperado:** a segunda chamada deveria falhar de forma que
  deixasse claro que o problema é uma chamada anterior ainda presa, não ela
  mesma.
- **Resultado observado (antes da correção):** mensagem idêntica e
  igualmente genérica para as duas chamadas; nenhuma pista de que a segunda
  nunca foi executada.
- **Causa:** `asyncio.wait_for` só consegue cancelar o *future* do lado
  asyncio. Para um `concurrent.futures.Future` que já começou a rodar numa
  `ThreadPoolExecutor`, `cancel()` retorna `False` e a thread real continua
  — ela só é liberada quando a função bloqueante de fato retornar. Como o
  executor tem exatamente 1 worker, qualquer chamada posterior fica na fila
  atrás dela.
- **Correção:** `_run` agora:
  1. Submete direto ao `_executor` (`_executor.submit(...)`) em vez de
     `loop.run_in_executor`, guardando o `concurrent.futures.Future` bruto.
     Isso é necessário porque o *wrapper* asyncio retornado por
     `run_in_executor` se marca como cancelado **imediatamente** quando
     `wait_for` expira, independente do estado real da thread — um sinal
     falso de "já terminou" que inviabilizaria rastrear quando o trabalho
     real de fato acaba.
  2. Ao expirar, grava um marcador global `_stuck_call` (ferramenta, desde
     quando, orçamento) e registra um `add_done_callback` no future bruto
     para limpar esse marcador quando — e só quando — a chamada realmente
     travada terminar.
  3. No topo de `_run`, antes de submeter qualquer trabalho novo, verifica
     esse marcador; se presente, falha **imediatamente** (sem nem tentar
     rodar) com uma mensagem nomeando a ferramenta travada, há quanto tempo,
     e o motivo ("COM não pode ser cancelado com segurança uma vez em
     execução").
- **Testes:** `tests/test_timeout_lockup.py` (3 testes novos, todos passam):
  - `test_a_timed_out_call_leaves_the_worker_thread_running_and_is_remembered`
  - `test_a_later_call_fails_fast_with_a_clear_reason_instead_of_its_own_timeout`
  - `test_the_stuck_marker_clears_once_the_thread_actually_finishes`
- **Riscos restantes:**
  - Isto melhora o *diagnóstico*, não resolve a limitação de fundo: uma
    chamada COM genuinamente travada (ex.: diálogo modal do SolidWorks)
    ainda deixa o servidor inteiro bloqueado até ela terminar ou o processo
    ser reiniciado — não há forma segura de interromper uma chamada COM em
    andamento. A mensagem de erro agora deixa isso explícito para quem está
    operando.
  - Não implementa o contrato `indeterminate` mais amplo para escritas
    proposto no pacote de auditoria (marcar o documento como precisando de
    reconciliação) — isso mudaria o schema de retorno de todas as
    ferramentas de escrita.
  - **Validação ao vivo necessária:** não — este bug e sua correção são
    inteiramente no nível do executor Python/asyncio, não dependem de COM
    real. Os testes cobrem o comportamento real de `ThreadPoolExecutor` +
    `asyncio.wait_for` do Python, que é idêntico com ou sem SolidWorks.

---

## BUG-02 — `validate_model` anotado como somente-leitura apesar de forçar rebuild

- **Severidade:** Baixa (metadado incorreto; não há perda de dados, mas um
  cliente MCP pode decidir auto-aprovar/repetir a chamada assumindo que ela
  não tem efeito colateral).
- **Status:** CONFIRMADO e CORRIGIDO.
- **Commit base:** `668f3105fe49196720fba35fe4c0ef72416eebac`.
- **Arquivo e função:** `server.py`, função `validate_model` (em torno da
  linha 14129, antes da correção).
- **Trecho mínimo (antes):**
  ```python
  @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
  async def validate_model() -> dict:
      ...
      def _impl():
          doc = _active_doc()
          rebuild_result = doc.ForceRebuild3(False)
          ...
  ```
  Comparar com `rebuild_model`, poucas linhas antes, que faz a mesma chamada
  `doc.ForceRebuild3(...)` e está corretamente anotada
  `readOnlyHint=False`.
- **Pré-condições:** nenhuma além de ler o código-fonte — este é um bug de
  metadado estático, não depende de execução.
- **Passos para reproduzir:** comparar a anotação de `validate_model` com a
  de `rebuild_model` e com o corpo de `validate_model`.
- **Resultado esperado:** ferramentas que chamam `ForceRebuild3` devem ser
  anotadas de forma consistente.
- **Resultado observado (antes):** `validate_model` dizia `readOnlyHint=True`
  enquanto fazia exatamente o que `rebuild_model` (anotado
  `readOnlyHint=False`) faz.
- **Causa:** inconsistência introduzida quando `validate_model` foi escrito
  — provavelmente por ênfase em "é uma ferramenta de leitura/verificação",
  sem considerar que seu primeiro passo (`ForceRebuild3`) não é
  observação pura.
- **Correção:** anotação trocada para
  `ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)`,
  e docstring da ferramenta passou a documentar a mudança e o porquê.
- **Testes:** suíte completa (`tests/test_contract.py` não fixa o valor
  antigo para este tool especificamente) — roda sem alteração, confirmando
  que nenhum teste existente dependia da anotação errada.
- **Riscos restantes:** é uma **quebra de contrato deliberada**: um cliente
  que auto-aprovava `validate_model` por confiar em `readOnlyHint=True`
  passará a precisar de confirmação, exatamente como já precisa para
  `rebuild_model`. Nenhuma validação ao vivo é necessária — é um metadado
  estático, verificável por inspeção do código e dos testes de contrato.

---

## Bugs do pacote de auditoria investigados e NÃO reproduzidos (ver AUDIT_REPORT.md §5, F7)

Nenhum outro item do pacote de auditoria (02 a 07) produziu um bug
reproduzível e corrigível sem SolidWorks dentro do escopo desta rodada. Os
achados de documentação (cobertura da matriz OK/EXP, contagem de
ferramentas) estão descritos como F1/F6 no `AUDIT_REPORT.md` e corrigidos no
`README.md`, não neste arquivo (não são bugs de comportamento).
