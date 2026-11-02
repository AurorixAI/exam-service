const fs = require("fs");
const { pathToFileURL } = require("url");
const katex = require("katex");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const cssPath = process.env.KATEX_CSS_PATH;

if (!cssPath) {
  throw new Error("KATEX_CSS_PATH is required");
}

const formulas = Array.isArray(input.formulas) ? input.formulas : [input];
const rowHeight = Number(input.rowHeight) || 144;
// One formula that KaTeX refuses must not take the whole batch with it (EX-15): its row stays empty (the rows below keep their
// place on the page), its index is named in the <!--failed:..--> marker and the caller sends only that formula to its fallback.
// ``throwOnError`` stays true on purpose: false would print the red TeX source into the exam paper.
const failed = [];
const rows = formulas.map((item, index) => {
  let formula = "";
  try {
    formula = katex.renderToString(item.latex, {
      displayMode: false,
      output: "html",
      strict: "ignore",
      throwOnError: true,
      trust: false,
    });
  } catch (error) {
    failed.push(index);
  }
  return `<div class="formula-row" data-index="${index}" style="color: ${item.color}; font-size: ${item.fontSize}px">${formula}</div>`;
}).join("");

process.stdout.write(`<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <!--failed:${failed.join(",")}-->
    <link rel="stylesheet" href="${pathToFileURL(cssPath).href}">
    <style>
      html, body { margin: 0; padding: 0; background: transparent; overflow: hidden; }
      body { width: 1600px; }
      .formula-row {
        box-sizing: border-box;
        display: flex;
        align-items: center;
        height: ${rowHeight}px;
        padding: 12px;
        line-height: 1;
        white-space: nowrap;
      }
      .formula-row .katex { font-size: 1em; }
    </style>
  </head>
  <body>${rows}</body>
</html>`);
