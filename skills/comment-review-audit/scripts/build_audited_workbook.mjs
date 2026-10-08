#!/usr/bin/env node
/** Backfill audit results and build the user's group-settlement summary template. */

import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    if (!key.startsWith("--") || argv[index + 1] === undefined) throw new Error(`Invalid argument: ${key}`);
    result[key.slice(2)] = argv[index + 1];
  }
  for (const required of ["source", "expected", "report", "output"]) {
    if (!result[required]) throw new Error(`Missing --${required}`);
  }
  return result;
}

async function loadArtifactTool() {
  const roots = [process.env.CODEX_NODE_MODULES, process.env.NODE_PATH].filter(Boolean);
  if (!roots.length) throw new Error("Set CODEX_NODE_MODULES to the host-provided spreadsheet node_modules path");
  const require = createRequire(import.meta.url);
  const entry = require.resolve("@oai/artifact-tool", { paths: roots });
  return import(pathToFileURL(entry).href);
}

function derivedCategory(result) {
  if (result.result_category) return result.result_category;
  if (result.status === "verified") return result.expected_type === "text+image" ? "图字成功" : "成功";
  if (result.status === "partial_text") return "文字成功";
  if (result.status === "partial_image") return "图片成功";
  if (["not_found", "mismatch"].includes(result.status)) return "失败";
  return "待人审";
}

function derivedTriage(result) {
  if (result.triage) return result.triage;
  if (result.status === "verified") return "成功";
  if (["not_found", "mismatch"].includes(result.status)) return "失败";
  return "待人审";
}

function approvedAmount(result) {
  if (result.approved_amount !== undefined && result.approved_amount !== null && result.approved_amount !== "") {
    return Number(result.approved_amount);
  }
  if (derivedTriage(result) === "失败") return 0;
  if (result.status === "verified") return Number(result.expected_amount);
  return null;
}

function countBy(rows, valueOf, labels) {
  return Object.fromEntries(labels.map((label) => [label, rows.filter((row) => valueOf(row) === label).length]));
}

function safeFileName(value) {
  return value.replace(/[\\/:*?"<>|]/g, "_");
}

function quotedSheetName(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function clean(value, fallback = "") {
  const text = value === null || value === undefined ? "" : String(value).trim();
  return text || fallback;
}

function groupKey(leader, product) {
  return `${leader}\u0000${product}`;
}

async function renderAllSheets(workbook, sheetNames, previewDir, phase, summaryRange = "A1:N20") {
  if (!previewDir) return;
  const destination = path.join(previewDir, phase);
  await fs.mkdir(destination, { recursive: true });
  for (const sheetName of sheetNames) {
    const preview = await workbook.render({
      sheetName,
      ...(sheetName === "审核汇总" ? { range: summaryRange } : { autoCrop: "all" }),
      scale: 0.75,
      format: "png",
    });
    await fs.writeFile(
      path.join(destination, `${safeFileName(sheetName)}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
}

const args = parseArgs(process.argv.slice(2));
const { FileBlob, SpreadsheetFile } = await loadArtifactTool();
const expectedPayload = JSON.parse(await fs.readFile(args.expected, "utf8"));
const reportPayload = JSON.parse(await fs.readFile(args.report, "utf8"));
const expectedRows = expectedPayload.rows ?? expectedPayload;
const results = reportPayload.results ?? [];
if (!expectedRows.length) throw new Error("Expected rows are empty");
const byCase = new Map(results.map((item) => [item.case_id, item]));
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(args.source));
const sheetInfo = await workbook.inspect({ kind: "sheet", include: "name,range", maxChars: 20000 });
const sourceSheets = sheetInfo.ndjson.split("\n").filter(Boolean).map((line) => JSON.parse(line));
const sourceSheetNames = new Set(sourceSheets.map((item) => item.name));
await renderAllSheets(workbook, sourceSheets.map((item) => item.name), args["preview-dir"], "before");

for (const sheetRecord of sourceSheets) {
  if (sheetRecord.name === "审核汇总") continue;
  const rows = expectedRows.filter((item) => item.source_sheet === sheetRecord.name);
  if (!rows.length) continue;
  const maxRow = Math.max(...rows.map((item) => Number(item.source_row)));
  const matrix = Array.from({ length: maxRow - 1 }, () => [null, null]);
  for (const expected of rows) {
    const result = byCase.get(expected.case_id);
    if (!result) throw new Error(`Missing result for ${expected.case_id}`);
    matrix[Number(expected.source_row) - 2] = [
      derivedCategory(result),
      approvedAmount(result),
    ];
  }
  const sheet = workbook.worksheets.getItem(sheetRecord.name);
  sheet.getRange("G1").values = [["审核结果"]];
  // Legacy templates merge blank audit cells vertically; writing hidden cells
  // does not make every row visible. Unmerge only the authorized result area.
  sheet.getRange(`G2:H${maxRow}`).unmerge();
  sheet.getRange(`G2:H${maxRow}`).values = matrix;
  sheet.getRange(`G2:G${maxRow}`).format.wrapText = true;
  sheet.getRange(`H2:H${maxRow}`).format.numberFormat = "0.00";
}

let summarySheet;
const preservedManual = new Map();
if (sourceSheetNames.has("审核汇总")) {
  summarySheet = workbook.worksheets.getItem("审核汇总");
  try {
    const prior = summarySheet.getRange("A1:N500").values;
    for (const row of Array.isArray(prior) ? prior.slice(1) : []) {
      const leader = clean(row?.[0]);
      const product = clean(row?.[1]);
      if (leader && product && leader !== "合计") preservedManual.set(groupKey(leader, product), row.slice(10, 14));
    }
  } catch {
    // A malformed old summary should not block rebuilding the audit data.
  }
  try {
    for (const table of summarySheet.tables.items) table.delete();
  } catch {
    // No table collection or no existing tables.
  }
  summarySheet.getRange("A1:X1000").unmerge();
  summarySheet.getRange("A1:X1000").clear({ applyTo: "all" });
  summarySheet.deleteAllDrawings();
} else {
  summarySheet = workbook.worksheets.add("审核汇总");
}

const grouped = new Map();
for (const expected of expectedRows) {
  const leader = clean(expected.group_leader, "未填写");
  const product = clean(expected.product_name, clean(expected.source_sheet, "未填写"));
  const key = groupKey(leader, product);
  if (!grouped.has(key)) {
    grouped.set(key, {
      key,
      leader,
      product,
      sheets: new Set(),
      contact: "",
      settlementTime: "",
      settlementVoucher: "",
      qrCode: "",
    });
  }
  const group = grouped.get(key);
  group.sheets.add(clean(expected.source_sheet, "未填写"));
  group.contact ||= clean(expected.leader_contact);
  group.settlementTime ||= clean(expected.settlement_time);
  group.settlementVoucher ||= clean(expected.settlement_voucher);
  group.qrCode ||= clean(expected.qr_code) === "embedded-image.png" ? "见原工作表" : clean(expected.qr_code);
}
const groups = [...grouped.values()];
for (const group of groups) {
  const prior = preservedManual.get(group.key) ?? [];
  group.contact = clean(prior[0], group.contact);
  group.settlementTime = clean(prior[1], group.settlementTime);
  group.settlementVoucher = clean(prior[2], group.settlementVoucher);
  group.qrCode = clean(prior[3], group.qrCode);
}

const groupStartRow = 2;
const groupEndRow = groupStartRow + groups.length - 1;
const totalRow = groupEndRow + 1;
const detailStartRow = 12;
const detailEndRow = detailStartRow + expectedRows.length - 1;

summarySheet.getRange("A1:N1").values = [[
  "团长", "产品名", "涉及工作表数", "总条数", "成功数", "失败数", "需人审数", "成功率", "报销金额", "涉及工作表",
  "团长联系方式", "跟团长结算时间", "结算凭证", "二维码",
]];
summarySheet.getRange(`A${groupStartRow}:B${groupEndRow}`).values = groups.map((group) => [group.leader, group.product]);
summarySheet.getRange(`J${groupStartRow}:N${groupEndRow}`).values = groups.map((group) => [
  [...group.sheets].join("、"), group.contact, group.settlementTime, group.settlementVoucher, group.qrCode,
]);

summarySheet.getRange("P1:Q1").values = [["图文组合结果", "数量"]];
summarySheet.getRange("P2:P6").values = [["图字成功"], ["文字成功"], ["图片成功"], ["失败"], ["待人审"]];
summarySheet.getRange("P8:X9").merge();
summarySheet.getRange("P8").values = [["口径：成功 / 图字成功按整项金额结算；文字成功 / 图片成功在源表明确分项价格时按分项金额结算，否则进入需人审。原始中间证据保留14天，压缩归档再保留14天。"]];
summarySheet.getRange("P11:X11").values = [["案例ID", "团长", "产品名", "工作表", "原行", "要求类型", "结果分类", "分流", "核定金额"]];
summarySheet.getRange(`P${detailStartRow}:U${detailEndRow}`).values = expectedRows.map((expected) => [
  expected.case_id,
  clean(expected.group_leader, "未填写"),
  clean(expected.product_name, clean(expected.source_sheet, "未填写")),
  expected.source_sheet,
  Number(expected.source_row),
  expected.comment_type,
]);
summarySheet.getRange(`V${detailStartRow}:X${detailEndRow}`).formulas = expectedRows.map((expected, index) => {
  const detailRow = detailStartRow + index;
  const sourceSheet = quotedSheetName(expected.source_sheet);
  const sourceRow = Number(expected.source_row);
  return [
    `=${sourceSheet}!G${sourceRow}`,
    `=IF(OR(V${detailRow}=\"成功\",V${detailRow}=\"图字成功\",AND(OR(V${detailRow}=\"文字成功\",V${detailRow}=\"图片成功\"),X${detailRow}>0)),\"成功\",IF(V${detailRow}=\"失败\",\"失败\",\"待人审\"))`,
    `=${sourceSheet}!H${sourceRow}`,
  ];
});

summarySheet.getRange(`C${groupStartRow}:I${groupEndRow}`).formulas = groups.map((group, index) => {
  const row = groupStartRow + index;
  const leaderRange = `$Q$${detailStartRow}:$Q$${detailEndRow}`;
  const productRange = `$R$${detailStartRow}:$R$${detailEndRow}`;
  const triageRange = `$W$${detailStartRow}:$W$${detailEndRow}`;
  const amountRange = `$X$${detailStartRow}:$X$${detailEndRow}`;
  return [
    `=IF(J${row}=\"\",0,LEN(J${row})-LEN(SUBSTITUTE(J${row},\"、\",\"\"))+1)`,
    `=COUNTIFS(${leaderRange},A${row},${productRange},B${row})`,
    `=COUNTIFS(${leaderRange},A${row},${productRange},B${row},${triageRange},\"成功\")`,
    `=COUNTIFS(${leaderRange},A${row},${productRange},B${row},${triageRange},\"失败\")`,
    `=COUNTIFS(${leaderRange},A${row},${productRange},B${row},${triageRange},\"待人审\")`,
    `=IFERROR(E${row}/D${row},0)`,
    `=SUMIFS(${amountRange},${leaderRange},A${row},${productRange},B${row})`,
  ];
});

summarySheet.getRange(`A${totalRow}:N${totalRow}`).values = [["合计", null, null, null, null, null, null, null, null, null, null, null, null, null]];
summarySheet.getRange(`C${totalRow}:I${totalRow}`).formulas = [[
  `=SUM(C${groupStartRow}:C${groupEndRow})`,
  `=SUM(D${groupStartRow}:D${groupEndRow})`,
  `=SUM(E${groupStartRow}:E${groupEndRow})`,
  `=SUM(F${groupStartRow}:F${groupEndRow})`,
  `=SUM(G${groupStartRow}:G${groupEndRow})`,
  `=IFERROR(E${totalRow}/D${totalRow},0)`,
  `=SUM(I${groupStartRow}:I${groupEndRow})`,
]];

summarySheet.getRange("Q2:Q6").formulas = ["图字成功", "文字成功", "图片成功", "失败", "待人审"].map((category) => [
  `=COUNTIFS($U$${detailStartRow}:$U$${detailEndRow},\"text+image\",$V$${detailStartRow}:$V$${detailEndRow},\"${category}\")`,
]);

const table = summarySheet.tables.add(`A1:J${groupEndRow}`, true, "CommentAuditSummaryTable");
table.showFilterButton = true;
table.style = "TableStyleMedium2";
summarySheet.showGridLines = false;
summarySheet.freezePanes.freezeRows(1);

summarySheet.getRange(`A1:N${totalRow}`).format = {
  borders: { preset: "all", style: "thin", color: "#111827" },
  verticalAlignment: "center",
};
summarySheet.getRange("A1:J1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
summarySheet.getRange("K1:N1").format = {
  fill: "#F4CCCC",
  font: { color: "#111827" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
summarySheet.getRange(`A${groupStartRow}:I${groupEndRow}`).format.horizontalAlignment = "center";
summarySheet.getRange(`A${groupStartRow}:J${groupEndRow}`).format.fill = "#FFFFFF";
summarySheet.getRange(`J${groupStartRow}:N${groupEndRow}`).format = { wrapText: true, horizontalAlignment: "center" };
summarySheet.getRange(`A${totalRow}:I${totalRow}`).format = { fill: "#DDEBF7", font: { bold: true }, verticalAlignment: "center", horizontalAlignment: "center" };
summarySheet.getRange(`J${totalRow}:J${totalRow}`).format = { fill: "#DDEBF7", font: { bold: true }, verticalAlignment: "center" };
summarySheet.getRange(`H${groupStartRow}:H${totalRow}`).format.numberFormat = "0.0%";
summarySheet.getRange(`I${groupStartRow}:I${totalRow}`).format.numberFormat = "0.00";
summarySheet.getRange(`A1:N${totalRow}`).format.wrapText = true;
summarySheet.getRange("A1:N1").format.rowHeight = 30;
summarySheet.getRange(`A${groupStartRow}:N${groupEndRow}`).format.rowHeight = 44;
summarySheet.getRange(`A${totalRow}:N${totalRow}`).format.rowHeight = 44;
summarySheet.getRange(`A1:A${totalRow}`).format.columnWidth = 13;
summarySheet.getRange(`B1:B${totalRow}`).format.columnWidth = 15;
summarySheet.getRange(`C1:C${totalRow}`).format.columnWidth = 14;
summarySheet.getRange(`D1:F${totalRow}`).format.columnWidth = 9;
summarySheet.getRange(`G1:G${totalRow}`).format.columnWidth = 11;
summarySheet.getRange(`H1:H${totalRow}`).format.columnWidth = 10;
summarySheet.getRange(`I1:I${totalRow}`).format.columnWidth = 12;
summarySheet.getRange(`J1:J${totalRow}`).format.columnWidth = 38;
summarySheet.getRange(`K1:K${totalRow}`).format.columnWidth = 16;
summarySheet.getRange(`L1:L${totalRow}`).format.columnWidth = 18;
summarySheet.getRange(`M1:M${totalRow}`).format.columnWidth = 16;
summarySheet.getRange(`N1:N${totalRow}`).format.columnWidth = 18;

summarySheet.getRange("P1:Q1").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
summarySheet.getRange("P2:Q6").format.borders = { preset: "all", style: "thin", color: "#CBD5E1" };
summarySheet.getRange("P8:X9").format = { fill: "#FFF2CC", font: { color: "#7F6000" }, wrapText: true, verticalAlignment: "center" };
summarySheet.getRange("P11:X11").format = { fill: "#5B9BD5", font: { bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
summarySheet.getRange(`T${detailStartRow}:T${detailEndRow}`).format.numberFormat = "0";
summarySheet.getRange(`X${detailStartRow}:X${detailEndRow}`).format.numberFormat = "0.00";
summarySheet.getRange(`P11:X${detailEndRow}`).format.wrapText = true;

workbook.recalculate();
const summaryCheck = await workbook.inspect({
  kind: "table",
  sheetId: "审核汇总",
  range: `A1:N${totalRow}`,
  include: "values,formulas",
  tableMaxRows: Math.max(totalRow, 10),
  tableMaxCols: 14,
  maxChars: 16000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
});
const errorRecords = errors.ndjson.split("\n").filter(Boolean).map((line) => JSON.parse(line));
if (errorRecords.some((item) => item.kind !== "notice")) throw new Error("Workbook formula verification failed");
const expectedCounts = countBy(results, derivedTriage, ["成功", "失败", "待人审"]);
const expectedPayout = results.reduce((sum, item) => sum + Number(approvedAmount(item) || 0), 0);
const totals = summarySheet.getRange(`D${totalRow}:I${totalRow}`).values[0];
if (Number(totals[0]) !== results.length || Number(totals[1]) !== expectedCounts["成功"]
    || Number(totals[2]) !== expectedCounts["失败"] || Number(totals[3]) !== expectedCounts["待人审"]
    || !Number.isFinite(Number(totals[5])) || Math.abs(Number(totals[5]) - expectedPayout) > 0.001) {
  throw new Error("Workbook summary does not reconcile with review report");
}
await renderAllSheets(
  workbook,
  [...sourceSheets.map((item) => item.name), ...(sourceSheetNames.has("审核汇总") ? [] : ["审核汇总"])],
  args["preview-dir"],
  "after",
  `A1:N${totalRow}`,
);

await fs.mkdir(path.dirname(args.output), { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(args.output);

const triage = countBy(results, derivedTriage, ["成功", "失败", "待人审"]);
const comboRows = results.filter((item) => item.expected_type === "text+image");
const combo = countBy(comboRows, derivedCategory, ["图字成功", "文字成功", "图片成功", "失败", "待人审"]);
const payout = results
  .filter((item) => derivedTriage(item) === "成功")
  .reduce((sum, item) => sum + Number(approvedAmount(item) || 0), 0);
console.log(JSON.stringify({
  output: args.output,
  summary: { triage, combo, payout, groups: groups.length },
  summaryCheck: summaryCheck.ndjson,
  formulaErrors: errors.ndjson,
}));
