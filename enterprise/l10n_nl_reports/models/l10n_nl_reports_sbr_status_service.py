import logging
from datetime import timedelta

import requests
from markupsafe import Markup

from odoo import fields, models

from odoo.addons.l10n_nl_reports.tools.iap_tooling import get_iap_endpoint


_logger = logging.getLogger(__name__)


class L10n_Nl_ReportsSbrStatusService(models.Model):
    _name = 'l10n_nl_reports.sbr.status.service'
    _description = 'Status checking service for Digipoort submission'

    kenmerk = fields.Char('Message Exchange ID')
    company_id = fields.Many2one('res.company', 'Company')
    report_name = fields.Char('Name of the submitted report')
    is_done = fields.Boolean('Is the cycle finished?', default=False)
    closing_entry_id = fields.Many2one('account.move', string='Related closing entry')
    is_test = fields.Boolean('Is it a test?', help="Technical field kept for stable compatibility. The SBR mode is now configured with the l10n_nl_reports.sbr_mode system parameter.")

    def _cron_process_submission_status(self):
        ongoing_processes = self.search([('is_done', '=', False)])
        if not ongoing_processes:
            return

        for process in ongoing_processes:
            kenmerk, access_token = process.kenmerk.split(':', 1) if ':' in process.kenmerk else (process.kenmerk, False)
            if access_token:
                try:
                    iap_response = requests.post(
                        get_iap_endpoint(self.env) + '/api/l10n_nl_reports/1/get_submission_status',
                        params={'db_uuid': self.env['ir.config_parameter'].sudo().get_param('database.uuid')},
                        json={
                            'kenmerk': kenmerk,
                            'access_token': access_token,
                        },
                        timeout=30,
                    )
                    iap_response.raise_for_status()
                    response = iap_response.json() or {}
                except (requests.RequestException, ValueError):
                    _logger.exception('Unexpected error while querying Digipoort submission status for kenmerk %s.', kenmerk)
                    continue

                if response.get('error'):
                    _logger.warning('The IAP proxy returned error %(error)s while checking kenmerk %(kenmerk)s.', {'error': response['error'], 'kenmerk': kenmerk})
                    continue
            else:
                _logger.warning('Could not query Digipoort status for kenmerk %s because the IAP access token is missing.', kenmerk)
                process.is_done = True
                account_return = process.closing_entry_id.closing_return_id
                if account_return:
                    subject = self.env._("%(report_name)s status retrieval failed", report_name=process.report_name)
                    body = self.env._(
                        "The status of the %(report_name)s submission with discussion ID '%(id)s' could not be retrieved "
                        "because its IAP authentication information is unavailable.",
                        report_name=process.report_name,
                        id=kenmerk,
                    )
                    process._process_messages_and_statuses(account_return, subject, body, status='error')
                continue

            if response.get('state') == 'pending':
                continue

            process.is_done = True

            account_return = process.closing_entry_id.closing_return_id
            if not account_return:
                continue
            if response.get('state') == 'failed':
                if response.get('error_type') == 'status_retrieval_failed':
                    subject = self.env._("%(report_name)s status retrieval failed", report_name=process.report_name)
                    body = self.env._(
                        "The status retrieval for the %(report_name)s with discussion ID '%(id)s' failed with the error:%(newline)s%(newline)s"
                        "%(italic_start)s%(error)s%(italic_end)s%(newline)s%(newline)s"
                        "Try submitting your report again.",
                        report_name=process.report_name,
                        id=kenmerk,
                        error=response.get('error_description', ''),
                        newline=Markup("<br>"),
                        italic_start=Markup("<i>"),
                        italic_end=Markup("</i>"),
                    )
                else:
                    subject = self.env._("%(report_name)s submission failed", report_name=process.report_name)
                    body = self.env._(
                        "The submission for the %(report_name)s with discussion ID '%(id)s' failed with the error:%(newline)s%(newline)s"
                        "%(italic_start)s%(error)s%(italic_end)s%(newline)s"
                        "%(italic_start)s%(detailed_error)s%(italic_end)s%(newline)s%(newline)s"
                        "Try submitting your report again.",
                        report_name=process.report_name,
                        id=kenmerk,
                        error=response.get('status_description', ''),
                        detailed_error=response.get('error_description', ''),
                        newline=Markup("<br>"),
                        italic_start=Markup("<i>"),
                        italic_end=Markup("</i>"),
                    )
                process._process_messages_and_statuses(account_return, subject, body, status='error')
                continue

            if response.get('state') == 'success':
                subject = self.env._("%(report_name)s submission succeeded", report_name=process.report_name)
                body = self.env._(
                    "The submission for the %(report_name)s with discussion ID '%(id)s' was successfully received by Digipoort.",
                    report_name=process.report_name,
                    id=kenmerk,
                )
                process._process_messages_and_statuses(account_return, subject, body, status='accepted')

        if self.search_count([('is_done', '=', False)]):
            # If there are still unfinished processes, we trigger a cron to check the status again in one minute
            statusinformatieservice_cron = self.env.ref('l10n_nl_reports.cron_l10n_nl_reports_status_process')
            statusinformatieservice_cron._trigger(fields.Datetime.now() + timedelta(minutes=1))

    def _process_messages_and_statuses(self, account_return, subject, body, attachments=None, subscribe=False, status=None):
        if not account_return:
            return
        if attachments:
            previous_xbrl_attachments = self.env['ir.attachment'].search_fetch([
                ('res_model', '=', account_return._name),
                ('res_id', '=', account_return.id),
                ('name', 'ilike', '.xbrl'),
                ('name', 'not ilike', '(detached)'),  # Old files are renamed with "(detached)". We don't want to rename those again.
            ], ['name'])
            for attachment in previous_xbrl_attachments:
                attachment_name, attachment_extension = attachment.name.rsplit('.', 1)
                attachment.name = self.env._(
                    '%(attachment_name)s (detached).%(attachment_extension)s',
                    attachment_name=attachment_name,
                    attachment_extension=attachment_extension,
                )
        account_return.message_post(
            subject=subject,
            body=body,
            author_id=self.env.ref('base.partner_root').id,
            subtype_id=self.env.ref('mail.mt_comment').id,
            attachments=attachments or [],
        )
        if subscribe:
            account_return.message_subscribe(partner_ids=[self.env.user.partner_id.id])
