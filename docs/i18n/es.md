# duet — dos modelos de IA lo construyen; el arnés comprueba las reglas

> Esta página es una introducción breve traducida del README en inglés. **La referencia es el [README en inglés](../../README.md)**; el registro completo de cada ejecución está en [docs/QA.md](../QA.md).

Un solo agente de IA corrige sus propios deberes y dice que aprobó. duet añade un **segundo modelo**, **ejecuta tus tests por sí mismo** y somete cada flujo de trabajo a una regla que comprueba contra lo que realmente ocurrió, no contra lo que dice cualquiera de los dos modelos.

## Instalación

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

O, dentro de Claude Code, como plugin:

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

Usa las suscripciones de Claude y ChatGPT que ya tienes — **sin claves de API**. Con una sola suscripción también funciona (por ejemplo `--pair claude:opus+claude:sonnet`).

## Nueve comandos, cada uno con una regla comprobada

```bash
duet build "una app web de gastos"        # desde cero: criterios, luego tests, luego código
duet fix "la exportación pierde filas con comas" # los tests deben fallar en el código original y pasar ahora
duet add "una opción --json en report"    # un test debe fallar sin la funcionalidad
duet refactor "dividir parser.py en dos"  # los tests existentes no pueden cambiar ni un byte
duet plan "migrar el almacenamiento a Postgres" # se discute y solo puede cambiar PLAN.md
duet review                               # otro modelo revisa tu diff en modo solo lectura
duet make "…" --like URL                  # investiga las referencias, construye, mide la paridad, sala limpia
duet jury                                 # usuarios, clientes, inversores y jueces simulados; cada punto citado
duet page check index.html                # la puerta de un sitio web: lo que se ve mal en escritorio y móvil
```

`fix` **vuelve a ejecutar los tests finales sobre una copia del código original**. Si también pasan con el código con el bug, no detectan el bug, y se anulan las dos aprobaciones.

## Resultados

Con una sola frase y un directorio vacío, duet construyó una app web de gastos (interfaz de una página, API JSON, SQLite, exportación CSV) en **35 minutos**. Después la atacamos a mano: la inyección SQL se guardó como texto, el XSS almacenado se mostró como texto en un navegador real, la inyección de fórmulas CSV quedó neutralizada y `0.1 + 0.2` sumó exactamente `0.30`.

## Hacerlo el predeterminado

```bash
duet skill default    # deshacer: duet skill undefault
```

A partir de ahí, Claude Code, Codex, Gemini CLI, Antigravity y OpenCode pasan a duet los cambios de código reales (corregir bugs, añadir funcionalidades, refactorizar), y siguen respondiendo directamente las preguntas y los cambios de una línea.
