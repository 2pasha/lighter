// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-purple; icon-glyph: bolt;

/**
 * ⚡ Svitlo Bot — iOS Lock Screen & Home Screen Widget (OTA Loader)
 *
 * Як змінити чергу або локацію:
 * У налаштуваннях віджета в полі "Parameter" можна вказати:
 * - номер черги, наприклад: 3.2
 * - або назву вашої локації з бота, наприклад: Робота
 */

const BASE_URL = "__BASE_URL__";
const USER_ID = "__USER_ID__";
const TOKEN = "__TOKEN__";
const DEFAULT_LOCATION_ID = "__DEFAULT_LOCATION_ID__";

const widgetParam = (args.widgetParameter || "").trim();

async function loadAndRunWidget() {
  const fm = FileManager.local();
  const cachePath = fm.joinPath(fm.cacheDirectory(), "svitlo_ota_ui_v1.js");
  let uiCode = null;

  try {
    const req = new Request(`${BASE_URL}/api/widget/script?v=1`);
    req.timeoutInterval = 8;
    const fetched = await req.loadString();
    if (fetched && fetched.includes("SvitloWidgetRunner")) {
      uiCode = fetched;
      fm.writeString(cachePath, uiCode);
    }
  } catch (err) {
    console.warn("OTA fetch failed, trying local cache: " + err);
  }

  if (!uiCode && fm.fileExists(cachePath)) {
    uiCode = fm.readString(cachePath);
  }

  if (uiCode) {
    const runner = new Function(
      "ctx",
      `${uiCode}\nreturn SvitloWidgetRunner(ctx);`
    );
    await runner({
      baseUrl: BASE_URL,
      userId: USER_ID,
      token: TOKEN,
      defaultLocationId: DEFAULT_LOCATION_ID,
      widgetParam: widgetParam,
    });
  } else {
    const w = new ListWidget();
    w.setPadding(10, 12, 10, 12);
    const title = w.addText("⚡ Світло Бот");
    title.font = Font.boldSystemFont(13);
    w.addSpacer(4);
    const msg = w.addText("Немає зв'язку з сервером. Перевірте інтернет.");
    msg.font = Font.systemFont(11);
    msg.textColor = Color.gray();
    w.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);
    if (config.runsInWidget || config.runsInAccessoryWidget) {
      Script.setWidget(w);
    } else {
      await w.presentSmall();
    }
    Script.complete();
  }
}

await loadAndRunWidget();
