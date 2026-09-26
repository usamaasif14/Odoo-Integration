# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json

from odoo.tests import TransactionCase


class AccountingDashboardFilterValue(TransactionCase):

    def snapshot(self, spreadsheet, current_revision_uuid, snapshot_revision_id, data):
        return spreadsheet.dispatch_spreadsheet_message({
            "type": "SNAPSHOT",
            "nextRevisionId": snapshot_revision_id,
            "serverRevisionId": current_revision_uuid,
            "data": data,
        })

    def test_not_upgraded_yet(self):
        dashboard = self.env.ref('spreadsheet_dashboard_account_accountant.dashboard_accounting')

        # simulate the data as it was before upgrade
        data = {
            'version': '18.4.14',  # version before
            'sheets': [
                {
                    'name': 'Sheet 1',
                    'cells': {'A1': '=ODOO.FILTER.VALUE("some_filter")'},
                },
            ],
        }
        dashboard.spreadsheet_data = json.dumps(data)
        data = json.loads(dashboard._get_spreadsheet_serialized_snapshot())
        self.assertEqual('18.5.10', data['version'], "data should be reloaded from file")
        self.assertEqual(
            data['sheets'][1]['cells']['E1'],
            '=ODOO.FILTER.VALUE("Year")',
            "data should be reloaded from file"
        )

    def test_already_upgraded(self):
        dashboard = self.env.ref('spreadsheet_dashboard_account_accountant.dashboard_accounting')
        next_uuid = 'next_revision_uuid'
        data = {
            'version': '18.5.10',  # current version
            'revisionId': next_uuid,
            'sheets': [
                {
                    'name': 'Sheet2',
                    'cells': {'A1': '=ODOO.FILTER.VALUE.V18("Year")'},
                },
                {
                    'name': 'Sheet2',
                    'cells': {'E1': '=ODOO.FILTER.VALUE.V18("Year")'},
                },
            ],
        }
        self.snapshot(dashboard, dashboard.current_revision_uuid, next_uuid, data)
        fixed_data = {
            'version': '18.5.10',  # current version
            'revisionId': next_uuid,
            'sheets': [
                {
                    'name': 'Sheet2',
                    'cells': {'A1': '=ODOO.FILTER.VALUE.V18("Year")'},
                },
                {
                    'name': 'Sheet2',
                    # only E1 should be fixed
                    'cells': {'E1': '=ODOO.FILTER.VALUE("Year")'},
                },
            ],
        }
        self.assertEqual(
            json.loads(dashboard._get_spreadsheet_serialized_snapshot()),
            fixed_data
        )

    def test_already_upgraded_and_already_fixed(self):
        dashboard = self.env.ref('spreadsheet_dashboard_account_accountant.dashboard_accounting')
        next_uuid = 'next_revision_uuid'
        data = {
            'revisionId': next_uuid,
            'version': '18.5.10',  # current version
            'sheets': [
                {
                    'name': 'Sheet 1',
                    'cells': {'A1': '=ODOO.FILTER.VALUE("some_filter")'},
                },
            ],
        }
        self.snapshot(dashboard, dashboard.current_revision_uuid, next_uuid, data)
        self.assertEqual(
            json.loads(dashboard._get_spreadsheet_serialized_snapshot()),
            data,
            "it should not change anything if already fixed"
        )
