# Plan maestro — Team 15 · The Bazaar · v3 (tras auditorías 1 y 2)

Objetivo único: **terminar #1 en la clasificación final** (domingo, cuando `schedule` marque `end_round`).

---

## 0. Modelo de puntuación (verificado con datos)

| Bloque | Peso | Qué mide | Notas de calibración |
|---|---|---|---|
| Negotiating | 30 | duelos (% de la tarta) · ladder (% del rango de precio de cada dealer, **3 mejores por nivel y ronda**, faltantes = 0, niveles altos pesan más) · valor P2P ganado **a nuestros valores privados** | **Relativo al líder**: líder = 30 exacto; nosotros 10,35 con `neg_points` 37 ⇒ líder ≈ 107. El P2P (sin techo) fija el denominador. |
| Market-making | 30 | Market Test (eficiencia entre límites **reales**; puesto gratuito = ½; máximo = media top-3) · valor creado entre otros equipos en nuestro mercado | Cada sesión cuenta tu mejor mercado abierto. Un mercado `board` sin broker = **0** (peor que el puesto). |
| Jueces | 40 | Ideas y oficio | El bloque mayor: entregables explícitos (§6). |

- Rondas: **viernes (½) sigue activa** (fase 0,646, banco de 3,0 h pendiente) · sábado (1) · domingo (1). Final = (0,5·V + S + D)/2,5. Una ronda nueva “crece” con la fracción del día jugada.
- Bancos: sábado 8 sesiones (5,7,9,11,13,15,16-difícil,17) ⇒ cada una ≈ 1/8 de la parte MM del sábado; **domingo solo 2 (19, 21) ⇒ cada una vale ~4× una del sábado.**
- Valor privado: `book × afinidad × marginal(copia)` + **bonus de página** (enorme: con LAT completa cada primera copia LAT vale ≈ 99 P en vez de 13). Fuente de verdad = `GET /api/me/value?card=` en vivo, nunca la fórmula.
- Afinidades: **RET 1.6**, LAT 1.3, MAL 1.1, CHA 0.9, LAV 0.7, SAL 0.5.
- Nunca puntúa: nº de trades, comisiones, suerte de sobres, regalos, grants. Regalar valor sistemáticamente a otro equipo = tratos anulados + posible penalización.

## 1. Lecciones del viernes
1. Supuestos de esquema sin verificar (dealers, unlocked, duelos, límites, makers…) → módulos muertos en silencio.
2. Sin guardarraíl de valor: sobres a 30 con EV privado ~12; aceptación hasta 1,2× `expected_book` público.
3. Aceptar en la apertura del dealer → ladder ≈ 0.
4. Segundo agente (Antigravity) con nuestra clave.
5. Duelos del viernes = sesión de práctica (no puntuaba), pero el motor sigue roto.

## 2. Inteligencia (resumen; detalle en `intel/market.db`)
- **Duelos**: 15/18 rivales que hablan ceden por tiempo sin necesitar respuesta (p. ej. 122→82); 2 “ofertas explosivas” empeoran; 6/24 rivales nunca hablan; algunos escenarios sin zona de acuerdo. El viernes era práctica: el código rival cambiará.
- **Abuela**: sobre 30→19–23 (final); compra comunes 5→6 (final); vende comunes 10–12, poco comunes 25–29. **Chato**: compra poco comunes 13→15 (final), vende poco comunes 26–29, raras ~85–93; memoria .9 / estricto .85 ⇒ castiga repetir.
- **Top-6**: mensajes vacíos + precio, pasos de 1–2 P en ticks distintos, apuran hasta `final:true`; t13/t12 ya tienen mercados `board`; t04/t13 nos perfilaron por “páginas”.
- **Demanda por sets**: t08/t18 → SAL; t04/t08 → LAV; t05/t17 → MAL; t18/t04 → LAT.
- **Mercados de líderes**: v01 (t06), v02 (t12), v03 (t13) ⇒ **nunca operar allí** (les regalaríamos MM). Usar Rastro o v04 (t02, colista).

## 3. Estrategia por fuente

### 3.1 Market-making (30)
1. Viernes residual (banco 3,0 h): sin caja para fianza ⇒ puesto gratuito (½).
2. **Abrir mercado `board`, 0 % comisión, antes del banco de 5,0 h**, financiado con ventas previas (duplicados/SAL/LAV) para no quedar a 14 P. **Mantenerlo todo el fin de semana** (los bancos del domingo valen ~4×); el bond vuelve al final.
3. Broker en capas, mismo proceso:
   - **Suelo** = `bench_plan` + `public_plan` del starter (garantiza ½).
   - **v2 (sombra primero)**: trayectoria de cada trader → límite estimado (asíntota geométrica para los que relajan; cotización ± margen aprendido para firmes); emparejamiento por lotes: compradores por valor ↓, vendedores por coste ↑, solo q* intramarginales; impacientes primero, pacientes 1–3 ticks; precio irrelevante para la nota (cualquiera dentro de cotizaciones). Activar v2 solo cuando en replay/sombra supere al suelo.
   - Watchdog: si el broker no lee el libro en 2 ticks o v2 no casa nada en 2 ticks con cruces disponibles ⇒ suelo.
   - Grabar cada libro (`bench_offers`) para entrenar y para jueces.
4. Anti-abuso: no casar ida y vuelta entre el mismo par de makers; registrar todo.
5. Domingo: **congelar** la versión ganadora del sábado; nada de despliegues antes de los bancos de 19 y 21.

### 3.2 Duelos
Política híbrida (código determinista, nunca fuera de límite):
- **Aceptar ya** si la oferta rival deja ≥ 30 % del límite de margen o es una “oferta explosiva” buena; si el rival empeora o se planta 3 ticks y está dentro de límite.
- **Silencio** mientras el rival cede (su concesión por tick > decay × margen restante).
- **T−4 sin oferta rival** ⇒ enviar una oferta propia (límite ∓ 25 %); un solo mensaje; reevaluar.
- **Últimos ticks**: aceptar la mejor oferta dentro de límite; nunca acabar sin trato si existe uno dentro de límite.
- Duelos II/III (precio+días): utilidad con `your_days_weight`; aceptar por utilidad total; hablar solo si la ganancia esperada por días > decay × tarta; MESO de 2–3 paquetes equivalentes; inferir peso del rival por el issue que menos mueve.
- Prior por ítem a partir de nuestros límites históricos (escenarios repetidos).
- Verificaciones en vivo (6 duelos `live` de práctica con deadline 168): etiqueta propia en `messages.from`, cómo cuenta `rounds`, si `duel_accept` consume la aceptación del equipo.

### 3.3 Árbitro de aceptaciones (1/tick/equipo)
Cola única de intenciones por tick con prioridad: (1) duelo con deadline ≤ 2 ticks, ordenando **primero los de menor pendiente de concesión** y dejando para T−1/T−2 los que más ceden; (2) `final:true` de dealer; (3) duelo bueno; (4) P2P. **Ventanas de duelos (oleadas): sin regatear con dealers en los últimos 6 ticks.**

### 3.4 Ladder de dealers
- Por ronda y dealer: 3 tratos negociados hasta `final:true` (curva Boulware β≈0,2, paso 1–2 P, nunca repetir precio ni texto; mensajes cortos y variados o vacíos).
  - Abuela: vender comunes duplicados (tenemos ~7–10).
  - Chato: vender solo LAV/SAL (SAL-06, LAV-08…) o **comprarle RET** poco común/rara por debajo de nuestro valor (RET poco común ≈ 40, rara ≈ 112 para nosotros).
- Nunca comprar sobres salvo EV privado (recalculado con RET) > precio.
- **Primeros 3 tratos con Chato en la 1ª hora** ⇒ desbloqueo temprano del nivel 3. Al activarse un dealer nuevo: leer menú/rasgos automáticamente y cerrar sus 3 tratos de bajo riesgo primero (pesa más).
- Ronda 1 aún abierta: si al abrir el sábado t < 4,0 h, cerrar 3 tratos con Abuela y 3 con Chato **antes** de que cambie la ronda.
- Calibración en los primeros 10 ticks: registrar Δ`ladder_points`, Δ`neg_points` por trato para fijar pesos.

### 3.5 P2P a valores privados (fija el denominador)
- **Guardarraíl duro** (P2P): Δvalor = valor privado tras − antes − caja − comisión, con `value()` en vivo copia a copia; bloquear si Δ < +2. Dealers: bloquear si Δ < 0. No ajustable.
- **Nunca vender** cartas que rompan una página completa o casi completa.
- Prioridad 1: **completar la página RET** (≈ +245 de valor estimado): ofertas **dirigidas** (`to=`) e hilos con los equipos que tienen RET y no lo valoran; trueque con nuestras SAL/LAV/duplicados en vez de caja; pujas públicas solo con precios señuelo.
- Prioridad 2: completar MAL (faltan MAL-03/08/09/10).
- Vender duplicados y SAL/LAV a quien puja por ellos (t08/t18 SAL; t04/t08 LAV), a precios cercanos al mercado (no regalar excedente; ≤ 3 tratos asimétricos por equipo y día).
- Lugar: Rastro o v04; **nunca v01/v02/v03**.

### 3.6 Flags
Solo casos deterministas: un mensaje marcado como “última/final” seguido de una concesión mejor del mismo emisor, o texto contradictorio con `final:true`. Revisión humana hasta 2 aciertos.

## 4. Arquitectura (reescritura del esqueleto, estrategia portada)
```
bazaar/api/      client.py (token bucket 4 req/s, backoff 1→30 s con jitter, respeta 429/next_tick)
                 public.py (sin clave) · models.py (parsers estrictos: campo ausente ⇒ SchemaError)
bazaar/core/     snapshot.py · values.py (caché por (ref, copias), None ⇒ bloquear)
                 guard.py · executor.py (ÚNICO con POST: límites de clock.limits, árbitro, dry-run, STOP)
                 journal.py (SQLite WAL: intent → resultado → Δscore)
bazaar/strategy/ dealers.py · duels.py · p2p.py   (snapshot → [Intent], funciones puras)
bazaar/broker/   main.py (suelo starter + v2 sombra) · bench.py
bazaar/intel/    collector.py (sin clave)
ops/             supervisor (launchd KeepAlive), heartbeats, alertas, caffeinate
tests/           fixtures reales (market.db, duels_session1.json, respuestas en vivo)
```
- Procesos: `agent` (único con clave de equipo) · `broker` (clave de broker) · `collector` (sin clave) · `supervisor`.
- Ciclo por tick: detectar tick nuevo → 4 GETs (`me`, `duels`, `my_threads`, `my_offers`) → decidir → POST en orden aceptación → mensajes → listados; cortar a `tick_seconds − 3 s`.
- Calendario por `schedule.at_hours` vs `clock.t_hours`, nunca por hora de pared.
- Recuperación: al arrancar, reconstruir desde servidor + diario; no repetir precios en hilos adoptados.
- Seguridad: clave solo por entorno; detector de segundo agente (mensajes/ofertas `t15` desconocidos ⇒ alerta + STOP); dry-run en sombra antes de activar; tests de: parsers con datos reales, guardarraíl bloquea sobre a 30, 1 aceptación/tick, no comprarnos a nosotros (pseudónimos).

## 5. Automejora (acotada)
- Atribución por deltas de `score.{duel_points, ladder_points, neg_points, bench_points, mm_points}` en el tick de liquidación (descartar ticks ambiguos).
- Lista blanca de parámetros con rango, máximo ±10 %/h, uno por fuente, ≥ 5 observaciones, reversión automática. Guardarraíl y límites fuera. Congelado desde domingo 13:00.
- Modelos que se actualizan solos: concesión por dealer y tipo de artículo; perfiles de rivales de duelo por alias; límites de traders del banco; demanda por set y equipo.

## 6. Jueces (40) — entregables
1. README con arquitectura y diagrama. 2. Diario de decisiones legible (por qué, Δvalor). 3. Simulador/replay de duelos y banco con métrica vs starter. 4. Panel en vivo (puntos por componente, libro, duelos). 5. Post-mortem honesto del viernes (lección: esquema verificado). 6. Ideas: valoración marginal por página, broker estimador de límites, árbitro de aceptaciones, silencio estratégico medido. 7. Mercado y mensajes en español cuidados. 8. Tests con datos reales, sin secretos en el código. 9. Pitch de 3 min con la curva de puntuación.

## 7. Orden de construcción (MVP estricto)
| Prioridad | Listo para | Qué |
|---|---|---|
| P0 | Sáb 08:50 | api/client+models, values, guard, executor+árbitro+journal, supervisor; dealers (ventas Abuela + 3 tratos Chato); duelos híbridos; P2P venta dirigida; abrir sobre; collector sin clave |
| P0 | antes banco 5,0 h | mercado `board` + broker suelo + grabación de libros + watchdog |
| P1 | antes Duelos I (6,5 h) | duelos verificados con los 6 duelos de práctica; replay de los 24 grabados |
| P2 | 12:00–17:00 | broker v2 en sombra → activo si gana; Boulware; compra RET dirigida/trueque |
| P3 | antes Duelos II (13,0 h) | utilidad precio+días, MESO |
| P4 | antes banco difícil (16,0 h) | perfiles de traders firmes |
| P5 | sáb noche | ajuste ticks 15 s, congelación domingo, entregables jueces |
Recortado si falta tiempo: automejora automática (se hace manual con el diario).

## 8. Acciones humanas necesarias
- **Cerrar Antigravity** (o cualquier otro agente con la clave) antes de las 08:50.
- Mac enchufado, tapa abierta, red estable (DNS 1.1.1.1; hotspot de respaldo).
- Decidir si se pide a la organización rotar la clave.

---
## 9. Cambios v3 (auditoría 2) — prevalecen sobre lo anterior
1. **Bonus de página = 25 % del valor base de la página** (LAT: 86,1), no 2,5×. El `your_value` de cada carta de una página completa incluye el bonus entero = lo que perderíamos al quitarla. RET completa ≈ 424 + 106. Comprar a dealers **no** suma valor privado a la puntuación (solo ladder); la carta que cierra una página, por P2P. Medir en el primer P2P del sábado si Δ`neg_points` sigue a Δvalor antes de gastar > 60 P en RET.
2. **Duelos**: aceptación inmediata si el margen ≥ 60 % del límite (replay viernes: 98 % del máximo posible frente a 77 % con 30 %); si no, silencio y aceptación en el último tick seguro, escalonando 1 tick extra por cada duelo de la misma ola; si en T−5 no hay oferta rival, una sola oferta propia (comprador 0,70·L, vendedor 1,30·C). Verificar en la primera ola si `duel_accept` consume la aceptación del equipo.
3. **Sin hilos con dealers ni aceptaciones P2P mientras haya duelos vivos** (la ola es dueña de la aceptación por tick).
4. **Mercado propio en t = 6,85** (antes del banco de 7,0), reservando 275 P desde la apertura; respaldo del broker en proceso separado.
5. **Pasos de regateo por dealer**: Chato rara +4, poco común +2, venta −3; Abuela común +1, poco común +2, venta −2; nada tras `final:true` salvo aceptar o irse.
6. **Comisiones**: el Rastro (5 % + 1) se come los comunes; preferir mercados de equipos no líderes a 0 % y ofertas dirigidas.
7. **Al arrancar**: cancelar ofertas propias heredadas que no pasaron por el guardarraíl; abrir el sobre de bienvenida solo con t ≥ 4,0.
8. **Calendario**: final de duelos en t = 23,0 (decay 0,10, 12 ticks); ladder del domingo cerrada antes de t = 22,5; el banco de 3,0 del viernes puede jugarse al abrir: el puesto gratuito no debe romperse.
