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
    return {
      bg: new Color("#FFFFFF"),
      primary: new Color("#202124"),
      muted: new Color("#657286"),
      divider: new Color("#657286", 0.35),
      emoji: "🟢",
      statusSmall: "Світло є",
      statusMedium: "Світло є",
      statusLock: "Світло є",
      nextEventLine: data.nextStatusTime ? `Вимкнуть о ${nextTime}` : "Без відключень",
      mediumRightLabel: "Вимкнуть",
      mediumBottomLabel: "До відключення",
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
