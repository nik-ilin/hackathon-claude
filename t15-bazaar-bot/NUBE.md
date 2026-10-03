# Sesiones en la nube (Claude Code on the web) sincronizadas con el repo

## Configuración única

1. En un terminal con `gh` autenticado (cuenta con permiso de escritura en `nik-ilin/hackathon-claude`), abrir `claude` y ejecutar `/web-setup`. Así las sesiones en la nube usan ese acceso de GitHub y pueden hacer push y abrir PR.
2. Alternativa del dueño del repo: instalar la app de GitHub de Claude en el repo (https://github.com/apps/claude/installations/new).
3. Comprobar: `gh repo view nik-ilin/hackathon-claude` funciona.

Sin este paso, `claude --cloud` sube una copia (bundle) del repo: la sesión trabaja sin remoto `origin`, no puede hacer push y `--teleport` no trae los commits.

## Lanzar una tarea

```bash
cd hackathon-claude      # clon con origin = github.com/nik-ilin/hackathon-claude
git checkout main && git pull
claude --cloud "Tarea… Todo offline: PROHIBIDO llamar a la API real o usar claves. Cambios opt-in con flags, todos los tests en verde. Rama nueva, push y PR contra main en español."
```

- `claude --cloud` necesita un terminal interactivo.
- La red de la nube no llega al servidor del juego (y no debe): las tareas son de código y simulación offline.
- Seguimiento: https://claude.ai/code. Si la sesión dejó la rama subida, `claude --teleport <session-id>` la trae a local.

## Ciclo de mejora

1. En local, `intel/profiler.py` (cada 5 min) escribe en `intel/REPORT.md` la sección «Bucle de automejora»: distancia al 1.º, palancas ordenadas por puntos y `self_improve.dealer_params`, además de los cambios en mercados.
2. Cada palanca nueva se convierte en una tarea `claude --cloud` (offline, con tests).
3. La sesión abre un PR; se revisa y se fusiona en `main`.
4. El bot del equipo arranca desde `main` con los flags que recomiende el PR.
