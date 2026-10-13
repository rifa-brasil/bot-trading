# BOT DE INVERSIÓN — VERSIÓN 47


## Cambios de V47
- La captura del comprobante de depósito es obligatoria en los flujos `/depositar` y el flujo manual del teclado; se eliminó la opción de finalizar el depósito con `/skip`.
- La pantalla **Mi cuenta** separa el total de ganancias diarias del total acumulado por bonos de referidos (directos e indirectos). El saldo disponible sigue siendo el saldo acreditado existente.
- **Sostenibilidad Económica** permite cancelar el registro del fondo y volver al panel de administración.
- **Historial General** incorpora consultas separadas por ID para bonos de referidos directos e indirectos.
- Las consultas de pagos/cuotas diarios muestran una alerta y orientan al administrador a registrar primero el fondo del día cuando falta ese dato.
- El cálculo de sostenibilidad ya no descuenta los bonos directos/indirectos como salidas. La reserva del 3% cubre las ganancias diarias y estas no se descuentan por segunda vez; los retiros pendientes sí se descuentan del neto operativo.

## Contenido del paquete
- `bot.py` — código principal del bot.
- `requirements.txt` — dependencias.
- `images/` — imágenes utilizadas por el bot.
- `Guia de Usuario del bot de Inversion.pdf` — guía actualizada de usuario.
- `README_GITHUB_RENDER.md` — configuración básica para GitHub/Render.


## Cambios de V36
- Se añadió el botón principal **🚀 Invertir Plan** fuera del historial.
- Ese botón muestra exclusivamente los depósitos de planes que ya fueron **aprobados por el administrador** y todavía no han sido invertidos.
- Cada plan disponible conserva el botón **🚀 Invertir Plan X USDT** para activarlo.
- El mensaje que recibe el usuario al aprobarse un depósito ahora indica correctamente que debe usar **🚀 Invertir Plan** desde el menú principal.
- La lógica de inversión del depósito aprobado se mantiene en la misma función y no se modifica la regla de activación.

## Cambios de V37
- Se conserva el sistema de referidos directo existente (primer plan y segundo plan con los porcentajes configurados).
- Se añade un bono multinivel del 0,1% sobre el primer plan invertido de cada usuario elegible.
- El beneficiario recibe el bono cuando el usuario referido está entre 4 y 14 enlaces por debajo de él: nivel 1 recibe por niveles 5-15, nivel 2 por niveles 6-16, nivel 3 por niveles 7-17, y así sucesivamente.
- Cada pareja usuario referido/beneficiario se acredita una sola vez mediante una tabla de registro con restricción única.
- Los bonos multinivel se aplican tanto a la activación inicial de un plan depositado aprobado como a una reinversión únicamente si esa reinversión es la primera inversión registrada del usuario.
- El resumen de referidos suma las bonificaciones directas y multinivel.
- No se incluyen bases de datos de producción; la tabla nueva se crea automáticamente al iniciar el bot.

## Variables de entorno
- `TELEGRAM_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `USDT_TRC20_ADDRESS`
- `BACKUP_TIME` (opcional, `06:00`)
- `BACKUP_TIMEZONE` (opcional, `America/Sao_Paulo`)
- `PROFIT_TIMEZONE` (opcional, `America/Sao_Paulo`)
- `TARGET_MULTIPLIER` (opcional, `2.0`)
- `ADMIN_WALLET_URL` (opcional)
- `REFERRAL_BONUS_FIRST_RATE` (opcional, `0.03`)
- `REFERRAL_BONUS_SECOND_RATE` (opcional, `0.015`)

## Render
Build Command:
```bash
pip install -r requirements.txt
```
Start Command:
```bash
python bot.py
```

## Guía de Usuario
El botón del usuario **📘 Guia de Usuario del bot de Inversion** busca exactamente el archivo:
`Guia de Usuario del bot de Inversion.pdf`

No cambies ese nombre sin actualizar también `USER_GUIDE_PDF` en `bot.py`.

## Comandos exclusivos del administrador

### Fin de semana
- `/fin_de_semana_on` — habilita excepcionalmente la acreditación manual de ganancias los sábados y domingos.
- `/fin_de_semana_off` — vuelve a bloquear sábado y domingo.
- `/fin_de_semana_estado` — consulta si está activa o desactivada.

La activación no acredita dinero automáticamente. Solo permite que el administrador utilice **📊 Cuotas Diarias** durante sábado o domingo. La acreditación automática de las 18:00 continúa siendo únicamente de lunes a viernes.

## Seguridad
Los comandos anteriores comprueban el `ADMIN_TELEGRAM_ID` y solo funcionan en el chat privado del administrador.

## Importante
El paquete no incluye `database.db` ni backups de producción. El bot utiliza SQLite y genera respaldos `.db` y `.xlsx` durante su operación.


## Cambios de V45
- Resumen de sostenibilidad con reserva diaria del 3 % y reserva acumulada sin duplicar días.
- Neto para operar al día siguiente después de salidas y de separar la reserva nueva del día.
- Objetivo mínimo para mañana calculado como el 115 % del total introducido por el administrador ese día.
