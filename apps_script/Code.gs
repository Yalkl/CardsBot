/**
 * CardsBot → Google Sheet, append only.
 *
 * Paste into the sheet: Extensions → Apps Script. Set a Script Property named SECRET
 * (Project Settings → Script properties) to the same value as SHEETS_WEBHOOK_SECRET on Railway.
 * Deploy → New deployment → Web app → Execute as: Me, Who has access: Anyone.
 *
 * The web app only appends rows. It never returns sheet contents, so the bot cannot read the list.
 */
var SHEET_NAME = "Contacts";
var COLUMNS = [
  ["scanned_at", "תאריך סריקה"],
  ["scanned_by", "נסרק ע״י"],
  ["name", "שם"],
  ["company", "חברה"],
  ["job_title", "תפקיד"],
  ["country", "מדינה"],
  ["phone", "טלפון"],
  ["email", "מייל"],
  ["whatsapp", "וואטסאפ"],
  ["telegram", "טלגרם"],
  ["website", "אתר"],
  ["address", "כתובת"],
  ["event", "אירוע"],
  ["note", "הערה"],
];

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    var data = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    var secret = PropertiesService.getScriptProperties().getProperty("SECRET");
    if (!secret || data.secret !== secret) return reply({ ok: false, error: "unauthorized" });

    lock.waitLock(10000);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(COLUMNS.map(function (c) { return c[1]; }));
      sheet.setFrozenRows(1);
      sheet.setRightToLeft(true);
    }
    // Text format so phone numbers keep their leading "+" and zeros.
    var row = COLUMNS.map(function (c) { return String(data[c[0]] || "").slice(0, 1000); });
    sheet.appendRow(row);
    sheet.getRange(sheet.getLastRow(), 1, 1, COLUMNS.length).setNumberFormat("@");
    return reply({ ok: true });
  } catch (err) {
    return reply({ ok: false, error: "failed" });
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
