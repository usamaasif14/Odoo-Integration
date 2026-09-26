# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import json

from odoo import models
from odoo.tools import file_open


class SpreadsheetDashboard(models.AbstractModel):
    _inherit = 'spreadsheet.dashboard'

    def _get_spreadsheet_serialized_snapshot(self):
        self._fix_accounting_dashboard()
        return super()._get_spreadsheet_serialized_snapshot()

    def _fix_accounting_dashboard(self):
        accounting_dashboard = self.env.ref(
            'spreadsheet_dashboard_account_accountant.dashboard_accounting',
            raise_if_not_found=False
        )
        if self == accounting_dashboard:
            snapshot = json.loads(
                base64.b64decode(self.spreadsheet_snapshot).decode('utf-8')
                if self.spreadsheet_snapshot
                else self.spreadsheet_data
            )
            version = snapshot.get('version')
            if version == '18.5.10' and len(snapshot['sheets']) > 1:
                E1 = snapshot['sheets'][1].get('cells', {}).get('E1')
                if E1 == '=ODOO.FILTER.VALUE.V18("Year")':
                    # already wrongly upgraded to use ODOO.FILTER.VALUE.V18 instead of ODOO.FILTER.VALUE
                    snapshot['sheets'][1]['cells']['E1'] = '=ODOO.FILTER.VALUE("Year")'
                    snapshot_b64 = base64.b64encode(json.dumps(snapshot).encode('utf-8'))
                    self.sudo().spreadsheet_snapshot = snapshot_b64
            elif version == '18.4.14':
                # not yet upgraded: force a reload from file
                with file_open('spreadsheet_dashboard_account_accountant/data/files/accounting_dashboard.json', 'r') as file:
                    self.sudo().spreadsheet_data = file.read()
