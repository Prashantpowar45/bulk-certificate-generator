"""One fixed, landscape certificate template; user strings are plain text."""
from io import BytesIO
from pathlib import Path
import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth, registerFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# Embed bundled fonts so rendering is consistent across PDF viewers and servers.
FONT_DIR = Path(reportlab.__file__).parent / "fonts"
registerFont(TTFont("Certificate", str(FONT_DIR / "Vera.ttf")))
registerFont(TTFont("CertificateBold", str(FONT_DIR / "VeraBd.ttf")))

def render_certificate(name: str, course_name: str, organization: str,
                       issue_date: str, certificate_id: str) -> bytes:
    output = BytesIO()
    width, height = landscape(A4)
    pdf = canvas.Canvas(output, pagesize=(width, height))
    pdf.setTitle(f'Certificate of completion - {name}')
    pdf.setAuthor(organization)
    pdf.setFillColor(colors.HexColor('#FAF8F3'))
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    pdf.setStrokeColor(colors.HexColor('#B59452'))
    pdf.setLineWidth(2)
    pdf.rect(28, 28, width - 56, height - 56)
    pdf.setLineWidth(0.5)
    pdf.rect(36, 36, width - 72, height - 72)

    def centered(text, y, size=20, font='Certificate', color='#182C45'):
        # Shrink long names and course titles to fit the fixed template.
        measured = stringWidth(text, font, size)
        if measured > width - 140:
            size *= (width - 140) / measured
        pdf.setFillColor(colors.HexColor(color))
        pdf.setFont(font, size)
        pdf.drawCentredString(width / 2, y, text)

    centered(organization, 500, 18, 'CertificateBold')
    centered('CERTIFICATE OF COMPLETION', 422, 31, 'CertificateBold')
    centered('This certificate is proudly presented to', 363, 14)
    centered(name, 304, 34, 'CertificateBold', '#9A7433')
    centered('for successfully completing', 256, 14)
    centered(course_name, 214, 25, 'CertificateBold')
    centered(f'Issued on {issue_date}', 143, 13)
    centered(f'Certificate ID: {certificate_id}', 80, 9)
    pdf.showPage()
    pdf.save()
    return output.getvalue()
