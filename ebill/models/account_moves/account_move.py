# -*- coding: utf-8 -*-

from odoo import models, fields, api
from decimal import *
from odoo.exceptions import UserError
import json
from datetime import *
import urllib3
import re
import base64
import json

from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.tools.misc import formatLang, format_date

FACT_DATA_LABELS = ['Nro Documento Emisor : ', 'Tipo de Comprobante : ', 'Serie : ', 'Numero : ', 'IGV : ', 'Total : ',
                    'Fecha Emision : ',
                    'Tipo de Documento : ', 'Nro de Documento : ', 'Codigo Hash : ', '']


class AccountMove(models.Model):
    _inherit = 'account.move'
    type_show_amount = fields.Selection([('odoo','Odoo'),
                                         ('odoo_rounded','Odoo Redondeado'),('cpe','CPE'),
                                         ('cpe_round','CPE REDONDEADO')],string="Mostrar de",
                                        required=True,default='cpe')
    amount_total_rounded_it = fields.Char(compute='get_amounts_rounded',string='Total')
    amount_untaxed_rounded_it = fields.Char(compute='get_amounts_rounded',string='Base imponible')
    amount_by_group_rounded_it = fields.Char(compute='_compute_invoice_taxes_by_group_rounded',string='Impuestos')
    amount_residual_rounded_it = fields.Char(compute='get_amounts_rounded',string='Importe adeudado')

    @api.onchange('line_ids.price_subtotal', 'line_ids.tax_base_amount', 'line_ids.tax_line_id', 'partner_id',
                 'currency_id','type_show_amount')
    @api.depends('line_ids.price_subtotal', 'line_ids.tax_base_amount', 'line_ids.tax_line_id', 'partner_id',
                  'currency_id', 'type_show_amount')
    def _compute_invoice_taxes_by_group_rounded(self):
        parameters = self.env['main.parameter'].search([('company_id', '=', self.env.company.id)], limit=1)
        rounded = parameters.rounded_show_invoice
        ''' Helper to get the taxes grouped according their account.tax.group.
        This method is only used when printing the invoice.
        '''
        for move in self:
            lang_env = move.with_context(lang=move.partner_id.lang).env
            tax_lines = move.line_ids.filtered(lambda line: line.tax_line_id)
            tax_balance_multiplicator = -1 if move.is_inbound(True) else 1
            res = {}
            # There are as many tax line as there are repartition lines
            done_taxes = set()
            for line in tax_lines:
                res.setdefault(line.tax_line_id.tax_group_id, {'base': 0.0, 'amount': 0.0})
                res[line.tax_line_id.tax_group_id]['amount'] += tax_balance_multiplicator * (
                    line.amount_currency if line.currency_id else line.balance)
                tax_key_add_base = tuple(move._get_tax_key_for_group_add_base(line))
                if tax_key_add_base not in done_taxes:
                    if line.currency_id and line.company_currency_id and line.currency_id != line.company_currency_id:
                        amount = line.company_currency_id._convert(line.tax_base_amount, line.currency_id,
                                                                   line.company_id, line.date or fields.Date.today())
                    else:
                        amount = line.tax_base_amount
                    res[line.tax_line_id.tax_group_id]['base'] += amount
                    # The base should be added ONCE
                    done_taxes.add(tax_key_add_base)

            # At this point we only want to keep the taxes with a zero amount since they do not
            # generate a tax line.
            zero_taxes = set()
            for line in move.line_ids:
                for tax in line.tax_ids.flatten_taxes_hierarchy():
                    if tax.tax_group_id not in res or tax.id in zero_taxes:
                        res.setdefault(tax.tax_group_id, {'base': 0.0, 'amount': 0.0})
                        res[tax.tax_group_id]['base'] += tax_balance_multiplicator * (
                            line.amount_currency if line.currency_id else line.balance)
                        zero_taxes.add(tax.id)

            res = sorted(res.items(), key=lambda l: l[0].sequence)


            total = 0

            for group, amounts in res:
                total += amounts['amount']
            moneda = move.currency_id.symbol
            total = round(total, rounded) if move.type_show_amount in ['odoo_rounded', 'cpe_round'] else total
            move.amount_by_group_rounded_it = moneda + " " + str(total)



    @api.onchange('amount_total','type_show_amount','amount_untaxed')
    def get_amounts_rounded(self):
        for record in self:
            parameters = self.env['main.parameter'].search([('company_id', '=', self.env.company.id)], limit=1)
            rounded = parameters.rounded_show_invoice
            moneda = record.currency_id.symbol
            amount_total = record.amount_total if record.type_show_amount.find('odoo') >= 0 else record.amount_total_origin
            amount_untaxed = record.amount_untaxed
            amount_residual = record.amount_residual
            if record.type_show_amount in ['odoo_rounded','cpe_round']:
                amount_total = round(amount_total,rounded)
                amount_untaxed = round(amount_untaxed, rounded)
                amount_residual = round(amount_residual, rounded)

            record.amount_total_rounded_it = moneda+" "+str(amount_total)
            record.amount_untaxed_rounded_it = moneda + " " + str(amount_untaxed)
            record.amount_residual_rounded_it = moneda + " " + str(amount_residual)


    op_type_sunat_id = fields.Many2one('einvoice.catalog.51', string='Tipo de Operacion SUNAT')
    debit_note_type_id = fields.Many2one('einvoice.catalog.10', string='Tipo de Nota de Debito')
    credit_note_type_id = fields.Many2one('einvoice.catalog.09', string='Tipo de Nota de Credito')
    related_code = fields.Char(related='type_document_id.code', store=True)
    hash_code = fields.Char(string='Codigo Hash', copy=False)
    print_version = fields.Char(string='Version Impresa', copy=False)
    xml_version = fields.Char(string='Version XML', copy=False)
    cdr_version = fields.Char(string='Version CDR', copy=False)
    qr_code = fields.Char(string='Codigo QR', copy=False)
    print_web_version = fields.Char(string='Version Impresa Web', copy=False)
    file_name = fields.Char(copy=False)
    binary_version = fields.Binary(string='Version Binaria', copy=False)
    billing_type = fields.Selection([('0', 'Nubefact'), ('1', 'Odoo Facturacion')], string='Tipo de Facturador')
    einvoice_id = fields.Many2one('einvoice', string='Facturacion Electronica', copy=False)
    advance_ids = fields.One2many('move.advance.line', 'move_id', copy=False)
    sunat_state = fields.Selection([('0', 'Esperando Envio'),
                                    ('1', 'Aceptada por SUNAT'),
                                    ('2', 'Rechazada'),
                                    ('3', 'Enviado')], string='Estado de Facturacion', default='0', copy=False, tracking=True)
    detraction_type_id = fields.Many2one('einvoice.catalog.54', string='Tipo de Detraccion')
    detraction_amount = fields.Float(string='Monto de Detraccion')
    retencion_amount = fields.Float(string='Monto de Retencion')
    detraction_payment_id = fields.Many2one('einvoice.catalog.payment', string='Medio de Pago Detraccion')
    codigo_unico = fields.Char(string='Codigo Unico CPE', copy=False)
    delete_reason = fields.Text(string='Razon de Baja', limit=100)
    sunat_ticket_number = fields.Char(string='Numero de Ticket SUNAT',tracking=True)
    guide_line_ids = fields.One2many('move.guide.line', 'move_id')
    json_sent = fields.Char(string='JSON enviado', copy=False)
    json_response = fields.Char(string='JSON respuesta', copy=False)
    json_response_consulta = fields.Char(string='JSON respuesta consulta', copy=False)
    state_json_ebill = fields.Selection([('draft','Pendiente'),('revisition','en Revision'),
                                    ('done','Correcto'),('sent','Enviado')])

    # === Amount fields Origin===
    amount_total_origin = fields.Float(string='Total', store=True, readonly=True,digits=(12,10))

    '''

    
    amount_untaxed_origin = fields.Monetary(string='Untaxed Amount', store=True, readonly=True, tracking=True,
                                     compute='_compute_amount_origin')
    amount_tax_origin = fields.Monetary(string='Tax', store=True, readonly=True,
                                 compute='_compute_amount_origin')
    
    #',inverse='_inverse_amount_total'
    amount_residual_origin = fields.Monetary(string='Amount Due', store=True,
                                      compute='_compute_amount')
    amount_untaxed_signed_origin = fields.Monetary(string='Untaxed Amount Signed', store=True, readonly=True,
                                            compute='_compute_amount_origin', currency_field='company_currency_id')
    amount_tax_signed_origin = fields.Monetary(string='Tax Signed', store=True, readonly=True,
                                        compute='_compute_amount_origin', currency_field='company_currency_id')
    amount_total_signed_origin = fields.Monetary(string='Total Signed', store=True, readonly=True,
                                          compute='_compute_amount', currency_field='company_currency_id')
    amount_residual_signed_origin = fields.Monetary(string='Amount Due Signed', store=True,
                                             compute='_compute_amount_origin', currency_field='company_currency_id')
    #amount_by_group_origin = fields.Binary(string="Tax amount by group",
    #                                compute='_compute_invoice_taxes_by_group_origin')
    
    '''

    @api.onchange('invoice_line_ids')
    def _compute_amount_origin(self):
        for record in self:
            amount_total = 0
            for line in record.invoice_line_ids:
                amount_total += line.price_total_origin
            record.amount_total_origin = amount_total

    @api.onchange('detraction_type_id', 'amount_total_signed')
    def _get_detraction_amount(self):
        for record in self:
            if record.detraction_type_id and record.amount_total_signed:
                record.detraction_amount = record.amount_total_signed * record.detraction_type_id.percentage * 0.01

    @api.model
    def create(self, vals):
        t = super(AccountMove, self).create(vals)
        if t.type in ['out_invoice', 'out_refund']:
            t.billing_type = self.env['main.parameter'].search([('company_id', '=', self.env.company.id)],
                                                               limit=1).billing_type or None
            catalog = self.env['einvoice.catalog.51'].search([('code', '=', '0101')], limit=1)
            if not t.op_type_sunat_id and catalog:
                t.op_type_sunat_id = catalog.id
        return t


    def check_format_numberg(self, numberg):
        match = re.match(r'\b[A-Z]|[0-9]{4}-[0-9]{1,}\b', numberg, re.I)
        if not match:
            raise UserError(
                u'El formato del número de guía "' + numberg + '" es incorrecto, el formato debe ser similar a "0001-00001"')

        return numberg







