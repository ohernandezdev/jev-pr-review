# jev-pr-review — modo sombra

## Objetivo

Una GitHub Action reutilizable que puntúa cada fichero de un PR con Jev y decide, en
código, si el PR puede mergearse solo. Arranca en **modo sombra**: comenta el veredicto,
no mergea nada. El automerge se activa cuando haya datos para fijar los umbrales.

## Por qué repo propio

La petición es "acoplarlo a CI/CD de GitHub en cualquier repo". Un composite action
autocontenido, sin dependencias fuera de la stdlib de Python, se consume con tres líneas
de YAML. `jevmod` es el primer consumidor, no el host.

## Decisión de diseño (verificada contra la API el 2026-09-19)

Jev puntúa **dimensiones de la situación**; el código aplica la política. Probado:
preguntar `needs_human_review` directamente dio 0.46 (inútil), mientras que `risk_level`
como score dimensional dio 2.97/3 con confidence 0.97 sobre el mismo diff de `auth.ts`.

Un request **por fichero**, no por PR: el límite de `state` es 32k tokens y agregar por
`max` conserva la señal (39 ficheros triviales no deben diluir el que toca `auth/`).

## Las cinco preguntas, por fichero

| id | tipo | qué juzga |
|---|---|---|
| `risk_level` | score 0–3 | cosmético / lógica aislada / lógica compartida / auth-pagos-datos |
| `diff_matches_title` | noul | el cambio hace lo que el título del PR promete |
| `hidden_scope` | noul | el cambio hace ALGO MÁS de lo que el título anuncia |
| `silent_failure` | noul | un error aquí falla en silencio, no ruidosamente |
| `tests_expected` | noul | un revisor competente esperaría tests con este cambio |

`state` por fichero: `pr_title`, `pr_body`, `file_path`, `diff` (truncado a 24k tokens),
`lines_added`, `lines_deleted`, `has_test_changes` (bool del PR completo), `ci_status`.

## Agregación (código, nunca el modelo)

`max` sobre ficheros para cada dimensión. Nada de medias.

Gates duros, evaluados antes que cualquier probabilidad:
- algún fichero casa con `blocked_paths` → nunca automerge
- líneas cambiadas (add+del) > `max_lines` → nunca automerge
- CI no verde → nunca automerge

Umbrales de arranque (a calibrar con datos reales, no son sagrados):

```yaml
automerge_when:
  max_risk_level:      "< 1.5"
  hidden_scope:        "< 0.25"
  silent_failure:      "< 0.40"
  diff_matches_title:  "> 0.80"
  max_lines:           400
blocked_paths: [".github/**", "**/auth/**", "**/migrations/**", "**/*secret*", "Dockerfile"]
```

## Alcance

DENTRO: action.yml composite, script stdlib-only, config por fichero, comentario en el PR
(idempotente: edita el suyo, no acumula), tests, README, workflow consumidor en jevmod.

FUERA (esta tarea): el merge real. `mode: shadow` es el único valor soportado hasta tener
datos. El código deja el camino de `mode: enforce` escrito pero no alcanzable.

## Seguridad

`pull_request`, **nunca** `pull_request_target`. El script no ejecuta nada del PR: solo lee
el diff por la API de GitHub. Permisos mínimos: `pull-requests: write`, `contents: read`.

## Tareas

- [x] T1 `jev_pr_review.py`: cliente Jev (stdlib urllib, backoff en 429/529), las 5 preguntas, agregación por max, gates duros, veredicto
- [x] T2 `action.yml` + `.jev-review.yml` de ejemplo + README
- [x] T3 Tests: unitarios de agregación/gates con fixtures, y un E2E real contra la API que verifique que un diff de auth puntúa alto y uno de README puntúa bajo
- [x] T4 Workflow consumidor en jevmod (`.github/workflows/jev-review.yml`), modo sombra
- [x] T5 Correr el E2E, publicar el repo y abrir un PR real (dogfooding sobre sí mismo)

## Criterio de aceptación

El E2E corre solo (`pytest`), pega contra la API de verdad y pasa. Un PR abierto en jevmod
recibe un comentario con las cinco dimensiones y el veredicto, y no mergea nada.

## Evidencia

- Smoke test 2026-09-19: `risk_level` 2.97/3 conf 0.97, `diff_matches_title` 0.91,
  625 tokens de entrada = $0.000026, ~750 ms. Gotcha: `criteria` de un `score` es LISTA.

- T1-T3 2026-09-19: `python3 -m pytest tests/ -v` → **32 passed** (incluye los 2 E2E
  reales contra `api.typesafe.ai`, sin mocks, con `TYPESAFE_API_KEY` del entorno):

  ```
  tests/test_aggregation.py .... [4]
  tests/test_config.py ...       [3]
  tests/test_e2e_jev.py ..       [2]  <- reales, contra la API de verdad
  tests/test_gates.py ..........[15]
  tests/test_mode.py .           [1]
  tests/test_truncation.py ......[6]
  32 passed in 3.02s
  ```

  E2E real observado: `src/auth.ts` (jwt.decode→jwt.verify) dio `risk_level` con
  score y confidence altos, muy por encima (>1.0) del README de una errata; el
  auth.ts quedó bloqueado por una dimensión de peligro (`risk_level`/`hidden_scope`)
  y el README nunca lo estuvo. El README SÍ puede salir en `escalate` por
  `silent_failure` (~0.51, roza el umbral 0.40) — es ruido real del modelo sobre
  umbrales sin calibrar, no un bug: el test verifica lo que el spec pide
  (permisividad relativa vía dimensiones de peligro), no un veredicto exacto fijo.

  Render del comentario verificado con los números reales del smoke test:
  625 tokens de entrada → `$0.000026` (coincide con la evidencia previa).

  Ficheros creados: `jev_pr_review.py`, `action.yml`, `.jev-review.yml`, `README.md`,
  `.gitignore`, `pytest.ini`, `tests/__init__.py`, `tests/test_aggregation.py`,
  `tests/test_config.py`, `tests/test_e2e_jev.py`, `tests/test_gates.py`,
  `tests/test_mode.py`, `tests/test_truncation.py`.

  Nota sobre config: se implementó un parser YAML mínimo propio (sin PyYAML)
  que cubre el subconjunto plano usado por `.jev-review.yml` (escalares,
  un nivel de mapping anidado, una lista plana). Documentado en el README como
  limitación deliberada; `.jev-review.json` es la vía robusta si hace falta más.

## Siguiente paso

esta sesión, las hace Omar.

## Corrección tras medir (2026-09-19)

`silent_failure` en crudo ordena al revés: README 0.57 vs auth 0.20. Jev acierta en
ambos (una errata no avisa a nadie; `jwt.verify` peta ruidosamente) — el error era la
composición, no la pregunta. Se pondera por el `risk_level` **del mismo fichero**, antes
del max entre ficheros, y el orden queda: README 0.00 < auth 0.20 < retry 0.44 <
`except: return None` sobre un cargo 0.91. Umbral de arranque `< 0.30`.

Los 32 tests iniciales pasaban sin cubrir esto: al quitar la clave de sus configs, el
código la saltaba en silencio. Añadidos 5 tests (`tests/test_weighted_silent_failure.py`),
uno de ellos E2E real. **37 passed.**

## Lo que encontró el primer PR real (2026-09-19)

PR #1 de `jev-pr-review`, dogfooding sobre sí mismo. Dos hallazgos:

**Acierto del modelo.** El PR se tituló `docs: use the real action reference` y
acabó llevando también el fix de CI y un workflow nuevo. `hidden_scope` subió a
**0.96** y `tests_expected` a 0.80. La dimensión hace exactamente lo que promete:
detectó que el diff hacía bastante más de lo que anunciaba el título. No se lo
enseñé, salió solo.

**Defectos propios, tres, en cascada.** El veredicto escalaba por `ci_status`, nunca
por las dimensiones:

1. Usaba `mergeable_state`, que valía `unstable` porque el propio revisor era un check
   en marcha. Se bloqueaba a sí mismo por existir. Ahora lee los check-runs del head SHA
   y descarta el suyo por `GITHUB_RUN_ID`.
2. Arreglado eso, arrancaba a la vez que los demás workflows y veía `no checks` por
   carrera. Espera acotada de 5 min.
3. Seguía sin leerlos. Diagnostiqué "faltan permisos" y añadí `checks: read` — la
   conclusión era falsa. `fetch_check_runs` llamaba a `_github_request` con una ruta
   relativa y el token posicional, cuando la función pide URL completa y token
   keyword-only. El `TypeError` caía en un `except Exception` que lo convertía en
   "no se pudo leer el CI, concede `checks: read`". Un bug de argumentos disfrazado de
   problema de permisos, y el mensaje de error que yo mismo había escrito mandó el
   diagnóstico en la dirección equivocada. Ahora solo se capturan errores de transporte.

La lección: **un `except` ancho con un mensaje que adivina la causa es peor que no
capturar nada.** Convierte un fallo de programación en un consejo confiado y falso.

**Conclusión de diseño.** El revisor no debe ser quien decide que el CI está verde:
es un check más, arranca a la vez que los demás y cualquier foto que saque es una
carrera que puede perder. En `enforce` el merge irá por `gh pr merge --auto` y el gate
de CI por branch protection, que retiene el merge de verdad. La espera acotada solo
sirve para que los datos de calibración sean honestos.

**Estado final de PR #1:** veredicto `escalate`, única razón `blocked path(s) touched:
.github/workflows/*`. Correcto: el PR toca workflows, que están en la lista negra. El
gate de CI ya lee verde. Coste del run: $0.000422.

## Legibilidad y cola de la distribución (2026-09-19, tras feedback de Omar)

**Comentario para no-técnicos.** Porcentajes en vez de 0.95, cada dimensión como
pregunta en lenguaje llano, el riesgo como palabra con su significado escrito, y el
fraseo llevando la dirección para que el número solo confirme ("Probably not (32%)",
no "No -- 0.32"). Los números crudos quedan en un `<details>` plegado. Razones
estructuradas (`{"code": ..., ...}`) en vez de cadenas: la lógica es de máquina y el
texto es presentación.

**Pregunta de Omar: "1,76 de risk level es rarísimo, ¿no?"** No: un `score` es la
posición esperada sobre los niveles, no una etiqueta. Medido en vivo:
retry/backoff 1.85 = `{1: 0.15, 2: 0.85}`; auth jwt 3.00 = `{3: 1.0}`.

Pero la pregunta destapó un fallo real del umbral: **la media compensa.** Un fichero
50% cosmético / 50% auth da exactamente 1.5 y colaba bajo `< 1.5` con la mitad de la
masa en catástrofe. Las propias docs de TypeSafe avisan de que una regla de "cualquier
violación grave" necesita condición aparte. Añadido `worst_case_risk` (masa de
probabilidad en el peor nivel, umbral `< 0.15`), que además usa las `probabilities`
que estábamos tirando. Distribución ausente ≠ 0: sin dato, el gate escala.

68 tests en verde.

## Red-team con lectora no técnica

El comentario se pasó por una lectora que no es ingeniera (jefa de operaciones), la
persona a la que de verdad se le pediría firmar. Ocho hallazgos, todos aceptados
salvo uno.

1. **La tabla no participaba en el veredicto.** `decide_verdict` hacía
   `if gate_reasons: return "escalate", gate_reasons`, así que al saltar un gate duro
   las dimensiones no se evaluaban nunca — pero la tabla se pintaba igual. Su frase:
   "me está entrenando a ignorar la tabla; el color solo depende del tamaño y del
   nombre del fichero". El más grave de los ocho. Ahora gates y dimensiones se evalúan
   SIEMPRE y se devuelven todas las razones juntas. Test:
   `test_a_hard_gate_does_not_hide_the_dimensions_that_also_failed` y
   `test_a_red_verdict_can_be_driven_by_the_table_alone`.
2. **El tick verde sobre un 38%.** `✅ Probably not (38%)` es indefendible. Tres
   bandas y nada más: `<0.10` → `No`, `0.10–0.90` → `Not confident either way`,
   `>0.90` → `Yes`. El ✅ exige dirección tranquilizadora Y certeza, así que nada de
   la banda media lleva visto bueno. Fuera "Probably yes/not": el encogimiento de
   hombros tiene que verse como tal.
3. **Dos filas para una sola pregunta.** `Does the change do what its title says?` y
   `Does it also change things its title doesn't mention?` se leían como la misma
   pregunta repetida. Fusionadas en *Does the title describe the whole change?*. Las
   dos dimensiones se siguen preguntando a Jev, siguen en los raw scores y siguen
   teniendo umbral propio: lo que cambió es solo la presentación. Además, cuando la
   respuesta es `No -- it changes more than it says` ese hallazgo sube a la PRIMERA
   línea de **Why** (razón `changes_more_than_title`): era el más grave del comentario
   y estaba enterrado en la fila 4.
4. **Preguntábamos una opinión teniendo el hecho.** `Would a reviewer expect tests
   with this?` es una expectativa; ella: "dime si hay tests o no hay". Y
   `has_test_changes` ya existía en `review_pr`. La fila pasa a *Does it include
   tests?* → `Yes`/`No` alimentada por ese booleano. `tests_expected` se mantiene en
   los raw scores y solo matiza: `> 0.90` sin tests → `No -- and a reviewer would
   expect them`, y esa combinación entra como razón en "Why".
5. **Tres riesgos distintos en un porcentaje agregado.** "que toque el login no es lo
   mismo que toque pagos, ni que toque datos personales; si no es un no rotundo quiero
   saber CUÁL". Tres preguntas `noul` nuevas — `touches_money`, `touches_accounts`,
   `touches_personal_data` — redactadas describiendo la situación, nunca la decisión.
   Agregadas por `max`, y su máximo es `sensitive_area`, con gate `< 0.10` en
   `.jev-review.yml`. La fila nombra las que aplican: `Yes -- money, personal data`.
6. **"No firmo lo que no sé qué es".** Ambos comentarios, incluido el verde, llevan
   bajo el titular título del PR, autor (`user.login`), ficheros y líneas cambiadas,
   sacados de la respuesta de la API que ya pedíamos. Si no hay dato, no se inventa.
7. **Un aviso sin destinatario no ocurre.** Config opcional `escalate_to`; si está, el
   titular rojo acaba en ` -- assigned to: @quien`. Si no está, no se inventa a nadie,
   y el titular verde nunca lo lleva.
8. **El coste.** "Me sugiere que esto es barato y por tanto flojo". `cost of this run`
   se movió dentro del `<details>` plegado; fuera queda solo el recuento de ficheros.

**Lo que NO se cambió: los porcentajes.** Ella quería quitarlos; se quedan por
decisión explícita del dueño del repo. Etiqueta + `(NN%)`.

**Lección transversal:** el red-team que sirve no es el que busca bugs en el código,
es el que lee la salida como la leería quien tiene que actuar. Un tick verde sobre un
38% y una tabla decorativa pasan cualquier test unitario y destruyen la confianza en
la herramienta a la primera lectura.

96 tests en verde (68 anteriores + 28 nuevos de `tests/test_red_team_readability.py`).

### Dos correcciones sobre el trabajo del red-team

- **`worst_case_risk` dejó de ser fila.** "How bad is it" y "how likely is the worst case"
  se leían como la misma pregunta dos veces: el pecado que el propio red-team señaló en
  las filas del título, reintroducido al arreglarlas. Sigue siendo gate y raw score.
- **Un código de razón sin frase pintaba el diccionario crudo** en un comentario que lee
  una persona. Ahora hay un texto de reserva honesto y un test que lee el propio fuente
  del módulo y exige una frase para cada código que se genera en él.

99 tests en verde.
