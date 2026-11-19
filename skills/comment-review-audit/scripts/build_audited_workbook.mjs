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
  if (result.status === "carried_forward") return result.result_category || "";
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

const settlementNotes = new Map([
  ["pending_duplicate_claim", "多行共用评论证据，金额暂缓核定"],
  ["pending_source_amount", "原表缺少可用报价，金额暂缓核定"],
]);

function approvedAmount(result) {
  if (result.status === "carried_forward") {
    return result.approved_amount == null ? null : Number(result.approved_amount);
  }
  if (settlementNotes.has(result.settlement_status)) {
    if (derivedTriage(result) !== "成功" || (result.approved_amount != null && result.approved_amount !== "")) {
      throw new Error("Invalid pending settlement");
    }
    return null;
  }
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

function splitAuditMerges(sheet, ranges, columns, active, lastRow) {
  const columnIndex = (name) => [...name].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
  const allowed = columns.map(columnIndex);
  for (const address of ranges) {
    const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(address);
    if (!match) throw new Error(`Invalid merged range ${address}`);
    const [, left, first, right, last] = match;
    const start = Number(first), end = Number(last);
    const x = columnIndex(left), y = columnIndex(right);
    if (!allowed.some((v) => x <= v && v <= y) || !active.some((r) => start <= r && r <= end)) continue;
    if (start < 2 || end > lastRow || y - x + 1 !== allowed.filter((v) => x <= v && v <= y).length) {
      throw new Error(`Merge extends outside authorized result cells: ${address}`);
    }
    const anchor = sheet.getRange(`${left}${start}`);
    const value = anchor.values[0][0], formula = anchor.formulas[0][0];
    sheet.getRange(address).unmerge();
    let head = start;
    for (const stop of [...active.filter((n) => start <= n && n <= end), end + 1]) {
      if (head < stop) {
        const ref = `${left}${head}`;
        const cell = sheet.getRange(ref);
        if (head !== start && cell.values[0][0] == null && !cell.formulas[0][0]) {
          if (value != null) cell.values = [[value]];
          else if (formula) cell.formulas = [[formula]];
        }
        if (head + 1 < stop || left !== right) sheet.getRange(`${ref}:${right}${stop - 1}`).merge();
      }
      head = stop + 1;
    }
  }
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
const mergeInfo = await workbook.inspect({ select: "$.sheets[*]", include: ["name", "mergedRanges"] });
const mergedBySheet = new Map(mergeInfo.records.map((record) => [record.name, record.mergedRanges]));
await renderAllSheets(workbook, sourceSheets.map((item) => item.name), args["preview-dir"], "before");

const layouts = new Map();
for (const sheetRecord of sourceSheets) {
  if (sheetRecord.name === "审核汇总") continue;
  const rows = expectedRows.filter((item) => item.source_sheet === sheetRecord.name);
  if (!rows.length) continue;
  const active = rows.filter((row) => {
    const result = byCase.get(row.case_id);
    if (!result) throw new Error(`Missing result for ${row.case_id}`);
    if (row.prior_review && (row.prior_review.triage !== "成功") !== (result.status === "carried_forward")) {
      throw new Error(`Invalid recheck selection for ${row.case_id}`);
    }
    return result.status !== "carried_forward";
  });
  if (!active.length) continue;
  const sheet = workbook.worksheets.getItem(sheetRecord.name);
  const prior = active[0].prior_review;
  const categoryColumn = prior ? prior.result_column : "G";
  let amountColumn = prior ? prior.amount_column : "H";
  if (!amountColumn) {
    const used = sheet.getUsedRange();
    let next = used.columnIndex + used.columnCount + 1;
    if (!Number.isInteger(next) || next < 1 || next > 16384) throw new Error("No room for settlement column");
    amountColumn = "";
    while (next > 0) {
      next -= 1;
      amountColumn = String.fromCharCode(65 + next % 26) + amountColumn;
      next = Math.floor(next / 26);
    }
    sheet.getRange(`${amountColumn}1`).values = [["核定金额"]];
  }
  if (!categoryColumn || categoryColumn === amountColumn) throw new Error("Invalid audit output columns");
  layouts.set(sheetRecord.name, [categoryColumn, amountColumn]);
  if (!prior) sheet.getRange("G1").values = [["审核结果"]];
  active.sort((a, b) => a.source_row - b.source_row);
  splitAuditMerges(sheet, mergedBySheet.get(sheetRecord.name) || [], [categoryColumn, amountColumn],
                  active.map((row) => row.source_row), Math.max(...rows.map((row) => row.source_row)));
  for (let start = 0; start < active.length;) {
    let stop = start + 1;
    while (stop < active.length && active[stop].source_row === active[stop - 1].source_row + 1) stop++;
    const first = active[start].source_row, last = active[stop - 1].source_row;
    const block = active.slice(start, stop).map((row) => byCase.get(row.case_id));
    const category = sheet.getRange(`${categoryColumn}${first}:${categoryColumn}${last}`);
    const amount = sheet.getRange(`${amountColumn}${first}:${amountColumn}${last}`);
    category.values = block.map((result) => [derivedCategory(result)]);
    amount.values = block.map((result) => [approvedAmount(result)]);
    category.format.wrapText = true;
    amount.format.numberFormat = "0.00";
    start = stop;
  }
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
  if (summarySheet.getRange("Y11").values?.[0]?.[0] === "结算说明") {
    summarySheet.getRange("Y:Y").clear({ applyTo: "contents" });
  }
  if (summarySheet.getRange("Z11").values?.[0]?.[0] === "原任务报价") {
    summarySheet.getRange("Z:Z").clear({ applyTo: "contents" });
  }
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
if (results.some((result) => result.triage === "未复核")) {
  summarySheet.getRange("H1").values = [["成功率（有审核结果）"]];
  summarySheet.getRange("P7").values = [["未复核（原结果空白或未识别）"]];
  summarySheet.getRange("Q7").formulas = [[`=COUNTIF($W$${detailStartRow}:$W$${detailEndRow},"未复核")`]];
}
summarySheet.getRange("P8:X9").merge();
summarySheet.getRange("P8").values = [["成功 / 图字成功按原表整项报价结算，文字成功 / 图片成功按明确的分项报价结算。共用证据或缺少报价的成功行可在明细中查看结算说明。原始中间证据保留14天，压缩归档再保留14天。"]];
summarySheet.getRange("P11:X11").values = [["案例ID", "团长", "产品名", "工作表", "原行", "要求类型", "结果分类", "分流", "核定金额"]];
summarySheet.getRange("Z11").values = [["原任务报价"]];
summarySheet.getRange(`Z${detailStartRow}:Z${detailEndRow}`).values = expectedRows.map((row) => {
  const quote = row.expected_amount == null ? null : (byCase.get(row.case_id).expected_amount ?? row.expected_amount);
  const value = Number(quote);
  return [clean(quote) && Number.isFinite(value) && value >= 0 ? value : null];
});
if (results.some((result) => settlementNotes.has(result.settlement_status))) {
  summarySheet.getRange("Y11").values = [["结算说明"]];
  summarySheet.getRange(`Y${detailStartRow}:Y${detailEndRow}`).values = expectedRows.map((expected) => [
    settlementNotes.get(byCase.get(expected.case_id).settlement_status) ?? null,
  ]);
  summarySheet.getRange("Y11").format.columnWidth = 42;
}
summarySheet.getRange(`P${detailStartRow}:U${detailEndRow}`).values = expectedRows.map((expected) => [
  expected.case_id,
  clean(expected.group_leader, "未填写"),
  clean(expected.product_name, clean(expected.source_sheet, "未填写")),
  expected.source_sheet,
  Number(expected.source_row),
  expected.comment_type,
]);
summarySheet.getRange(`V${detailStartRow}:X${detailEndRow}`).formulas = expectedRows.map((expected, index) => {
  if (byCase.get(expected.case_id).status === "carried_forward") return [null, null, null];
  const detailRow = detailStartRow + index;
  const sourceSheet = quotedSheetName(expected.source_sheet);
  const sourceRow = Number(expected.source_row);
  const [categoryColumn, amountColumn] = layouts.get(expected.source_sheet);
  const priceCell = `${sourceSheet}!${amountColumn}${sourceRow}`;
  const hasPrice = `ISNUMBER(${priceCell})`;
  const partialApproved = settlementNotes.has(byCase.get(expected.case_id).settlement_status)
    ? `OR(${hasPrice},Y${detailRow}<>"")` : hasPrice;
  return [
    `=${sourceSheet}!${categoryColumn}${sourceRow}`,
    `=IF(OR(V${detailRow}="成功",V${detailRow}="图字成功",AND(OR(V${detailRow}="文字成功",V${detailRow}="图片成功"),${partialApproved})),"成功",IF(V${detailRow}="失败","失败","待人审"))`,
    `=IF(${hasPrice},${priceCell},"")`,
  ];
});
for (const [index, expected] of expectedRows.entries()) {
  const result = byCase.get(expected.case_id);
  if (result.status !== "carried_forward") continue;
  const row = index + detailStartRow;
  summarySheet.getRange(`V${row}:X${row}`).values = [[result.result_category || null, result.triage, approvedAmount(result)]];
}

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
    `=IFERROR(E${row}/SUM(E${row}:G${row}),0)`,
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
  `=IFERROR(E${totalRow}/SUM(E${totalRow}:G${totalRow}),0)`,
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

const triage = countBy(results, derivedTriage, results.some((r) => r.triage === "未复核") ? ["成功", "失败", "待人审", "未复核"] : ["成功", "失败", "待人审"]);
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
