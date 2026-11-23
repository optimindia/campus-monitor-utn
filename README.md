# Monitor Campus UTN FRM → Telegram

Vigila el Campus Virtual de la UTN Facultad Regional Mendoza (Moodle) y te avisa por Telegram:

- **Foros** de todas tus materias: avisos y respuestas nuevas, con el texto completo.
- **🚨 Notas publicadas**: detecta cuándo las cátedras publican notas y, si vienen en un
  PDF/Excel adjunto, en una página de consulta o en una carpeta pública de Google Drive,
  **busca tu legajo y te manda tu nota directamente**.
- **📊 Cambios en tu nota** en planillas que ya existían.
- Material nuevo, notificaciones del campus y mensajes privados.

Entra con el login web normal (el campus tiene deshabilitada la API de la app móvil).
Solo usa la librería estándar de Python + `pypdf` para leer PDFs.

## Instalación

1. Creá un bot con [@BotFather](https://t.me/BotFather) y guardá el token.
2. Copiá `.env.ejemplo` como `.env` y completalo (usuario/clave del campus, token, legajo).
3. Mandale cualquier mensaje a tu bot y sacá tu chat_id:
   ```bash
   pip install pypdf
   python campus_monitor.py --chat-id
   ```
4. Probá: `python campus_monitor.py --test`
5. Arrancalo: `python campus_monitor.py` (en Windows: doble clic en `iniciar.bat`).

La primera vuelta solo registra lo que ya existe (no te llena de avisos viejos).
`python campus_monitor.py --resumen` te manda todas tus notas encontradas.

## En un VPS Linux (24/7 con systemd)

```bash
git clone <este repo> ~/campus-monitor && cd ~/campus-monitor
python3 -m venv venv && venv/bin/pip install pypdf
cp .env.ejemplo .env && nano .env && chmod 600 .env
venv/bin/python campus_monitor.py --test
sed -i "s/TU_USUARIO/$USER/g" campus-monitor.service
sudo cp campus-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now campus-monitor
journalctl -u campus-monitor -f
```

⚠️ No lo corras en dos lugares a la vez: te llegaría cada aviso duplicado.

## Privacidad

`.env` (contraseñas), `estado.json` (tus notas) y `monitor.log` están en `.gitignore`
y nunca se suben al repo.
