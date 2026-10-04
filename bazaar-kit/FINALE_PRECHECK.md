# Preflight de Duelos III · Team 15

Usar desde el checkout operativo de Nikola, después de actualizar la rama aprobada. Este chequeo es solo lectura; el dry-run no envía ofertas ni acepta duelos.

## Prompt para Claude Code

> Prepara el agente de Team 15 para la final sin operar todavía. En `bazaar-kit`, verifica la rama y los cambios descargados; confirma que existe `.env` y que `BAZAAR_KEY` está definida sin imprimirla ni mostrar el contenido del archivo. Comprueba hora/tick del servidor, duelos vivos y deadlines, procesos que usen el agente y `data/agent.lock`. Ejecuta `./run.sh duels --day3` en modo dry-run y revisa que cada aceptación proyectada esté dentro del límite privado, tenga excedente total positivo (incluidos los días) y que se respete una aceptación por tick. Resume configuración, acciones propuestas, duelos omitidos, avisos y si queda otro agente activo. No uses `--learn`, no borres el lock manualmente y no ejecutes `--execute` durante este preflight. Si hay un proceso operativo activo o una comprobación falla, detente y explica cómo resolverlo.

## Arranque tras el preflight

Solo cuando el preflight confirme que no hay otro coordinador usando `data/agent.lock`, detener el proceso anterior de forma limpia y ejecutar:

```bash
./run.sh duels --execute --day3
```

`--day3` activa negociación con días, ladder para rivales mudos, perfiles, reconciliación de escrituras ambiguas y verificación justo antes de aceptar. La política nunca acepta fuera del límite o con excedente neto no positivo. El aprendizaje sigue apagado porque el replay temporal de esta versión cambió 0 decisiones en 36 duelos de validación/prueba y la simulación de semillas separadas no mostró ventaja.
