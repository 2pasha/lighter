/**
 * ⚡ Svitlo Bot — OTA UI Script for Scriptable (v2 — Live Timer)
 * Підтримує:
 * - Системний живий зворотний відлік iOS (applyTimerStyle / applyRelativeStyle)
 * - Розумне планування refreshAfterDate під час самого перемикання світла
 * - Lock Screen Rectangular, Circular, Inline та Home Screen
 */

async function SvitloWidgetRunner(ctx) {
  const fm = FileManager.local();
  const dataCachePath = fm.joinPath(
    fm.cacheDirectory(),
    `svitlo_data_${ctx.userId}_${(ctx.widgetParam || "default").replace(/[^a-zA-Z0-9._-]/g, "_")}.json`
  );

  let query = `userId=${encodeURIComponent(ctx.userId)}&token=${encodeURIComponent(ctx.token || "")}`;
  if (ctx.widgetParam) {
    query += `&param=${encodeURIComponent(ctx.widgetParam)}`;
  } else if (ctx.defaultLocationId) {
    query += `&locationId=${encodeURIComponent(ctx.defaultLocationId)}`;
  }

  const dataUrl = `${ctx.baseUrl}/api/widget/data?${query}`;
  let data = null;

  try {
    const req = new Request(dataUrl);
    req.timeoutInterval = 10;
    const json = await req.loadJSON();
    if (json && json.status) {
      data = json;
      fm.writeString(dataCachePath, JSON.stringify(json));
    }
  } catch (e) {
    console.warn("Data fetch error: " + e);
  }

  if (!data && fm.fileExists(dataCachePath)) {
    try {
      data = JSON.parse(fm.readString(dataCachePath));
      data._fromCache = true;
    } catch (e) {}
  }

  const family = config.widgetFamily || "small";
  let widget;

  if (!data) {
    widget = buildErrorWidget(family);
  } else if (family === "accessoryCircular") {
    widget = buildCircularWidget(data);
  } else if (family === "accessoryRectangular") {
    widget = buildRectangularWidget(data);
  } else if (family === "accessoryInline") {
    widget = buildInlineWidget(data);
  } else {
    widget = buildHomeScreenWidget(data, family);
  }

  // Розумне планування наступного фонового оновлення:
  // За замовчуванням через 15 хвилин, але якщо зміна світла настане раніше —
  // просимо iOS оновити віджет одразу після моменту перемикання (+10 сек).
  const nowMs = Date.now();
  let nextRefreshMs = nowMs + 15 * 60 * 1000;
  const changeDate = parseNextChangeDate(data);
  if (changeDate) {
    const changeMs = changeDate.getTime() + 10 * 1000;
    if (changeMs > nowMs + 30 * 1000 && changeMs < nextRefreshMs) {
      nextRefreshMs = changeMs;
    }
  }
  widget.refreshAfterDate = new Date(nextRefreshMs);

  if (config.runsInWidget || config.runsInAccessoryWidget) {
    Script.setWidget(widget);
  } else {
    await widget.presentSmall();
  }
  Script.complete();
}

function parseNextChangeDate(data) {
  if (!data || !data.nextChangeAt) return null;
  const d = new Date(data.nextChangeAt);
  if (isNaN(d.getTime())) return null;
  if (d.getTime() <= Date.now()) return null;
  return d;
}

function getStatusVisuals(data) {
  const isOnline = data.status === "online";
  const isPossible = data.slotState === "possible_off";

  if (isOnline) {
    return {
      icon: "bolt.fill",
      emoji: "🟢",
      shortTitle: "Світло є",
      accentColor: new Color("#22c55e"),
      bgGradient: [new Color("#0f291e"), new Color("#091711")],
      nextPrefix: "До вимкнення",
      atPrefix: "Вимкнуть о",
    };
  }
  if (isPossible) {
    return {
      icon: "bolt.trianglebadge.exclamationmark.fill",
      emoji: "🟡",
      shortTitle: "Можливе відкл.",
      accentColor: new Color("#eab308"),
      bgGradient: [new Color("#29210f"), new Color("#171209")],
      nextPrefix: "До увімкнення",
      atPrefix: "Увімкнуть о",
    };
  }
  return {
    icon: "bolt.slash.fill",
    emoji: "🔴",
    shortTitle: "Без світла",
    accentColor: new Color("#ef4444"),
    bgGradient: [new Color("#2b1111"), new Color("#170909")],
    nextPrefix: "До увімкнення",
    atPrefix: "Увімкнуть о",
  };
}

function buildInlineWidget(data) {
  const w = new ListWidget();
  const v = getStatusVisuals(data);
  const nextPart = data.nextStatusTime
    ? ` до ${data.nextStatusTime} (${data.timeUntilNextChangeUk || data.timeUntilNextChange})`
    : "";
  w.addText(`${v.emoji} ${data.queue ? "Ч." + data.queue : data.currentGroup}: ${v.shortTitle}${nextPart}`);
  return w;
}

function buildCircularWidget(data) {
  const w = new ListWidget();
  w.setPadding(4, 4, 4, 4);
  const v = getStatusVisuals(data);
  const changeDate = parseNextChangeDate(data);

  const stack = w.addStack();
  stack.layoutVertically();
  stack.centerAlignContent();

  const iconRow = stack.addStack();
  iconRow.centerAlignContent();
  const sym = SFSymbol.named(v.icon);
  if (sym) {
    const img = iconRow.addImage(sym.image);
    img.imageSize = new Size(14, 14);
    img.tintColor = Color.white();
  }
  iconRow.addSpacer(2);
  const qTxt = iconRow.addText(data.queue || "");
  qTxt.font = Font.boldSystemFont(10);
  qTxt.minimumScaleFactor = 0.7;

  stack.addSpacer(1);

  const timeStr = data.nextStatusTime || "24:00";
  const mainTxt = stack.addText(timeStr);
  mainTxt.font = Font.boldRoundedSystemFont(13);
  mainTxt.minimumScaleFactor = 0.7;

  if (changeDate) {
    const timerTxt = stack.addDate(changeDate);
    timerTxt.applyTimerStyle();
    timerTxt.font = Font.monospacedDigitSystemFont(9, 0.2);
    timerTxt.textOpacity = 0.85;
    timerTxt.minimumScaleFactor = 0.6;
    timerTxt.centerAlignText();
  } else {
    const subStr = data.timeUntilNextChange || "—";
    const subTxt = stack.addText(subStr);
    subTxt.font = Font.systemFont(9);
    subTxt.textOpacity = 0.8;
    subTxt.minimumScaleFactor = 0.6;
  }

  return w;
}

function buildRectangularWidget(data) {
  const w = new ListWidget();
  w.setPadding(2, 4, 2, 4);
  const v = getStatusVisuals(data);
  const changeDate = parseNextChangeDate(data);

  // Рядок 1: Іконка + Статус + Черга
  const header = w.addStack();
  header.layoutHorizontally();
  header.centerAlignContent();

  const sym = SFSymbol.named(v.icon);
  if (sym) {
    const img = header.addImage(sym.image);
    img.imageSize = new Size(13, 13);
    img.tintColor = Color.white();
    header.addSpacer(4);
  }

  const titleTxt = header.addText(`${v.shortTitle} • ${data.currentGroupUk || data.currentGroup}`);
  titleTxt.font = Font.boldSystemFont(12);
  titleTxt.minimumScaleFactor = 0.75;
  titleTxt.lineLimit = 1;

  w.addSpacer(2);

  // Рядок 2: Коли наступна зміна + живий таймер зворотного відліку
  const line2 = w.addStack();
  line2.layoutHorizontally();
  line2.centerAlignContent();

  if (data.nextStatusTime && changeDate) {
    const prefixTxt = line2.addText(`${v.atPrefix} ${data.nextStatusTime} (`);
    prefixTxt.font = Font.systemFont(11);
    prefixTxt.minimumScaleFactor = 0.75;
    prefixTxt.lineLimit = 1;

    const timerTxt = line2.addDate(changeDate);
    timerTxt.applyTimerStyle();
    timerTxt.font = Font.monospacedDigitSystemFont(11, 0.3);
    timerTxt.minimumScaleFactor = 0.75;

    const suffixTxt = line2.addText(")");
    suffixTxt.font = Font.systemFont(11);
  } else if (data.nextStatusTime) {
    const dur = data.timeUntilNextChangeUk || data.timeUntilNextChange;
    const nextTxt = line2.addText(`${v.atPrefix} ${data.nextStatusTime} (${dur})`);
    nextTxt.font = Font.systemFont(11);
    nextTxt.minimumScaleFactor = 0.75;
    nextTxt.lineLimit = 1;
  } else {
    const nextTxt = line2.addText(
      data.status === "online" ? "Без відключень до кінця доби" : "До кінця доби"
    );
    nextTxt.font = Font.systemFont(11);
    nextTxt.minimumScaleFactor = 0.75;
    nextTxt.lineLimit = 1;
  }

  w.addSpacer(2);

  // Рядок 3: Назва локації та всього годин без світла
  const footer = w.addStack();
  footer.layoutHorizontally();
  footer.centerAlignContent();

  const locName = data.locationLabel ? data.locationLabel.replace(/^[^\wА-Яа-яІіЇїЄєҐґ]+/, "").trim() : "";
  const totalOff = data.totalOutageTimeUk || "";
  const footerParts = [];
  if (locName) footerParts.push(locName);
  if (totalOff) footerParts.push(`Всього: ${totalOff}`);
  if (data._fromCache) footerParts.push("кеш");

  const footTxt = footer.addText(footerParts.join(" • ") || "Вінницяобленерго");
  footTxt.font = Font.systemFont(10);
  footTxt.textOpacity = 0.7;
  footTxt.minimumScaleFactor = 0.75;
  footTxt.lineLimit = 1;

  return w;
}

function buildHomeScreenWidget(data, family) {
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  const v = getStatusVisuals(data);
  const changeDate = parseNextChangeDate(data);

  const grad = new LinearGradient();
  grad.colors = v.bgGradient;
  grad.locations = [0.0, 1.0];
  w.backgroundGradient = grad;

  // Верхній рядок: Локація + бейдж черги
  const topRow = w.addStack();
  topRow.layoutHorizontally();
  topRow.centerAlignContent();

  const locTxt = topRow.addText(data.locationLabel || "⚡ Світло");
  locTxt.font = Font.boldSystemFont(12);
  locTxt.textColor = new Color("#e2e8f0");
  locTxt.lineLimit = 1;
  locTxt.minimumScaleFactor = 0.75;

  topRow.addSpacer();

  const badge = topRow.addStack();
  badge.backgroundColor = new Color("#ffffff", 0.14);
  badge.cornerRadius = 6;
  badge.setPadding(2, 6, 2, 6);
  const badgeTxt = badge.addText(data.queue ? `Ч. ${data.queue}` : data.currentGroup);
  badgeTxt.font = Font.boldSystemFont(10);
  badgeTxt.textColor = Color.white();

  w.addSpacer();

  // Центральний блок: Статус
  const statusRow = w.addStack();
  statusRow.layoutHorizontally();
  statusRow.centerAlignContent();

  const sym = SFSymbol.named(v.icon);
  if (sym) {
    const img = statusRow.addImage(sym.image);
    img.imageSize = new Size(20, 20);
    img.tintColor = v.accentColor;
    statusRow.addSpacer(6);
  }

  const stTxt = statusRow.addText(v.shortTitle);
  stTxt.font = Font.boldSystemFont(16);
  stTxt.textColor = v.accentColor;
  stTxt.minimumScaleFactor = 0.8;

  w.addSpacer(4);

  // Інформація про наступну зміну + живий таймер
  if (data.nextStatusTime) {
    const nextLabel = w.addText(`${v.atPrefix} ${data.nextStatusTime}`);
    nextLabel.font = Font.semiboldSystemFont(13);
    nextLabel.textColor = Color.white();

    const timerRow = w.addStack();
    timerRow.layoutHorizontally();
    timerRow.centerAlignContent();

    if (changeDate) {
      const remPrefix = timerRow.addText("через ");
      remPrefix.font = Font.systemFont(12);
      remPrefix.textColor = new Color("#94a3b8");

      const liveTimer = timerRow.addDate(changeDate);
      liveTimer.applyTimerStyle();
      liveTimer.font = Font.monospacedDigitSystemFont(12, 0.3);
      liveTimer.textColor = new Color("#e2e8f0");
    } else {
      const remLabel = timerRow.addText(`через ${data.timeUntilNextChangeUk || data.timeUntilNextChange}`);
      remLabel.font = Font.systemFont(12);
      remLabel.textColor = new Color("#94a3b8");
    }
  } else {
    const allDay = w.addText(
      data.status === "online" ? "Відключень більше немає 🎉" : "Відключення до 24:00"
    );
    allDay.font = Font.systemFont(12);
    allDay.textColor = new Color("#94a3b8");
  }

  w.addSpacer();

  // Нижній рядок: Загалом без світла + час оновлення
  const botRow = w.addStack();
  botRow.layoutHorizontally();
  botRow.centerAlignContent();

  const updTime = data.updatedAtLocal || "";
  const footStr = data.totalOutageTimeUk
    ? `Без світла: ${data.totalOutageTimeUk}`
    : "ГПВ Вінниця";
  const footTxt = botRow.addText(footStr);
  footTxt.font = Font.systemFont(10);
  footTxt.textColor = new Color("#64748b");
  footTxt.minimumScaleFactor = 0.7;

  botRow.addSpacer();

  if (updTime) {
    const timeTxt = botRow.addText(updTime);
    timeTxt.font = Font.systemFont(9);
    timeTxt.textColor = new Color("#64748b");
  }

  return w;
}

function buildErrorWidget(family) {
  const w = new ListWidget();
  const title = w.addText("⚡ Світло Бот");
  title.font = Font.boldSystemFont(12);
  const sub = w.addText("Не вдалося завантажити графік");
  sub.font = Font.systemFont(10);
  sub.textOpacity = 0.7;
  return w;
}
