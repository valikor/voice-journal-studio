// Edit notation: "/" splits a turn, "c:/d:/m:/o:" pins a speaker. Parsing the same text twice must give the same turns.
// node tests/test_notation.js   (reads the real functions out of web/app.js)
const fs = require("fs"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const grab = (name) => { const i = src.indexOf("function " + name); let d = 0, j = src.indexOf("{", i); const s = j; for (; j < src.length; j++) { if (src[j] === "{") d++; if (src[j] === "}" && --d === 0) break; } return src.slice(i, j + 1); };
const SPK = ["kid", "mom", "dad", "other"], NAMES = { kid: "K", mom: "M", dad: "D", other: "O" };
const SPLIT_RE = /\s*(?<!\d)[\/|](?!\d)\s*/;
eval(grab("speakerPrefix") + grab("splitParts") + grab("parseCardText") + "; globalThis.sp = splitParts; globalThis.pc = parseCardText;");
let bad = 0; const eq = (a, b, n) => { const ok = JSON.stringify(a) === JSON.stringify(b); if (!ok) bad++; console.log(ok ? "PASS" : "FAIL", n, ok ? "" : JSON.stringify(a)); };
eq(sp("i don't like it. / c: why / d: but it's okay", "dad", "dad"), [{ text: "i don't like it.", speaker: "dad" }, { text: "why", speaker: "kid" }, { text: "but it's okay", speaker: "dad" }], "label after slash");
eq(sp("a / b / c", "kid", "dad").map((x) => x.speaker), ["kid", "dad", "kid"], "unlabelled turns alternate");
eq(sp("d: one / d: two", "kid", "dad").map((x) => x.speaker), ["dad", "dad"], "explicit labels win");
eq(sp("no split here", "kid", "dad"), null, "no slash = no split");
eq(sp("it was 1/2 done", "kid", "dad"), null, "digits around / are not a split");
const t = "c: why\nd: but it's okay\nc: you don't like it";
eq(pc(t, "kid"), pc(t, "kid"), "parseCardText is deterministic (same text twice = same turns)");
eq(pc("c: a\nb", "kid").map((x) => x.spk), ["kid", "kid"], "line with no letter continues speaker");
process.exit(bad ? 1 : 0);
