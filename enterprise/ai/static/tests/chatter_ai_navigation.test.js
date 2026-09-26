import {
    click,
    contains,
    defineMailModels,
    insertText,
    openFormView,
    start,
    startServer,
} from "@mail/../tests/mail_test_helpers";
import { test } from "@odoo/hoot";
import {
    defineActions,
    defineMenus,
    MockServer,
    serverState,
} from "@web/../tests/web_test_helpers";
defineMailModels();
defineMenus([
    {
        id: 1,
        name: "Contacts",
        appID: 1,
        actionID: 100,
    },
]);
defineActions([
    {
        id: 100,
        xml_id: "action_contacts",
        name: "Contacts",
        res_model: "res.partner",
        views: [[false, "list"]],
    },
]);

test.tags("desktop");
test("closing the full composer after the AI agent navigated away should not crash", async () => {
    const pyEnv = await startServer();
    const partnerId = pyEnv["res.partner"].create({ name: "Abigail Peterson" });
    await start();
    await openFormView("res.partner", partnerId);
    await contains(".o-mail-Chatter");
    await click("button", { text: "Send message" });
    await insertText(".o-mail-Composer-input", "hey");
    await click("button[title='Open Full Composer']");
    await contains(".o_dialog .o_form_view");

    // Simulate the AI agent navigating to another view while the full
    // composer dialog is still open, the same way ai_natural_language_service
    // does it: this tears down the current form view, and with it the
    // chatter of the partner, underneath the still-open dialog.
    MockServer.env["bus.bus"]._sendone(serverState.partnerId, "AI_OPEN_MENU_LIST", { menuID: 1 });
    await contains(".o_list_view");
    await contains(".o-mail-Chatter", { count: 0 });
    // make sure composer dialog is closed
    await contains(".o_dialog", { count: 0 });
});
