import json
import re
import uuid

import requests
from dateutil.relativedelta import relativedelta
from lxml.etree import Element
from markupsafe import Markup
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import SSLError
from stdnum.nl.btw import compact

from odoo import _lt, api, fields, models
from odoo.exceptions import RedirectWarning, ValidationError
from odoo.tools.misc import format_date
from odoo.tools.zeep import wsse
from odoo.tools.zeep.exceptions import Fault

from odoo.addons.l10n_nl_reports.tools.digipoort_envelope import (
    extract_fault_description,
    prepare_signed_xbrl_envelope,
)
from odoo.addons.l10n_nl_reports.tools.iap_tooling import get_iap_endpoint, is_test_mode

ERROR_CODE_TO_MESSAGE = {
    'certificate_access': _lt("Your certificate was not accepted by the server. Please verify the certificate you uploaded and try again."),
    'dbuuid_not_exist': _lt("The IAP Odoo server could not identify your database. Please contact Odoo support."),
    'digipoort_error': _lt("The Digipoort server returned the following error:\n"),
    'digipoort_unavailable': _lt("The Digipoort server is not accessible at the moment. Please try again later."),
    'error_subscription': _lt("The IAP Odoo server could not verify your subscription. Please try again later."),
    'internal_error': _lt("An internal error occurred on the IAP Odoo server. Please contact Odoo support."),
    'limit_call_reached': _lt("Too many requests were sent to the IAP Odoo server. Please try again later."),
    'not_active_db': _lt("Your database is not active on the IAP Odoo server. Please contact Odoo support."),
    'not_enterprise': _lt("Only Enterprise databases can submit Dutch SBR reports through Odoo."),
    'not_prod_env': _lt("Only production databases can submit Dutch SBR reports through Odoo."),
    'root_certificate': _lt("The server root certificate is not accessible at the moment. Please try again later."),
    'too_many_requests': _lt("Too many requests were sent to the IAP Odoo server. Please try again later."),
}


class L10n_Nl_ReportsSbrTaxReportWizard(models.TransientModel):
    _name = 'l10n_nl_reports.sbr.tax.report.wizard'
    _description = 'L10n NL Tax Report for SBR Wizard'

    def _get_default_initials(self):
        user_name = self.env.user.name
        return ''.join([name[0].upper() for name in re.split(r"[- ']", user_name)])

    def _get_default_infix(self):
        # The infix is the "little names" in-between the surname and last name (typically "van de")
        user_name = self.env.user.name
        user_names = user_name.split()
        return ' '.join(user_names[1:-1]) if len(user_names) > 2 else False

    date_from = fields.Date(string="Period Starting Date")
    date_to = fields.Date(string="Period Ending Date")
    can_report_be_sent = fields.Boolean(compute='_compute_sending_conditions')

    contact_initials = fields.Char(string="Contact Initials", default=_get_default_initials)
    contact_prefix = fields.Char(string="Contact Name Infix", default=_get_default_infix)
    contact_surname = fields.Char(string="Contact Last Name", default=lambda self: self.env.user.name.split()[-1])
    contact_phone = fields.Char(string="Contact Phone", default=lambda self: self.env.user.phone)
    contact_type = fields.Selection(
        [('BPL', 'Taxpayer (BPL)'), ('INT', 'Intermediary (INT)')],
        string="Contact Type",
        default='BPL',
        required=True,
        help="BPL: if the taxpayer files a turnover tax return as an individual entrepreneur."
             "INT: if the turnover tax return is made by an intermediary.",
    )
    tax_consultant_order = fields.Selection(
        [
            ('NBA', 'NBA - Accountants'),
            ('RB', 'RB - Register of Tax Advisors'),
            ('NOB', 'NOB - Dutch Order of Tax Advisors'),
            ('NOAB', 'NOAB - Dutch Order of Administrative and Tax Experts'),
        ],
        required=True,
        string="Tax Consultant Order",
        compute="_compute_tax_consultant_order",
        readonly=False,
        help="The order of tax consultants the tax consultant belongs to.",
    )
    tax_consultant_number = fields.Char(string="Tax Consultant Number", help="The tax consultant number of the office aware of the content of this report.")
    is_test = fields.Boolean(string="Is Test", help="Technical field kept for stable compatibility. The SBR mode is now configured with the l10n_nl_reports.sbr_mode system parameter.")
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('date_to', 'date_from')
    def _compute_sending_conditions(self):
        for wizard in self:
            wizard.can_report_be_sent = is_test_mode(wizard.env) or (
                wizard.env.company.tax_lock_date and wizard.env.company.tax_lock_date >= wizard.date_to
                and (
                    not wizard.env.company.l10n_nl_reports_sbr_last_sent_date_to
                    or wizard.date_from > wizard.env.company.l10n_nl_reports_sbr_last_sent_date_to
                    or wizard.date_to < wizard.env.company.l10n_nl_reports_sbr_last_sent_date_to + relativedelta(months=1)
                )
            )

    def _compute_tax_consultant_order(self):
        self.tax_consultant_order = self.tax_consultant_order or 'NBA'

    def _check_values(self):
        if self.env.company.account_representative_id:
            if not self.env.company.account_representative_id.vat:
                raise RedirectWarning(
                    self.env._("Your accounting firm does not have a VAT number set. Please set it up before trying to send the report."),
                    self.env.ref('base.action_res_company_form').id,
                    self.env._("Company Settings"),
                )
        elif not self.env.company.vat:
            raise RedirectWarning(
                self.env._("Your company does not have a VAT number set. Please set it up before trying to send the report."),
                self.env.ref('base.action_res_company_form').id,
                self.env._("Company Settings"),
            )

    def _get_sbr_identifier(self, options=None):
        is_company_only = not options or options.get('tax_unit', 'company_only') == 'company_only'
        if is_company_only and self.env.company.l10n_nl_reports_sbr_ob_nummer:
            return self.env.company.l10n_nl_reports_sbr_ob_nummer

        vat = self.env.company.vat
        if options and options.get('report_id'):
            report = self.env['account.report'].browse(options['report_id'])
            vat = report.get_vat_for_export(options, raise_warning=False)
        return compact(vat) if vat else ''

    def _get_iap_common_params(self, report_type, report_file, vat):
        return {
            'vat': vat,
            'report_type': report_type,
            'report_file': report_file.decode('utf-8') if isinstance(report_file, bytes) else report_file,
        }

    def _call_iap_proxy(self, route, params):
        try:
            iap_response = requests.post(
                get_iap_endpoint(self.env) + route,
                params={'db_uuid': self.env['ir.config_parameter'].sudo().get_param('database.uuid')},
                json=params,
                timeout=30,
            )
            iap_response.raise_for_status()
            response = iap_response.json() or {}
        except (requests.RequestException, ValueError) as error:
            raise ValidationError(ERROR_CODE_TO_MESSAGE['internal_error']) from error
        if response.get('error'):
            error_code = response['error']
            error_message = ERROR_CODE_TO_MESSAGE.get(error_code, self.env._("An unexpected error occurred while sending your report."))
            error_message += response.get('error_additional_message', '')
            raise ValidationError(error_message)
        return response

    def _send_signed_xbrl_through_iap(self, report_type, report_file, vat, certificate, private_key):
        try:
            signed_request = prepare_signed_xbrl_envelope(
                report_type=report_type,
                report_file=report_file,
                vat=vat,
                certificate=certificate,
                private_key=private_key,
                server_root_certificate=self.env.company.sudo()._l10n_nl_get_server_root_certificate_bytes(),
                is_test=is_test_mode(self.env),
            )
            return self._call_iap_proxy(
                '/api/l10n_nl_reports/1/send_signed_xbrl',
                {
                    'report_type': report_type,
                    **signed_request,
                },
            )
        except SSLError as error:
            raise ValidationError(ERROR_CODE_TO_MESSAGE['certificate_access']) from error
        except Fault as error:
            raise ValidationError(ERROR_CODE_TO_MESSAGE['digipoort_error'] + extract_fault_description(error)) from error
        except RequestsConnectionError as error:
            raise ValidationError(ERROR_CODE_TO_MESSAGE['digipoort_unavailable']) from error
        except wsse.signature.SignatureVerificationFailed as error:
            raise ValidationError(ERROR_CODE_TO_MESSAGE['internal_error']) from error

    def _additional_processing(self, options, kenmerk, closing_move):
        access_token = self.env.context.get('l10n_nl_reports_sbr_access_token')
        self.env['l10n_nl_reports.sbr.status.service'].create({
            'kenmerk': f'{kenmerk}:{access_token}' if access_token else kenmerk,
            'company_id': self.env.company.id,
            'report_name': self.env['account.report'].browse(options['report_id']).name,
            'closing_entry_id': closing_move and closing_move.id,
        })
        status_service_cron = self.env.ref('l10n_nl_reports.cron_l10n_nl_reports_status_process')
        status_service_cron._trigger()

    # TODO: Remove in master
    def _check_sbr_certificates(self):
        cert_data = self.env.company.sudo().l10n_nl_reports_sbr_cert_id.pem_certificate
        key_data = self.env.company.sudo().l10n_nl_reports_sbr_cert_id.private_key_id.pem_key
        if not cert_data or not key_data:
            raise RedirectWarning(
                self.env._("The certificate or the private key is missing. Please upload it in the Accounting Settings first."),
                self.env.ref('account.action_account_config').id,
                self.env._("Go to the Accounting Settings"),
            )

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == 'form':
            node = arch.find(".//field[@name='can_report_be_sent']...")
            if node is not None:
                pwd_element = Element('field')
                pwd_element.set('name', 'company_id')
                pwd_element.set('invisible', '1')
                # Mark the node as automatically added, like `_add_missing_fields` does in ir_ui_view,
                # so that Studio does not consider it as a normal, user-editable node.
                pwd_element.set('data-used-by', 'l10n_nl_reports')
                node.append(pwd_element)
        return arch, view

    def action_download_xbrl_file(self):
        options = self.env.context['options']
        options['codes_values'] = self._generate_general_codes_values(options)
        return {
            'type': 'ir_actions_account_report_download',
            'data': {
                'model': self.env.context.get('model'),
                'options': json.dumps(options),
                'file_generator': 'export_tax_report_to_xbrl',
            }
        }

    def send_xbrl(self):
        # Send directly with a personal certificate, otherwise through the IAP group certificate.
        options = self.env.context['options']
        is_test = is_test_mode(self.env)
        report_handler = self.env['l10n_nl_reports.tax.report.handler']
        account_return = self.env['account.return']._get_return_from_report_options(options)
        closing_move = self.env['account.move']
        if account_return:
            account_return._proceed_with_submission()
            closing_move = account_return.closing_move_ids.filtered(
                lambda move: move.company_id == account_return.company_id
            )[:1]
        if not is_test and account_return:
            if not closing_move:
                raise RedirectWarning(
                    self.env._("No closing entry was found for the selected period. Please create one and post it before sending your report."),
                    self.env['account.return'].action_open_tax_return_view(additional_return_domain=[
                        ('date_to', '<=', options['date']['date_to']),
                        ('date_from', '>=', options['date']['date_from']),
                        ('type_id.report_id', '=', options['report_id']),
                    ]),
                    self.env._("Create Closing Entry"),
                )
            if any(move.state == 'draft' for move in closing_move):
                raise RedirectWarning(
                    self.env._("The closing entry for the selected period is still in draft. Please post it before sending your report."),
                    self.env['account.return'].action_open_tax_return_view(additional_return_domain=[
                        ('date_to', '<=', options['date']['date_to']),
                        ('date_from', '>=', options['date']['date_from']),
                        ('type_id.report_id', '=', options['report_id']),
                    ]),
                    self.env._("Closing Entry"),
                )
        options['codes_values'] = self._generate_general_codes_values(options)
        xbrl_data = report_handler.export_tax_report_to_xbrl(options)
        report_file = xbrl_data['file_content']
        report_type = 'tax_correction' if options.get('l10n_nl_is_correction') else 'tax_report'

        certificate, private_key = self.env.company.sudo()._l10n_nl_get_certificate_and_key_bytes()
        if certificate and private_key:
            response = self._send_signed_xbrl_through_iap(report_type, report_file, self._get_sbr_identifier(options), certificate, private_key)
        else:
            response = self._call_iap_proxy(
                '/api/l10n_nl_reports/1/sign_and_send_xbrl',
                self._get_iap_common_params(report_type, report_file, self._get_sbr_identifier(options)),
            )
        kenmerk = response.get('kenmerk')
        access_token = response.get('access_token')
        can_track_status = bool(kenmerk and access_token)
        can_post_status = bool(account_return and can_track_status)

        if not is_test:
            self.env.company.sudo().l10n_nl_reports_sbr_last_sent_date_to = self.date_to

        subject = self.env._("Tax report sent")
        body = self.env._(
            "The tax report from %(date_from)s to %(date_to)s was sent to %(environment)s.%(newline)s"
            "%(status_message)s%(newline)s"
            "%(discussion_id)s",
            date_from=format_date(self.env, self.date_from),
            date_to=format_date(self.env, self.date_to),
            environment=self.env._("Digipoort pre-production") if is_test else self.env._("Digipoort"),
            status_message=self.env._("We will post its processing status in this chatter once received.") if can_post_status else self.env._("Status polling is unavailable because no discussion ID was returned."),
            discussion_id=self.env._("Discussion ID: %(id)s", id=kenmerk) if can_track_status else self.env._("Discussion ID unavailable."),
            newline=Markup("<br>"),
        )
        filename = f'tax_report_{self.date_to.year}_{self.date_to.month}.xbrl'
        self.env['l10n_nl_reports.sbr.status.service']._process_messages_and_statuses(
            account_return, subject, body, attachments=[(filename, report_file)], subscribe=True, status='pending',
        )

        if can_track_status:
            self.with_context(l10n_nl_reports_sbr_access_token=access_token)._additional_processing(
                options, kenmerk, closing_move,
            )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Sending your report"),
                'type': 'success',
                'message': (
                    self.env._("Your test tax report has been sent to Digipoort pre-production.")
                    if is_test
                    else (
                        self.env._("Your tax report is being sent to Digipoort. Check its status in the closing entry's chatter.")
                        if can_post_status
                        else self.env._("Your tax report has been sent to Digipoort.")
                    )
                ),
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }

    def _generate_general_codes_values(self, options):
        self._check_values()
        report = self.env['account.report'].browse(options['report_id'])
        sender_vat = report._get_sender_company_for_export(options).vat
        vat_identification_division = compact(sender_vat) if sender_vat else ''
        vat = report.get_vat_for_export(options)
        message_reference_supplier_vat = (self.env.company.account_representative_id.vat or vat)
        if message_reference_supplier_vat.startswith('NL'):
            message_reference_supplier_vat = compact(message_reference_supplier_vat)
        return {
            'identifier': self._get_sbr_identifier(options),
            'startDate': fields.Date.to_string(self.date_from),
            'endDate': fields.Date.to_string(self.date_to),
            'ContactInitials': self.contact_initials or '',
            'ContactPrefix': self.contact_prefix,
            'ContactSurname': self.contact_surname,
            'ContactTelephoneNumber': re.sub(r"[^\+\d]", "", self.contact_phone or ''),
            'ContactType': self.contact_type,
            'DateTimeCreation': fields.Datetime.now().strftime("%Y%m%d%H%M"),
            'MessageReferenceSupplierVAT': (message_reference_supplier_vat + '-' + str(uuid.uuid4()))[:20],
            'ProfessionalAssociationForTaxServiceProvidersName': (self.env.company.account_representative_id.name or '')[:20],
            'ProfessionalAssociationForTaxServiceProvidersOrder': self.tax_consultant_order,
            'SoftwarePackageName': 'Odoo',
            'SoftwarePackageVersion': '.'.join(self.sudo().env.ref('base.module_base').latest_version.split('.')[0:3]),
            'SoftwareVendorAccountNumber': 'swo02770',
            'TaxConsultantNumber': self.tax_consultant_number,
            'VATIdentificationNumberNLFiscalEntityDivision': vat_identification_division,
        }
