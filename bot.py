# ... (código anterior igual)

    elif data_cb == "pedir_retiro":
        if is_reg and data["usuarios"][user_id].get("activo"):
            u_info = data["usuarios"][user_id]
            
            # Validación de día (Jueves 3, Viernes 4)
            dia_actual = datetime.now().weekday()
            if dia_actual not in [3, 4]:
                await query.message.reply_text("⚠️ Los retiros solo están habilitados los días **jueves y viernes**.", reply_markup=obtener_menu(is_reg))
                return

            # Validar retiro semanal
            ultimo_retiro = u_info.get("ultimo_retiro_fecha", "")
            if es_misma_semana(ultimo_retiro):
                await query.message.reply_text("⚠️ Ya realizaste el retiro correspondiente a esta semana.", reply_markup=obtener_menu(is_reg))
                return

            ganancias_disp = u_info.get("ganancias_acumuladas", 0)
            deposito_inicial = u_info.get("deposito", 0)

            # NUEVA LÓGICA:
            # Si tiene ganancias, se comporta normal (mínimo de retiro).
            # Si NO tiene ganancias (todo es capital inicial), puede retirar sin mínimo.
            se_puede_retirar = False
            if ganancias_disp > 0:
                if ganancias_disp >= MIN_RETIRO:
                    se_puede_retirar = True
                    mensaje_retiro = f"📤 Tienes {ganancias_disp:.2f} USDT disponibles de ganancias."
                else:
                    await query.message.reply_text(f"⚠️ Para retirar ganancias, el mínimo es {MIN_RETIRO} USDT.", reply_markup=obtener_menu(is_reg))
                    return
            else:
                # Si no hay ganancias, le permitimos retirar el depósito inicial sin mínimo
                se_puede_retirar = True
                mensaje_retiro = f"📤 No tienes ganancias generadas. Puedes retirar tu capital inicial ({deposito_inicial:.2f} USDT)."

            if se_puede_retirar:
                if "estados_retiro" not in data:
                    data["estados_retiro"] = {}
                data["estados_retiro"][user_id] = {"fase": "monto"}
                guardar_data(data)
                await query.message.reply_text(f"{mensaje_retiro}\n\nPor favor, escribe la cantidad que deseas retirar:", parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ Tu cuenta debe estar activa para solicitar retiros.", reply_markup=obtener_menu(is_reg))

# ... (resto del código)
