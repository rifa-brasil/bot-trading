elif data_cb == "ver_saldo":
        # Verificamos si el usuario existe en la base de datos
        if user_id in data["usuarios"]:
            u = data["usuarios"][user_id]
            deposito = u.get("deposito", 0)
            ganancias = u.get("ganancias_acumuladas", 0)
            total_disponible = deposito + ganancias
            
            estado = "✅ Activa" if u.get("activo") else "⏳ Pendiente de aprobación"
            
            mensaje_saldo = (
                f"📊 *ESTADO DE TU CUENTA*\n\n"
                f"👤 *Usuario:* {u.get('nombre')}\n"
                f"📌 *Estado:* {estado}\n\n"
                f"💵 *Depósito Inicial:* {deposito:.2f} USDT\n"
                f"📈 *Ganancias Acumuladas:* {ganancias:.2f} USDT\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💎 *TOTAL DISPONIBLE:* {total_disponible:.2f} USDT\n\n"
                f"Recuerda que las ganancias se actualizan automáticamente cada 24 horas."
            )
            await query.message.reply_text(mensaje_saldo, parse_mode="Markdown")
        else:
            await query.message.reply_text("⚠️ No encontramos un registro activo para tu cuenta.")
