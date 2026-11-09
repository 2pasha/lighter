/**
 * ⚡ Svitlo Bot — OTA UI Script for Scriptable (v3 — Pen.dev Design Uc4DJ)
 * Дизайн:
 * - Home Screen Small (170x170 style: Місце · черга + аватар, Статус, Великий відлік, Наступна подія, Оновлено)
 * - Home Screen Medium (364x170 style: Аватар + Місце · Черга, Статус + Великий відлік | Вертикальний розділювач + Тип зміни та час, Підпис + Оновлено)
 * - Lock Screen Rectangular (З круглим аватаром 32x32 зліва та 3 рядками статусу/відліку справа)
 * - Lock Screen Circular & Inline
 */

const AVATAR_B64 = "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";
const AVATAR_LS_B64 = "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";

let _cachedColorAvatar = null;
let _cachedLsAvatar = null;

function getBotAvatar(forLockScreen) {
  try {
    if (forLockScreen) {
      if (!_cachedLsAvatar) {
        _cachedLsAvatar = Image.fromData(Data.fromBase64String(AVATAR_LS_B64));
      }
      return _cachedLsAvatar;
    } else {
      if (!_cachedColorAvatar) {
        _cachedColorAvatar = Image.fromData(Data.fromBase64String(AVATAR_B64));
      }
      return _cachedColorAvatar;
    }
  } catch (e) {
    return null;
  }
}

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
  } else if (family === "medium") {
    widget = buildMediumWidget(data);
  } else {
    widget = buildSmallWidget(data);
  }

  // Розумне планування наступного фонового оновлення (кожні 15 хв або одразу після зміни слоту)
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

function formatDynamicCountdownUk(data) {
  const changeDate = parseNextChangeDate(data);
  if (changeDate) {
    const diffMs = changeDate.getTime() - Date.now();
    if (diffMs > 0) {
      const totalMins = Math.max(1, Math.ceil(diffMs / 60000));
      const h = Math.floor(totalMins / 60);
      const m = totalMins % 60;
      const parts = [];
      if (h > 0) parts.push(`${h} год`);
      if (m > 0) parts.push(`${m} хв`);
      return parts.length > 0 ? parts.join(" ") : "1 хв";
    }
  }
  return data.timeUntilNextChangeUk || data.timeUntilNextChange || "—";
}

function cleanLocationName(rawLabel) {
  if (!rawLabel) return "Дім";
  const cleaned = rawLabel.replace(/^[^\wА-Яа-яІіЇїЄєҐґ0-9]+/u, "").trim();
  return cleaned || "Дім";
}

function getTheme(data) {
  const isOnline = data.status === "online";
  const isPossible = data.slotState === "possible_off";
  const nextTime = data.nextStatusTime || "24:00";

  if (isOnline) {
    const hasOutage = Boolean(data.nextStatusTime);
    return {
      bg: new Color("#FFFFFF"),
      primary: new Color("#202124"),
      muted: new Color("#657286"),
      divider: new Color("#657286", 0.35),
      emoji: "🟢",
      statusSmall: "Світло є",
      statusMedium: "Світло є",
      statusLock: "Світло є",
      nextEventLine: hasOutage ? `Вимкнуть о ${nextTime}` : "До кінця доби",
      mediumRightLabel: hasOutage ? "Вимкнуть" : "До кінця доби",
      mediumBottomLabel: hasOutage ? "До відключення" : "Сьогодні без відключень",
    };
  }

  if (isPossible) {
    return {
      bg: new Color("#FFCA3A"),
      primary: new Color("#3D3000"),
      muted: new Color("#655218"),
      divider: new Color("#655218", 0.35),
      emoji: "🟡",
      statusSmall: "Можливе відкл.",
      statusMedium: "Можливе відключення",
      statusLock: "Можливе відкл.",
      nextEventLine: data.nextStatusTime ? `Можливо з ${nextTime}` : "До кінця доби",
      mediumRightLabel: "Можливо з",
      mediumBottomLabel: "До можливої зміни",
    };
  }

  return {
    bg: new Color("#1456D8"),
    primary: new Color("#FFFFFF"),
    muted: new Color("#DCE7FF"),
    divider: new Color("#DCE7FF", 0.35),
    emoji: "🔴",
    statusSmall: "Без світла",
    statusMedium: "Без світла",
    statusLock: "Без світла",
    nextEventLine: data.nextStatusTime ? `Увімкнуть о ${nextTime}` : "До кінця доби",
    mediumRightLabel: "Увімкнуть",
    mediumBottomLabel: "До повернення світла",
  };
}

// -----------------------------------------------------------------------------
// HOME SCREEN · SMALL (Node u4M5F / F3cH80 / YhysU)
// -----------------------------------------------------------------------------
function buildSmallWidget(data) {
  const w = new ListWidget();
  const t = getTheme(data);
  w.backgroundColor = t.bg;
  w.setPadding(16, 16, 16, 16);

  // 1. Верхній рядок: "Дім · 3.2" + круглий аватар бота (24x24)
  const topRow = w.addStack();
  topRow.layoutHorizontally();
  topRow.centerAlignContent();

  const locName = cleanLocationName(data.locationLabel);
  const queueStr = data.queue || "3.2";
  const placeTxt = topRow.addText(`${locName} · ${queueStr}`);
  placeTxt.font = Font.semiboldSystemFont(11);
  placeTxt.textColor = t.muted;
  placeTxt.lineLimit = 1;
  placeTxt.minimumScaleFactor = 0.75;

  topRow.addSpacer();

  const avatar = getBotAvatar(false);
  if (avatar) {
    const avImg = topRow.addImage(avatar);
    avImg.imageSize = new Size(24, 24);
    avImg.cornerRadius = 12;
    avImg.applyFittingContentMode();
  }

  w.addSpacer();

  // 2. Середній блок: Статус + Час до зміни (24pt bold) + Наступна подія
  const midStack = w.addStack();
  midStack.layoutVertically();
  midStack.spacing = 4;

  const statusTxt = midStack.addText(t.statusSmall);
  statusTxt.font = Font.semiboldSystemFont(12);
  statusTxt.textColor = t.primary;
  statusTxt.lineLimit = 1;

  const countdownStr = formatDynamicCountdownUk(data);
  const timeTxt = midStack.addText(countdownStr);
  timeTxt.font = Font.boldSystemFont(22);
  timeTxt.textColor = t.primary;
  timeTxt.lineLimit = 1;
  timeTxt.minimumScaleFactor = 0.65;

  const nextTxt = midStack.addText(t.nextEventLine);
  nextTxt.font = Font.systemFont(11);
  nextTxt.textColor = t.primary;
  nextTxt.lineLimit = 1;
  nextTxt.minimumScaleFactor = 0.75;

  w.addSpacer();

  // 3. Нижній рядок: "Оновлено 11:45"
  const updTime = data.updatedAtLocal || "";
  const updStr = updTime ? `Оновлено ${updTime}` : "Оновлено щойно";
  const footTxt = w.addText(data._fromCache ? `${updStr} · кеш` : updStr);
  footTxt.font = Font.systemFont(9);
  footTxt.textColor = t.muted;
  footTxt.lineLimit = 1;

  return w;
}

// -----------------------------------------------------------------------------
// HOME SCREEN · MEDIUM (Node AS4sx / mvihy / P6nlWC)
// -----------------------------------------------------------------------------
function buildMediumWidget(data) {
  const w = new ListWidget();
  const t = getTheme(data);
  w.backgroundColor = t.bg;
  w.setPadding(16, 16, 16, 16);

  // 1. Верхній рядок: Аватар (24x24) + "Дім · Черга 3.2"
  const topRow = w.addStack();
  topRow.layoutHorizontally();
  topRow.centerAlignContent();
  topRow.spacing = 8;

  const avatar = getBotAvatar(false);
  if (avatar) {
    const avImg = topRow.addImage(avatar);
    avImg.imageSize = new Size(24, 24);
    avImg.cornerRadius = 12;
    avImg.applyFittingContentMode();
  }

  const locName = cleanLocationName(data.locationLabel);
  const queueStr = data.queue || "3.2";
  const placeTxt = topRow.addText(`${locName} · Черга ${queueStr}`);
  placeTxt.font = Font.mediumSystemFont(12);
  placeTxt.textColor = t.muted;
  placeTxt.lineLimit = 1;

  topRow.addSpacer();

  w.addSpacer();

  // 2. Середній блок: Зліва Статус і Головний відлік (32pt), Справа вертикальна лінія + Тип зміни і Час (23pt)
  const midRow = w.addStack();
  midRow.layoutHorizontally();
  midRow.centerAlignContent();

  const leftCol = midRow.addStack();
  leftCol.layoutVertically();
  leftCol.spacing = 5;

  const stTxt = leftCol.addText(t.statusMedium);
  stTxt.font = Font.semiboldSystemFont(13);
  stTxt.textColor = t.primary;
  stTxt.lineLimit = 1;

  const countdownStr = formatDynamicCountdownUk(data);
  const mainCountdown = leftCol.addText(countdownStr);
  mainCountdown.font = Font.boldSystemFont(30);
  mainCountdown.textColor = t.primary;
  mainCountdown.lineLimit = 1;
  mainCountdown.minimumScaleFactor = 0.65;

  midRow.addSpacer();

  // Вертикальний розділювач
  const divider = midRow.addStack();
  divider.size = new Size(1, 46);
  divider.backgroundColor = t.divider;

  midRow.addSpacer(16);

  // Права колонка (Наступна зміна)
  const rightCol = midRow.addStack();
  rightCol.layoutVertically();
  rightCol.spacing = 5;
  rightCol.size = new Size(88, 0);

  const changeTypeTxt = rightCol.addText(t.mediumRightLabel);
  changeTypeTxt.font = Font.systemFont(11);
  changeTypeTxt.textColor = t.muted;
  changeTypeTxt.lineLimit = 1;

  const changeTimeTxt = rightCol.addText(data.nextStatusTime || "24:00");
  changeTimeTxt.font = Font.semiboldSystemFont(23);
  changeTimeTxt.textColor = t.primary;
  changeTimeTxt.lineLimit = 1;
  changeTimeTxt.minimumScaleFactor = 0.75;

  w.addSpacer();

  // 3. Нижній рядок: "До відключення" зліва та "Оновлено 11:45" справа
  const botRow = w.addStack();
  botRow.layoutHorizontally();
  botRow.centerAlignContent();

  const subMeaningTxt = botRow.addText(t.mediumBottomLabel);
  subMeaningTxt.font = Font.systemFont(10);
  subMeaningTxt.textColor = t.muted;
  subMeaningTxt.lineLimit = 1;

  botRow.addSpacer();

  const updTime = data.updatedAtLocal || "";
  const updStr = updTime ? `Оновлено ${updTime}` : "Оновлено щойно";
  const updTxt = botRow.addText(data._fromCache ? `${updStr} · кеш` : updStr);
  updTxt.font = Font.systemFont(9);
  updTxt.textColor = t.muted;
  updTxt.lineLimit = 1;

  return w;
}

// -----------------------------------------------------------------------------
// LOCK SCREEN · RECTANGULAR З АВАТАРОМ (Node V8eJl / wetE7 / X3CN8)
// -----------------------------------------------------------------------------
function buildRectangularWidget(data) {
  const w = new ListWidget();
  w.setPadding(2, 0, 2, 0);
  const t = getTheme(data);

  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  row.spacing = 8;

  // Круглий аватар бота 32x32
  const avatar = getBotAvatar(true);
  if (avatar) {
    const avImg = row.addImage(avatar);
    avImg.imageSize = new Size(32, 32);
    avImg.cornerRadius = 16;
    avImg.applyFittingContentMode();
  }

  // Права частина: Поточний стан (10pt) + Відлік часу (20pt bold) + Наступна подія (10pt)
  const col = row.addStack();
  col.layoutVertically();
  col.spacing = 2;

  const stTxt = col.addText(t.statusLock);
  stTxt.font = Font.semiboldSystemFont(10);
  stTxt.textColor = Color.white();
  stTxt.lineLimit = 1;

  const countdownStr = formatDynamicCountdownUk(data);
  const cdTxt = col.addText(countdownStr);
  cdTxt.font = Font.boldSystemFont(19);
  cdTxt.textColor = Color.white();
  cdTxt.lineLimit = 1;
  cdTxt.minimumScaleFactor = 0.65;

  const nextTxt = col.addText(t.nextEventLine);
  nextTxt.font = Font.systemFont(10);
  nextTxt.textColor = new Color("#DAE1EB");
  nextTxt.textOpacity = 0.88;
  nextTxt.lineLimit = 1;
  nextTxt.minimumScaleFactor = 0.75;

  row.addSpacer();

  return w;
}

// -----------------------------------------------------------------------------
// LOCK SCREEN · CIRCULAR & INLINE
// -----------------------------------------------------------------------------
function buildCircularWidget(data) {
  const w = new ListWidget();
  w.setPadding(4, 4, 4, 4);

  const stack = w.addStack();
  stack.layoutVertically();
  stack.centerAlignContent();

  const avatar = getBotAvatar(true);
  if (avatar) {
    const avImg = stack.addImage(avatar);
    avImg.imageSize = new Size(16, 16);
    avImg.cornerRadius = 8;
    avImg.applyFittingContentMode();
    stack.addSpacer(2);
  }

  const timeStr = data.nextStatusTime || "24:00";
  const mainTxt = stack.addText(timeStr);
  mainTxt.font = Font.boldRoundedSystemFont(12);
  mainTxt.minimumScaleFactor = 0.7;

  const subStr = data.timeUntilNextChange || "—";
  const subTxt = stack.addText(subStr);
  subTxt.font = Font.systemFont(9);
  subTxt.textOpacity = 0.85;
  subTxt.minimumScaleFactor = 0.6;

  return w;
}

function buildInlineWidget(data) {
  const w = new ListWidget();
  const t = getTheme(data);
  const cd = formatDynamicCountdownUk(data);
  w.addText(`${t.emoji} ${t.statusLock} · ${cd} (${t.nextEventLine})`);
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
