from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from datetime import datetime

def creer_facture():
    filename = "Facture_Ladini_Comprod.pdf"
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        rightMargin=40, leftMargin=40,
        topMargin=40, bottomMargin=40
    )
    
    story = []
    styles = getSampleStyleSheet()
    
    # Couleurs personnalisées
    primary_color = colors.HexColor("#1A365D")  # Bleu marine
    secondary_color = colors.HexColor("#4A5568") # Gris foncé
    light_bg = colors.HexColor("#EDF2F7")        # Gris clair
    
    # Styles de texte
    normal_style = ParagraphStyle(
        'InvoiceNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        textColor=secondary_color,
        leading=14
    )
    
    bold_style = ParagraphStyle(
        'InvoiceBold',
        parent=normal_style,
        fontName='Helvetica-Bold'
    )
    
    right_normal = ParagraphStyle(
        'InvoiceRight',
        parent=normal_style,
        alignment=2  # Aligné à droite
    )

    right_bold = ParagraphStyle(
        'InvoiceRightBold',
        parent=bold_style,
        alignment=2
    )

    # --- EN-TÊTE : Émetteur (Ladini) et Infos Facture ---
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    
    header_data = [
        [
            Paragraph("<b>LADINI</b><br/>Startup Innovante<br/>Ouagadougou, Kadiogo<br/>Burkina Faso<br/>01 BP 2549 (10010)<br/>Tél : +226 57 11 47 80<br/>Contact : contact@ladini.com", normal_style),
            Paragraph(f"<b>FACTURE</b><br/><br/><b>N° de facture :</b> FAC-{datetime.now().strftime('%Y%m')}-001<br/><b>Date :</b> {date_du_jour}<br/><b>Réf Client :</b> COMPROD-01", right_normal)
        ]
    ]
    
    header_table = Table(header_data, colWidths=[250, 255])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 20),
    ]))
    story.append(header_table)
    
    story.append(Spacer(1, 15))

    # --- CLIENT : Comprod ---
    client_data = [
        [Paragraph("<b>FACTURÉ À :</b>", bold_style)],
        [Paragraph("<b>Entreprise de Communication COMPROD</b><br/>Ouagadougou, Burkina Faso", normal_style)]
    ]
    client_table = Table(client_data, colWidths=[505])
    client_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), light_bg),
        ('PADDING', (0,0), (-1,-1), 10),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor("#CBD5E0")),
    ]))
    story.append(client_table)
    
    story.append(Spacer(1, 25))

    # --- TABLEAU DES PRESTATIONS ---
    table_data = [
        [
            Paragraph("<b>Description des prestations</b>", bold_style),
            Paragraph("<b>Qté</b>", bold_style),
            Paragraph("<b>Prix H.T.</b>", right_bold),
            Paragraph("<b>Total H.T.</b>", right_bold)
        ],
        [
            Paragraph("Gestion de la publicité (Facebook & WhatsApp)", normal_style),
            Paragraph("1", normal_style),
            Paragraph("45 000", right_normal),
            Paragraph("45 000", right_normal)
        ],
        [
            Paragraph("Conception et design de flyers publicitaires", normal_style),
            Paragraph("1", normal_style),
            Paragraph("56 000", right_normal),
            Paragraph("56 000", right_normal)
        ],
        [
            Paragraph("Organisation complète de la soirée d'inauguration", normal_style),
            Paragraph("1", normal_style),
            Paragraph("133 400", right_normal),
            Paragraph("133 400", right_normal)
        ]
    ]

    item_table = Table(table_data, colWidths=[245, 50, 105, 105])
    item_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E0")),
    ]))

    story.append(item_table)

    # --- TOTAUX ---
    total_ht = 45000 + 56000 + 133400  # Total = 234 400
    
    totals_data = [
        [Paragraph("<b>Total H.T. :</b>", right_normal), Paragraph(f"<b>{total_ht:,.2f} XOF</b>".replace(",", " "), right_normal)],
        [Paragraph("<b>TVA (0% / Exonéré) :</b>", right_normal), Paragraph("0.00 XOF", right_normal)],
        [Paragraph("<b>TOTAL NET À PAYER :</b>", right_bold), Paragraph(f"<b>{total_ht:,.2f} XOF</b>".replace(",", " "), right_bold)]
    ]
    
    totals_table = Table(totals_data, colWidths=[355, 150])
    totals_table.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LINEABOVE', (0,2), (1,2), 1, primary_color),
        ('BACKGROUND', (0,2), (1,2), light_bg),
    ]))
    
    story.append(Spacer(1, 10))
    story.append(totals_table)

    story.append(Spacer(1, 40))

    # --- PIED DE PAGE / CONDITIONS ---
    footer_text = Paragraph(
        "<b>Modalités de paiement :</b><br/>"
        "Paiement par virement ou espèces dans un délai de 30 jours.<br/>"
        "Merci pour votre collaboration !",
        normal_style
    )
    story.append(footer_text)

    # Construction du document PDF
    doc.build(story)
    print(f"Facture générée avec succès : {filename}")

if __name__ == "__main__":
    creer_facture()