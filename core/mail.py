import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

_FOOTER = """
<br><br>
<hr style="border:none;border-top:1px solid #e2e8f0;margin:16px 0">
<table style="width:100%;font-family:-apple-system,sans-serif;font-size:12px;color:#94a3b8">
  <tr>
    <td style="padding-bottom:4px">
      Hata tespit ederseniz
      <a href="mailto:ayanisa47@gmail.com"
         style="color:#6366f1;text-decoration:none">ayanisa47@gmail.com</a>
      adresine mail atınız.
    </td>
  </tr>
  <tr>
    <td>
      Developed by
      <a href="https://www.linkedin.com/in/isa-ayan/"
         style="color:#6366f1;font-weight:600;text-decoration:none">Isa Ayan</a>
    </td>
  </tr>
</table>
"""


def send_mail(to: list[str], subject: str, body: str,
              attachments: list[Path] = None):
    from core.config_manager import load_settings
    cfg = load_settings().get("email", {})

    sender   = cfg.get("sender_email", "")
    password = cfg.get("sender_password", "")
    host     = cfg.get("smtp_host", "smtp.gmail.com")
    port     = int(cfg.get("smtp_port", 587))

    if not sender or not password:
        raise ValueError("E-posta ayarları eksik — Ayarlar → E-posta bölümünden doldurun.")

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = ", ".join(to)
    msg["Subject"] = subject
    msg.attach(MIMEText(body + _FOOTER, "html", "utf-8"))

    for path in (attachments or []):
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
        msg.attach(part)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, to, msg.as_string())


def send_error_mail(subject: str, body: str):
    """Hata bildirimlerini error_recipients listesine gönderir."""
    from core.config_manager import load_settings
    cfg        = load_settings().get("email", {})
    recipients = list(cfg.get("error_recipients", []))
    old        = cfg.get("error_recipient", "")
    if old and old not in recipients:
        recipients.insert(0, old)
    if recipients:
        send_mail(recipients, f"[HATA] {subject}", body)
